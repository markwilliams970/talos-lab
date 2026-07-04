"""Post-bootstrap Helm installs, driven entirely by
~/.talos-lab/templates/addons.yaml (seeded from the package default on
first use, same pattern as config.py's vm-profiles.yaml) so chart
versions/values can be customized without a code change.

Two distinct install paths:
- install_cni(): the CNI (Cilium) -- mandatory, no `enabled` toggle,
  always installed. Talos's built-in Flannel CNI has no NetworkPolicy
  support, so gen_config always disables it and this always replaces it.
- install_addons(): the optional standard complement (metrics-server,
  cert-manager, kube-state-metrics, ingress-nginx, metallb) -- each can be
  toggled off in addons.yaml, and commands.py additionally gates the whole
  step on the lab's own addons_enabled choice (the micro/small opt-out
  prompt at creation time).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from talos_lab import paths
from talos_lab.exceptions import AddonInstallError

_PACKAGE_DEFAULT_ADDONS = Path(__file__).parent / "templates" / "addons.yaml"

METALLB_CR_APPLY_RETRIES = 6
METALLB_CR_APPLY_RETRY_INTERVAL_SECONDS = 5

CNI_INSTALL_POLL_INTERVAL_SECONDS = 5
CNI_INSTALL_TIMEOUT_SECONDS = 300

# Shown in the create-time opt-out prompt (commands.py) -- not read from
# addons.yaml, since that file only owns chart mechanics, not user-facing
# copy. Falls back to just the bare name for any addon in addons.yaml this
# dict doesn't know about (e.g. one the user added themselves).
ADDON_DESCRIPTIONS = {
    "metrics-server": "resource metrics for `kubectl top`",
    "cert-manager": "TLS certificate automation",
    "kube-state-metrics": "cluster object state metrics",
    "ingress-nginx": "HTTP(S) ingress controller",
    "metallb": "LoadBalancer IPs on this bare libvirt network",
}


def _seed_defaults() -> None:
    paths.ensure_root_dirs()
    if not paths.ADDONS_FILE.exists():
        shutil.copyfile(_PACKAGE_DEFAULT_ADDONS, paths.ADDONS_FILE)


def load_addons_config() -> dict[str, Any]:
    _seed_defaults()
    with open(paths.ADDONS_FILE) as f:
        return yaml.safe_load(f)


def coredns_image_override() -> str | None:
    image = load_addons_config().get("coredns", {}).get("image")
    return image or None


def enabled_addon_names() -> list[str]:
    """For the commands.py opt-out prompt -- only addons actually enabled
    in addons.yaml, in file order, so a user who disabled one there
    doesn't get prompted about it."""
    cfg = load_addons_config()
    return [name for name, spec in cfg.get("addons", {}).items() if spec.get("enabled", True)]


def _run_helm(args: list[str]) -> None:
    result = subprocess.run(["helm", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise AddonInstallError(args, result.returncode, result.stderr.strip())


def _helm_repo_add(repo_name: str, repo_url: str) -> None:
    # --force-update makes this idempotent even if repo_name is already
    # registered pointing at a different URL (e.g. left over from outside
    # talos-lab, or a chart source that moved).
    _run_helm(["repo", "add", repo_name, repo_url, "--force-update"])
    _run_helm(["repo", "update", repo_name])


def _install_chart(kubeconfig_path: Path, release_name: str, namespace: str, spec: dict[str, Any]) -> None:
    _helm_repo_add(spec["repo_name"], spec["repo_url"])
    fd, values_file = tempfile.mkstemp(prefix=f".addon-values-{release_name}-", suffix=".yaml")
    try:
        with open(fd, "w") as f:
            yaml.safe_dump(spec.get("values") or {}, f)
        _run_helm(
            [
                "upgrade",
                "--install",
                release_name,
                f"{spec['repo_name']}/{spec['chart_name']}",
                "--version",
                str(spec["chart_version"]),
                "--namespace",
                namespace,
                "--create-namespace",
                "--kubeconfig",
                str(kubeconfig_path),
                "--values",
                values_file,
                "--wait",
                "--timeout",
                "5m",
            ]
        )
    finally:
        Path(values_file).unlink(missing_ok=True)


def _ensure_privileged_namespace(kubeconfig_path: Path, namespace: str) -> None:
    """MetalLB's speaker DaemonSet needs hostNetwork, hostPort, and the
    NET_ADMIN/NET_RAW/SYS_ADMIN capabilities to do L2 announcement.
    Talos's default cluster-wide Pod Security admission config enforces
    "baseline" on every namespace except kube-system -- FOUND IN
    PRACTICE, not anticipated: `helm upgrade --install --wait` for
    metallb timed out because the speaker DaemonSet's pods were being
    outright REJECTED at admission ("forbidden: violates PodSecurity
    baseline:latest"), not because anything was slow. Must run BEFORE
    the chart installs -- helm's own --create-namespace doesn't set
    PodSecurity labels, and the DaemonSet controller doesn't proactively
    retry rejected pods the moment the namespace is relabeled after the
    fact (confirmed: needed an explicit `kubectl rollout restart` to
    nudge it during manual testing), so labeling after `helm --wait` has
    already started racing the DaemonSet isn't reliable.
    """
    subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig_path), "create", "namespace", namespace],
        capture_output=True,
    )
    subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "label",
            "namespace",
            namespace,
            "pod-security.kubernetes.io/enforce=privileged",
            "pod-security.kubernetes.io/audit=privileged",
            "pod-security.kubernetes.io/warn=privileged",
            "--overwrite",
        ],
        check=True,
    )


