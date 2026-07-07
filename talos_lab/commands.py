"""One function per user-facing command. This is the only layer that
sequences engine adapters (tofu/talosctl/kubeconfig/network) together
and it is the only layer that reads/writes state -- cli.py just calls
into here and prints results.

Every step in create_lab() is gated on a state flag so re-running
create after a partial failure resumes instead of restarting.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from talos_lab import addons, config, images, kubeconfig, network, paths, state, talosctl, tofu, vms
from talos_lab.exceptions import LabNotFoundError, TalosLabError

console = Console()

SMALL_PROFILES = {"micro", "small"}


def _prompt_for_profile(role: str, default: str = "medium") -> str:
    """Interactive only -- callers pass an explicit profile name (from
    --cp-profile/--worker-profile) to skip this entirely. Shows the actual
    cpu/memory/disk for every profile rather than just profile names, since
    "medium" on its own tells you nothing about what you're about to boot.
    """
    profiles = config.load_vm_profiles()
    console.print(f"[bold]{role} VM profile:[/bold]")
    for profile_name, spec in profiles.items():
        marker = "  (default)" if profile_name == default else ""
        console.print(
            f"  {profile_name:<8} {spec['cpu']} vCPU / {spec['memory']}MB / {spec['disk']}GB{marker}"
        )
    return Prompt.ask(f"{role} profile", choices=list(profiles.keys()), default=default)


def _prompt_addons_enabled(
    cp_profile_name: str, worker_profile_name: str, single_node: bool, assume_yes: bool
) -> bool:
    """Only called once, at first registration -- see RESUME CONSISTENCY:
    a resumed `create` reads this back from the registry (addons_enabled)
    instead of re-prompting. DaemonSet-style addons (metallb, ingress-nginx,
    and Cilium itself) run on every node including the control plane, so a
    constrained control-plane profile matters here even in a multi-node
    lab, not just a constrained worker profile.
    """
    constrained = cp_profile_name in SMALL_PROFILES or (
        not single_node and worker_profile_name in SMALL_PROFILES
    )
    if not constrained or assume_yes:
        return True

    console.print(
        "[yellow]note:[/yellow] this lab's VM profile is small enough that the standard "
        "add-on complement can meaningfully crowd out workloads:"
    )
    for addon_name in addons.enabled_addon_names():
        console.print(f"  - {addon_name:<20} {addons.ADDON_DESCRIPTIONS.get(addon_name, '')}")
    console.print(
        "Cilium (the cluster's CNI) is always installed regardless of this choice -- "
        "NetworkPolicy support isn't optional."
    )
    return Confirm.ask("Install the standard add-on complement?", default=True)


def _prompt_permissive(assume_yes: bool) -> bool:
    """Only called once, at first registration, and only when --permissive
    wasn't already passed explicitly (that skips this prompt and implies
    yes -- see create_lab). Resumed `create` calls read the answer back
    from the registry (permissive) instead of re-prompting, same pattern
    as _prompt_addons_enabled above.

    Default is "no": talos-lab's baseline posture (Talos's own stricter
    default, see SECURITY.md) is enforced unless the user opts out, not
    the other way around. --yes alone (without --permissive) answers "no"
    here, same as it answers "yes" for _prompt_addons_enabled -- --yes
    means "don't ask, take the safe/sane default" for every prompt, and
    the safe default for security posture is the stricter one.
    """
    if assume_yes:
        return False
    console.print(
        "[yellow]note:[/yellow] permissive mode sets this cluster's Pod Security Admission "
        "to \"privileged\" cluster-wide (GKE Standard's default posture) -- any pod is "
        "admitted, and workloads you deploy are YOUR responsibility to secure, not the "
        "platform's. Talos's own default (baseline enforcement) is stricter than this and "
        "is what you get by declining."
    )
    return Confirm.ask("Install this cluster in permissive PSA mode?", default=False)


def create_lab(
    name: str,
    worker_count: int | None,
    cp_profile_name: str | None = None,
    worker_profile_name: str | None = None,
    single_node: bool = False,
    assume_yes: bool = False,
    permissive: bool = False,
) -> None:
    paths.ensure_root_dirs()

    if not state.lab_exists(name):
        # worker_count/single_node are only meaningful for a brand new
        # lab -- on resume they're read back from the stored registry
        # entry below instead, so an incomplete/inconsistent resume
        # invocation (e.g. forgetting --single-node) can't diverge from
        # what was actually registered.
        if single_node:
            if worker_count not in (None, 0):
                raise TalosLabError("--single-node can't be combined with a nonzero worker count")
            worker_count = 0
        elif worker_count is None:
            raise TalosLabError("worker_count is required unless --single-node is passed")

        net = network.allocate_network(name)
        if cp_profile_name is None:
            cp_profile_name = _prompt_for_profile("Control-plane")
        if single_node:
            # No workers exist in this topology -- prompting for a worker
            # profile would just be confusing. The value is inert (0
            # instances get created) but the .tf template still needs a
            # valid profile dict to render, so reuse the control-plane one.
            worker_profile_name = cp_profile_name
        elif worker_profile_name is None:
            worker_profile_name = _prompt_for_profile("Worker")
        addons_enabled = _prompt_addons_enabled(
            cp_profile_name, worker_profile_name, single_node, assume_yes
        )
        # --permissive on the command line skips the prompt and implies
        # yes, same as an explicit --cp-profile skips _prompt_for_profile
        # above -- only prompt when the choice wasn't already made.
        permissive_enabled = True if permissive else _prompt_permissive(assume_yes)
        # Snapshot the version + resolved VM specs at creation time, not
        # just the profile/version names -- both can be edited or repinned
        # later, and this lab should keep reporting what it was actually
        # built with (`list` and resumed `create` calls both read this
        # back instead of re-resolving the current global config).
        talos_version = config.get_talos_version()
        cp_profile = config.get_vm_profile(cp_profile_name)
        worker_profile = config.get_vm_profile(worker_profile_name)
        # MACs are generated here (not read back from a Terraform output)
        # so we can wait for their DHCP leases right after `apply` without
        # any extra round-trip -- the provider has no stable "MAC of this
        # interface" computed attribute we can rely on.
        control_plane_mac = network.generate_mac()
        worker_macs = [network.generate_mac() for _ in range(worker_count)]
        state.register_lab(
            name,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "worker_count": worker_count,
                "single_node": single_node,
                "control_plane_profile": cp_profile_name,
                "worker_profile": worker_profile_name,
                "control_plane_spec": cp_profile,
                "worker_spec": worker_profile,
                "network_cidr": net.cidr,
                "network_index": net.index,
                "network_name": net.name,
                # dhcp_end/metallb_pool_* are snapshotted (not just the
                # fixed-offset gateway/dhcp_start recomputed below) because
                # the offsets themselves can change between talos-lab
                # versions -- see network.py's DHCP/MetalLB split comment.
                # A resume must keep using whatever was ACTUALLY applied
                # by Terraform for this lab, not a newer default.
                "network_dhcp_end": net.dhcp_end,
                "network_metallb_pool_start": net.metallb_pool_start,
                "network_metallb_pool_end": net.metallb_pool_end,
                "talos_version": talos_version,
                "control_plane_mac": control_plane_mac,
                "worker_macs": worker_macs,
                "addons_enabled": addons_enabled,
                "permissive": permissive_enabled,
            },
        )
        state.save_lab_state(name, dict(state.DEFAULT_LAB_STATE))
    else:
        meta = state.get_lab_meta(name)
        net = network.LabNetwork(
            name=meta["network_name"],
            index=meta["network_index"],
            cidr=meta["network_cidr"],
            gateway=meta["network_cidr"].replace(".0/24", ".1"),
            dhcp_start=meta["network_cidr"].replace(".0/24", ".2"),
            # Fall back to the pre-MetalLB-split full range/no pool for
            # labs registered before these keys existed -- see the
            # snapshot comment above and network.py's split comment.
            dhcp_end=meta.get("network_dhcp_end", meta["network_cidr"].replace(".0/24", ".254")),
            metallb_pool_start=meta.get("network_metallb_pool_start"),
            metallb_pool_end=meta.get("network_metallb_pool_end"),
        )
        talos_version = meta["talos_version"]
        cp_profile = meta["control_plane_spec"]
        worker_profile = meta["worker_spec"]
        control_plane_mac = meta["control_plane_mac"]
        worker_macs = meta["worker_macs"]
        # Resuming trusts the stored topology, not whatever was passed to
        # this invocation -- same reasoning as talos_version/profiles above.
        # Otherwise a resume that forgets --single-node (or passes a
        # different worker_count) would silently diverge from what was
        # actually registered.
        worker_count = meta["worker_count"]
        single_node = meta.get("single_node", False)
        # Missing on labs registered before this addon system existed --
        # default to installing the standard complement, same as the
        # prompt's own default.
        addons_enabled = meta.get("addons_enabled", True)
        # Missing on labs registered before --permissive existed -- default
        # to Talos's own stricter baseline enforcement (False), same
        # "unsafe to retroactively relax" reasoning as cni_installed's
        # grandfathering in STATE MODEL: an old lab should keep whatever
        # posture it was actually created with, not silently loosen.
        permissive_enabled = meta.get("permissive", False)

    paths.ensure_lab_dirs(name)
    lab_state = state.load_lab_state(name)

    if not lab_state["tofu_state_done"]:
        # Same reasoning as start_lab's check: this is a laptop with
        # finite RAM, and `create` is the OTHER place (besides `start`)
        # that actually boots VMs (tofu apply sets running = true). Check
        # right here, not earlier in the function, so it only fires when
        # we're actually about to provision -- a resumed create that's
        # already past this stage shouldn't re-prompt.
        if not assume_yes:
            already_running = _labs_with_running_vms(exclude=name)
            if already_running:
                console.print(
                    f"[yellow]warning:[/yellow] lab(s) already running: {', '.join(already_running)}"
                )
                console.print("creating another lab increases memory pressure. Current host memory:")
                free_output = subprocess.run(["free", "-h"], capture_output=True, text=True)
                console.print(free_output.stdout)
                if not Confirm.ask(f"Provision '{name}' anyway?", default=False):
                    console.print(
                        "aborted -- VMs not provisioned. Re-run `taloslab create` to resume."
                    )
                    return

        console.print(f"[bold]{name}[/bold]: provisioning VMs + network via OpenTofu...")
        talos_image = images.ensure_image(talos_version)
        template_context = {
            "lab_name": name,
            "network_name": net.name,
            "network_cidr": net.cidr,
            "network_gateway": net.gateway,
            "network_dhcp_start": net.dhcp_start,
            "network_dhcp_end": net.dhcp_end,
            "worker_count": worker_count,
            "cp_profile": cp_profile,
            "worker_profile": worker_profile,
            "control_plane_mac": control_plane_mac,
            "worker_macs": worker_macs,
        }
        tofu.render_tofu_files(paths.lab_tofu_dir(name), template_context)
        tofu.render_network_xml(paths.lab_network_dir(name), template_context)
        tofu.init(paths.lab_tofu_dir(name))
        tofu.apply(paths.lab_tofu_dir(name), talos_image_path=str(talos_image))
        lab_state = state.update_lab_state(name, tofu_state_done=True)

    if not lab_state.get("control_plane_ip"):
        console.print(f"[bold]{name}[/bold]: discovering node IPs...")
        macs = [control_plane_mac, *worker_macs]
        leases = network.wait_for_leases(net.name, macs)
        cp_ip = leases[control_plane_mac.lower()]
        worker_ips = [leases[mac.lower()] for mac in worker_macs]
        lab_state = state.update_lab_state(
            name, control_plane_ip=cp_ip, worker_ips=worker_ips
        )

    talosconfig = paths.lab_talosconfig_file(name)

    if not lab_state["config_applied"]:
        # Once apply-config succeeds, the node drops out of insecure
        # maintenance mode and starts requiring a client cert -- retrying
        # this step with --insecure after a partial failure would fail
        # differently ("tls: certificate required"), so gate it on its
        # own flag rather than lumping it in with talos_bootstrapped.
        console.print(f"[bold]{name}[/bold]: generating and applying Talos config...")
        talos_dir = paths.lab_talos_dir(name)
        talosctl.gen_config(
            name,
            lab_state["control_plane_ip"],
            talos_dir,
            talos_version,
            allow_scheduling_on_control_plane=single_node,
            coredns_image=addons.coredns_image_override(),
            permissive=permissive_enabled,
        )

        talosctl.apply_config(
            lab_state["control_plane_ip"], talos_dir / "controlplane.yaml", talosconfig
        )
        for worker_ip in lab_state["worker_ips"]:
            talosctl.apply_config(worker_ip, talos_dir / "worker.yaml", talosconfig)
        lab_state = state.update_lab_state(name, config_applied=True)

    if not lab_state["talos_bootstrapped"]:
        console.print(f"[bold]{name}[/bold]: bootstrapping cluster...")
        talosctl.bootstrap(lab_state["control_plane_ip"], talosconfig)
        lab_state = state.update_lab_state(name, talos_bootstrapped=True)

    if not lab_state["cni_installed"]:
        # Needs the kubeconfig fetched (idempotent -- wait_for_kubeconfig
        # is re-called below too, this just needs it a step earlier).
        # Nodes stay NotReady without a CNI, so this must run before
        # wait_for_nodes_ready below, not after.
        console.print(f"[bold]{name}[/bold]: waiting for kubeconfig...")
        talosctl.wait_for_kubeconfig(
            lab_state["control_plane_ip"],
            paths.lab_talosconfig_file(name),
            paths.lab_kubeconfig_file(name),
        )
        console.print(f"[bold]{name}[/bold]: installing CNI (Cilium, for NetworkPolicy support)...")
        addons.install_cni(paths.lab_kubeconfig_file(name))
        lab_state = state.update_lab_state(name, cni_installed=True)

    if not lab_state["kubeconfig_ready"]:
        console.print(f"[bold]{name}[/bold]: waiting for kubeconfig...")
        talosctl.wait_for_kubeconfig(
            lab_state["control_plane_ip"],
            paths.lab_talosconfig_file(name),
            paths.lab_kubeconfig_file(name),
        )
        # Kubeconfig being fetchable only proves the API server answered
        # once -- it says nothing about whether every node has actually
        # joined and gone Ready yet. Without this, `create` could report
        # "ready" while the apiserver was still mid-startup and about to
        # become briefly unreachable again (observed in practice).
        console.print(f"[bold]{name}[/bold]: waiting for nodes to join and become Ready...")
        kubeconfig.wait_for_nodes_ready(
            paths.lab_kubeconfig_file(name), expected_count=1 + len(lab_state["worker_ips"])
        )
        if single_node:
            kubeconfig.label_single_node_as_worker(paths.lab_kubeconfig_file(name))
        elif lab_state["worker_ips"]:
            kubeconfig.label_worker_nodes(paths.lab_kubeconfig_file(name))
        kubeconfig.merge_into_global(paths.lab_kubeconfig_file(name), name)
        lab_state = state.update_lab_state(name, kubeconfig_ready=True)

    if not lab_state["addons_installed"]:
        if addons_enabled:
            console.print(f"[bold]{name}[/bold]: installing add-ons...")
            skipped = addons.install_addons(
                paths.lab_kubeconfig_file(name),
                metallb_pool_start=net.metallb_pool_start,
                metallb_pool_end=net.metallb_pool_end,
            )
            if "metallb" in skipped:
                console.print(
                    "[yellow]warning:[/yellow] skipped metallb -- this lab predates the "
                    "MetalLB IP pool reservation. Recreate the lab to get MetalLB support."
                )
        else:
            console.print(f"[bold]{name}[/bold]: skipping optional add-ons (declined at creation)")
        lab_state = state.update_lab_state(name, addons_installed=True)

    console.print(f"[green]lab '{name}' ready[/green] -- context: {kubeconfig.context_name(name)}")


def _format_spec(profile_name: str, spec: dict | None) -> str:
    if not spec:
        return profile_name  # older lab predating spec snapshotting -- best effort
    return f"{profile_name} ({spec['cpu']}vCPU/{spec['memory']}MB/{spec['disk']}GB)"


def list_labs() -> None:
    registry = state.load_registry()
    active = kubeconfig.current_context()

    table = Table()
    table.add_column("")
    table.add_column("lab")
    table.add_column("talos")
    table.add_column("control-plane")
    table.add_column("workers")
    table.add_column("ready")

    for lab_name, meta in registry["labs"].items():
        marker = "*" if kubeconfig.context_name(lab_name) == active else ""
        lab_state = state.load_lab_state(lab_name)
        cp_spec = _format_spec(meta.get("control_plane_profile", "?"), meta.get("control_plane_spec"))
        workers_column = (
            "-- (single-node)"
            if meta.get("single_node")
            else f"{meta['worker_count']} x {_format_spec(meta.get('worker_profile', '?'), meta.get('worker_spec'))}"
        )
        table.add_row(
            marker,
            lab_name,
            meta.get("talos_version", "unknown"),
            f"1 x {cp_spec}",
            workers_column,
            "yes" if lab_state["kubeconfig_ready"] else "no",
        )

    console.print(table)


BOOTSTRAP_STAGES = (
    ("tofu_state_done", "VMs provisioned (OpenTofu)"),
    ("config_applied", "Talos config applied"),
    ("talos_bootstrapped", "Cluster bootstrapped"),
    ("cni_installed", "CNI installed (Cilium)"),
    ("kubeconfig_ready", "Kubeconfig retrieved + nodes Ready"),
    ("addons_installed", "Add-ons installed"),
)


def show_status(name: str) -> None:
    """Reports VM status, bootstrap stage, and live cluster readiness for
    a lab -- unlike every other command here, this must work (and degrade
    gracefully, never raise) at ANY bootstrap stage, including before
    anything has been provisioned at all. Partial/broken states are
    exactly what this command exists to inspect.
    """
    if not state.lab_exists(name):
        raise LabNotFoundError(name)

    meta = state.get_lab_meta(name)
    lab_state = state.load_lab_state(name)

    topology = "single-node" if meta.get("single_node") else f"1 control-plane + {meta['worker_count']} worker(s)"
    addons_note = "enabled" if meta.get("addons_enabled", True) else "declined at creation"
    psa_note = "permissive" if meta.get("permissive", False) else "enforced (baseline)"
    console.print(
        f"[bold]{name}[/bold]  talos={meta.get('talos_version', 'unknown')}  "
        f"topology={topology}  network={meta.get('network_name', '?')}  add-ons={addons_note}  "
        f"psa={psa_note}"
    )

    console.print("\n[bold]Bootstrap stage:[/bold]")
    for flag, label in BOOTSTRAP_STAGES:
        mark = "[green]done   [/green]" if lab_state[flag] else "[yellow]pending[/yellow]"
        console.print(f"  {mark}  {label}")

    console.print("\n[bold]VMs:[/bold]")
    domains = vms.domain_names(name, meta["worker_count"])
    ips = [lab_state.get("control_plane_ip"), *lab_state.get("worker_ips", [])]

    vm_table = Table()
    vm_table.add_column("domain")
    vm_table.add_column("role")
    vm_table.add_column("virsh state")
    vm_table.add_column("ip")
    for i, domain in enumerate(domains):
        role = "control-plane" if i == 0 else "worker"
        virsh_state = vms.domain_state_or_none(domain) or "not created"
        ip = ips[i] if i < len(ips) and ips[i] else "-"
        vm_table.add_row(domain, role, virsh_state, ip)
    console.print(vm_table)

    console.print("[bold]Cluster:[/bold]")
    if not lab_state["kubeconfig_ready"]:
        console.print("  kubeconfig not yet retrieved")
    else:
        nodes = kubeconfig.get_node_statuses(paths.lab_kubeconfig_file(name))
        if nodes is None:
            console.print("  [yellow]unreachable[/yellow] (VMs may be stopped, or API server not responding)")
        elif not nodes:
            console.print("  no nodes reported")
        else:
            for node in nodes:
                status_text = "[green]Ready[/green]" if node["ready"] else "[yellow]NotReady[/yellow]"
                console.print(f"  {node['name']}: {status_text}")

    context = kubeconfig.context_name(name)
    active_marker = " (active)" if kubeconfig.current_context() == context else ""
    console.print(f"\nkube context: {context}{active_marker}")


def show_status_all() -> None:
    """Same report as show_status(), once per registered lab. Each lab is
    independent -- an unexpected failure reporting on one lab doesn't stop
    the rest from being shown (show_status itself already degrades
    gracefully for normal partial-bootstrap states; this only guards
    against something truly unexpected, e.g. a corrupted state.json).
    """
    lab_names = list(state.load_registry()["labs"].keys())

    if not lab_names:
        console.print("no labs registered")
        return

    for i, lab_name in enumerate(lab_names):
        if i > 0:
            console.rule()
        try:
            show_status(lab_name)
        except TalosLabError as e:
            console.print(f"[bold]{lab_name}[/bold]")
            console.print(f"[red]error:[/red] {e}")


def use_lab(name: str) -> None:
    if not state.lab_exists(name):
        raise LabNotFoundError(name)
    kubeconfig.use_context(name)
    console.print(f"switched to lab '{name}'")


def _require_provisioned(name: str) -> dict:
    if not state.lab_exists(name):
        raise LabNotFoundError(name)
    if not state.load_lab_state(name)["tofu_state_done"]:
        raise TalosLabError(f"lab '{name}' has no provisioned VMs yet -- run `taloslab create` first")
    return state.get_lab_meta(name)


def _labs_with_running_vms(exclude: str) -> list[str]:
    running = []
    for lab_name, meta in state.load_registry()["labs"].items():
        if lab_name == exclude:
            continue
        domains = vms.domain_names(lab_name, meta.get("worker_count", 0))
        if any(vms.domain_state_or_none(d) == "running" for d in domains):
            running.append(lab_name)
    return running


def start_lab(name: str, assume_yes: bool = False) -> None:
    meta = _require_provisioned(name)

    # This is meant to run on a laptop with finite RAM (32-64GB, not
    # unlimited) -- each lab's VMs are sized independently and talos-lab
    # has no cross-lab awareness of total memory commitment, so starting
    # a second lab on top of one already running is an easy way to
    # overcommit the host silently. Warn and confirm rather than block
    # outright: there are legitimate reasons to run two labs at once,
    # this just makes sure it's a deliberate choice.
    if not assume_yes:
        already_running = _labs_with_running_vms(exclude=name)
        if already_running:
            console.print(
                f"[yellow]warning:[/yellow] lab(s) already running: {', '.join(already_running)}"
            )
            console.print("starting another lab increases memory pressure. Current host memory:")
            free_output = subprocess.run(["free", "-h"], capture_output=True, text=True)
            console.print(free_output.stdout)
            if not Confirm.ask(f"Start '{name}' anyway?", default=False):
                console.print("aborted -- lab not started")
                return

    console.print(f"[bold]{name}[/bold]: starting VMs...")
    for domain in vms.domain_names(name, meta["worker_count"]):
        vms.start_domain(domain)
    console.print(f"[green]lab '{name}' started[/green]")


def stop_lab(name: str, force: bool = False) -> None:
    meta = _require_provisioned(name)
    console.print(f"[bold]{name}[/bold]: stopping VMs...")
    for domain in vms.domain_names(name, meta["worker_count"]):
        vms.stop_domain(domain, force=force)
    console.print(f"[green]lab '{name}' stopped[/green]")


def stop_all_labs(force: bool = False) -> None:
    """Stop every registered lab's VMs, one after another -- e.g. before
    suspending a laptop, since host suspend freezes VMs in place rather
    than shutting them down cleanly (see README section 4a). Mirrors
    show_status_all()'s per-lab isolation: one lab failing to stop (or
    not being provisioned yet) doesn't stop the rest from being
    attempted, but a failure anywhere is still surfaced via a nonzero
    exit so a pre-suspend script can tell not everything actually
    stopped.
    """
    lab_names = list(state.load_registry()["labs"].keys())

    if not lab_names:
        console.print("no labs registered")
        return

    failures = []
    for lab_name in lab_names:
        if not state.load_lab_state(lab_name)["tofu_state_done"]:
            console.print(f"[bold]{lab_name}[/bold]: skipping (not provisioned yet)")
            continue
        try:
            stop_lab(lab_name, force=force)
        except TalosLabError as e:
            failures.append(lab_name)
            console.print(f"[red]error stopping '{lab_name}':[/red] {e}")

    if failures:
        raise TalosLabError(f"failed to stop: {', '.join(failures)}")


def delete_lab(name: str) -> None:
    if not state.lab_exists(name):
        raise LabNotFoundError(name)

    tofu_dir = paths.lab_tofu_dir(name)
    if tofu_dir.exists():
        console.print(f"[bold]{name}[/bold]: destroying VMs + network via OpenTofu...")
        meta = state.get_lab_meta(name)
        # Use the version this lab was actually built with, not whatever
        # is currently pinned -- `version set` may have moved on since.
        lab_talos_version = meta.get("talos_version") or config.get_talos_version()
        talos_image = images.image_path(lab_talos_version)
        tofu.destroy(tofu_dir, talos_image_path=str(talos_image))

    kubeconfig.remove_context(name)
    state.unregister_lab(name)

    import shutil

    shutil.rmtree(paths.lab_dir(name), ignore_errors=True)
    console.print(f"[green]lab '{name}' deleted[/green]")


def version_set(talos_version: str) -> None:
    config.set_talos_version(talos_version)
    console.print(f"talos version set to {talos_version}")


def version_show() -> None:
    console.print(config.get_talos_version())


def get_image(version: str | None, assume_yes: bool = False) -> None:
    talos_version = images.normalize_version(version) if version else config.get_talos_version()
    target = images.image_path(talos_version)

    installed_version = config._detect_talosctl_version()
    if installed_version and installed_version != talos_version:
        console.print(
            f"[yellow]warning:[/yellow] installed talosctl is {installed_version}, "
            f"but fetching an image for {talos_version}. Version drift between the "
            "talosctl client and the Talos OS image can cause inconsistent results."
        )
        if not assume_yes:
            proceed = Confirm.ask("Proceed anyway?", default=False)
            if not proceed:
                console.print("aborted -- no image fetched")
                return

    if target.exists() and not assume_yes:
        overwrite = Confirm.ask(
            f"Image for {talos_version} already exists at {target}. Overwrite?", default=False
        )
        if not overwrite:
            console.print("aborted -- existing image left in place")
            return

    console.print(f"fetching Talos {talos_version} image...")
    path = images.download_image(talos_version)
    console.print(f"[green]saved {path}[/green]")


def put_image(source: str, version: str | None, assume_yes: bool = False) -> None:
    talos_version = images.normalize_version(version) if version else config.get_talos_version()
    source_path = Path(source).expanduser()
    target = images.image_path(talos_version)

    if target.exists() and not assume_yes:
        overwrite = Confirm.ask(
            f"Image for {talos_version} already exists at {target}. Overwrite?", default=False
        )
        if not overwrite:
            console.print("aborted -- existing image left in place")
            return

    console.print(f"copying {source_path} -> {target}...")
    path = images.import_image(source_path, talos_version)
    console.print(f"[green]saved {path}[/green]")
