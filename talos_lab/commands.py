"""One function per user-facing command. This is the only layer that
sequences engine adapters (tofu/talosctl/kubeconfig/network) together
and it is the only layer that reads/writes state -- cli.py just calls
into here and prints results.

Every step in create_lab() is gated on a state flag so re-running
create after a partial failure resumes instead of restarting.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from talos_lab import config, images, kubeconfig, network, paths, state, talosctl, tofu, vms
from talos_lab.exceptions import LabNotFoundError, TalosLabError

console = Console()


def create_lab(
    name: str,
    worker_count: int,
    cp_profile_name: str = "medium",
    worker_profile_name: str = "medium",
) -> None:
    paths.ensure_root_dirs()

    if not state.lab_exists(name):
        net = network.allocate_network(name)
        # Snapshot the version + resolved VM specs at creation time, not
        # just the profile/version names -- both can be edited or repinned
        # later, and this lab should keep reporting what it was actually
        # built with (`list` and resumed `create` calls both read this
        # back instead of re-resolving the current global config).
        talos_version = config.get_talos_version()
        cp_profile = config.get_vm_profile(cp_profile_name)
        worker_profile = config.get_vm_profile(worker_profile_name)
        state.register_lab(
            name,
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "worker_count": worker_count,
                "control_plane_profile": cp_profile_name,
                "worker_profile": worker_profile_name,
                "control_plane_spec": cp_profile,
                "worker_spec": worker_profile,
                "network_cidr": net.cidr,
                "network_index": net.index,
                "network_name": net.name,
                "talos_version": talos_version,
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
            dhcp_end=meta["network_cidr"].replace(".0/24", ".254"),
        )
        talos_version = meta["talos_version"]
        cp_profile = meta["control_plane_spec"]
        worker_profile = meta["worker_spec"]

    paths.ensure_lab_dirs(name)
    lab_state = state.load_lab_state(name)

    if not lab_state["tofu_state_done"]:
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
        }
        tofu.render_tofu_files(paths.lab_tofu_dir(name), template_context)
        tofu.render_network_xml(paths.lab_network_dir(name), template_context)
        tofu.init(paths.lab_tofu_dir(name))
        tofu.apply(paths.lab_tofu_dir(name), talos_image_path=str(talos_image))
        lab_state = state.update_lab_state(name, tofu_state_done=True)

    if not lab_state["talos_bootstrapped"] or not lab_state.get("control_plane_ip"):
        console.print(f"[bold]{name}[/bold]: discovering node IPs...")
        outputs = tofu.output(paths.lab_tofu_dir(name))
        macs = [outputs["control_plane_mac"], *outputs["worker_macs"]]
        leases = network.wait_for_leases(net.name, macs)
        cp_ip = leases[outputs["control_plane_mac"].lower()]
        worker_ips = [leases[mac.lower()] for mac in outputs["worker_macs"]]
        lab_state = state.update_lab_state(
            name, control_plane_ip=cp_ip, worker_ips=worker_ips
        )

    if not lab_state["talos_bootstrapped"]:
        console.print(f"[bold]{name}[/bold]: generating and applying Talos config...")
        talos_dir = paths.lab_talos_dir(name)
        talosctl.gen_config(name, lab_state["control_plane_ip"], talos_dir)
        talosconfig = paths.lab_talosconfig_file(name)

        talosctl.apply_config(
            lab_state["control_plane_ip"], talos_dir / "controlplane.yaml", talosconfig
        )
        for worker_ip in lab_state["worker_ips"]:
            talosctl.apply_config(worker_ip, talos_dir / "worker.yaml", talosconfig)

        console.print(f"[bold]{name}[/bold]: bootstrapping cluster...")
        talosctl.bootstrap(lab_state["control_plane_ip"], talosconfig)
        lab_state = state.update_lab_state(name, talos_bootstrapped=True)

    if not lab_state["kubeconfig_ready"]:
        console.print(f"[bold]{name}[/bold]: waiting for kubeconfig...")
        talosctl.wait_for_kubeconfig(
            lab_state["control_plane_ip"],
            paths.lab_talosconfig_file(name),
            paths.lab_kubeconfig_file(name),
        )
        kubeconfig.merge_into_global(paths.lab_kubeconfig_file(name), name)
        lab_state = state.update_lab_state(name, kubeconfig_ready=True)

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
        worker_spec = _format_spec(meta.get("worker_profile", "?"), meta.get("worker_spec"))
        table.add_row(
            marker,
            lab_name,
            meta.get("talos_version", "unknown"),
            f"1 x {cp_spec}",
            f"{meta['worker_count']} x {worker_spec}",
            "yes" if lab_state["kubeconfig_ready"] else "no",
        )

    console.print(table)


def use_lab(name: str) -> None:
    if not state.lab_exists(name):
        raise LabNotFoundError(name)
    kubeconfig.use_context(name)
    console.print(f"switched to lab '{name}'")


def _require_provisioned(name: str) -> dict:
    if not state.lab_exists(name):
        raise LabNotFoundError(name)
    if not state.load_lab_state(name)["tofu_state_done"]:
        raise TalosLabError(f"lab '{name}' has no provisioned VMs yet -- run `talos-lab create` first")
    return state.get_lab_meta(name)


def start_lab(name: str) -> None:
    meta = _require_provisioned(name)
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