def install_cni(kubeconfig_path: Path) -> None:
    """Nodes stay NotReady until this succeeds -- kubelet ties overall
    node readiness to the container runtime's CNI status -- so
    commands.py runs this before waiting for nodes to go Ready, right
    after gen_config has already disabled Flannel (cluster.network.cni.name:
    "none").

    Retries the whole helm install, not just a reachability check first:
    right after `talosctl bootstrap`, the apiserver can accept the
    kubeconfig fetch moments earlier and then briefly go unreachable
    again while it finishes starting up -- the exact same flakiness
    kubeconfig.wait_for_nodes_ready() already retries around, just hit
    earlier here since this step runs before that wait. A failed attempt
    doesn't create the Helm release at all (Helm couldn't reach the
    cluster to do anything), so retrying the full command is safe.
    """
    cni_cfg = load_addons_config()["cni"]
    deadline = time.monotonic() + CNI_INSTALL_TIMEOUT_SECONDS
    last_error: AddonInstallError | None = None
    while time.monotonic() < deadline:
        try:
            _install_chart(kubeconfig_path, release_name="cilium", namespace=cni_cfg["namespace"], spec=cni_cfg)
            return
        except AddonInstallError as e:
            last_error = e
            time.sleep(CNI_INSTALL_POLL_INTERVAL_SECONDS)
    raise last_error


def _metallb_pool_manifest(pool_start: str, pool_end: str) -> str:
    return (
        "apiVersion: metallb.io/v1beta1\n"
        "kind: IPAddressPool\n"
        "metadata:\n"
        "  name: default\n"
        "  namespace: metallb-system\n"
        "spec:\n"
        "  addresses:\n"
        f"    - {pool_start}-{pool_end}\n"
        "---\n"
        "apiVersion: metallb.io/v1beta1\n"
        "kind: L2Advertisement\n"
        "metadata:\n"
        "  name: default\n"
        "  namespace: metallb-system\n"
        "spec:\n"
        "  ipAddressPools:\n"
        "    - default\n"
    )


def _apply_metallb_pool(kubeconfig_path: Path, pool_start: str, pool_end: str) -> None:
    """MetalLB's validating webhook isn't necessarily up the instant the
    chart's --wait returns (the webhook Service/Pod can take a few more
    seconds) -- retry `kubectl apply` instead of a single attempt, same
    reasoning as talosctl.bootstrap()/wait_for_kubeconfig() retrying
    around a freshly-started component."""
    manifest = _metallb_pool_manifest(pool_start, pool_end)
    last_result = None
    for _ in range(METALLB_CR_APPLY_RETRIES):
        result = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"],
            input=manifest,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_result = result
        time.sleep(METALLB_CR_APPLY_RETRY_INTERVAL_SECONDS)
    raise AddonInstallError(
        ["apply", "metallb-pool"], last_result.returncode if last_result else 1,
        last_result.stderr.strip() if last_result else "",
    )


def install_addons(
    kubeconfig_path: Path,
    metallb_pool_start: str | None = None,
    metallb_pool_end: str | None = None,
) -> list[str]:
    """Installs every addons.yaml entry with enabled: true. Safe to call
    on an already-provisioned lab -- `helm upgrade --install` is
    idempotent, unlike the CNI swap this deliberately does NOT touch.

    metallb is ALWAYS installed first (regardless of its position in
    addons.yaml's `addons` mapping) -- FOUND IN PRACTICE, not anticipated:
    ingress-nginx's chart defaults its Service to type: LoadBalancer, and
    `helm upgrade --install --wait` blocks until that Service actually
    gets an EXTERNAL-IP. If metallb installs afterward (e.g. plain YAML
    file order, metallb listed last), there's nothing yet to assign that
    IP and ingress-nginx's install hangs for its full --wait --timeout
    (5m) before failing outright. Installing metallb (chart + IP pool)
    before anything else sidesteps this for ingress-nginx and any other
    addon/values a user adds later that also wants type: LoadBalancer.

    Returns the names of addons skipped despite being enabled (currently
    only ever "metallb", when no pool range was given -- see
    commands.py's caller, which prints the reason) so the caller can
    surface that rather than have it look like a silent success.
    """
    cfg = load_addons_config()
    addons_cfg = cfg.get("addons", {})
    skipped = []

    metallb_spec = addons_cfg.get("metallb")
    if metallb_spec and metallb_spec.get("enabled", True):
        if metallb_pool_start and metallb_pool_end:
            _ensure_privileged_namespace(kubeconfig_path, metallb_spec["namespace"])
            _install_chart(kubeconfig_path, release_name="metallb", namespace=metallb_spec["namespace"], spec=metallb_spec)
            _apply_metallb_pool(kubeconfig_path, metallb_pool_start, metallb_pool_end)
        else:
            skipped.append("metallb")

    for name, spec in addons_cfg.items():
        if name == "metallb" or not spec.get("enabled", True):
            continue
        _install_chart(kubeconfig_path, release_name=name, namespace=spec["namespace"], spec=spec)
    return skipped
