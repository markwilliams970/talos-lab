"""Talos bootstrap orchestration: gen config -> apply-config -> bootstrap
-> kubeconfig retrieval. Every step is a plain `talosctl` subprocess call;
this module owns none of the Talos machine-config semantics itself.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from talos_lab.exceptions import TalosctlError

BOOTSTRAP_POLL_INTERVAL_SECONDS = 5
BOOTSTRAP_TIMEOUT_SECONDS = 300


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(["talosctl", *args])
    if result.returncode != 0:
        raise TalosctlError(args, result.returncode)
    return result


SINGLE_NODE_CONTROL_PLANE_PATCH = '{"cluster": {"allowSchedulingOnControlPlanes": true}}'

# Talos's own built-in default (verified via `talosctl read
# .../admission-control-config.yaml` against a real control-plane node)
# sets enforce=baseline but warn=restricted/audit=restricted, so any pod
# that merely satisfies baseline (the actual enforced level) still prints
# a "would violate PodSecurity restricted:latest" warning on every
# `kubectl run`/`apply`. That's just noise for a local lab -- this lowers
# warn/audit to match enforce (baseline) so nothing changes about what's
# actually admitted (enforce itself is untouched, so metallb-system's
# dedicated privileged-namespace labeling in addons.py is still required
# and unaffected).
#
# CONFIRMED BY BREAKING A REAL CONTROL PLANE, TWICE (once via a live
# `talosctl patch mc`, once via this exact --config-patch at gen-config
# time): Talos's config-patch merge is NOT a JSON Merge Patch that
# replaces arrays wholesale -- for the typed `admissionControl` list it
# merges list entries by matching `name` (so this patch's single
# "PodSecurity" entry correctly merges into Talos's own existing one,
# rather than appending a second plugin entry), but for a plain
# string-array field *inside* the opaque, untyped `configuration` blob
# (like `exemptions.namespaces`) it APPENDS the patch's list onto the
# existing one instead of replacing it. Restating
# `exemptions.namespaces: ["kube-system"]` here produced
# `["kube-system", "kube-system"]` on the live node, which the
# PodSecurity admission plugin hard-rejects as a duplicate at apiserver
# startup ("PodSecurity invalid: exemptions.namespaces[1]: Duplicate
# value") -- the apiserver container then exits immediately and never
# comes back (CrashLoopBackOff), taking the whole control plane down
# with it. There is no known way to force a list-replace instead of
# list-append through this patch mechanism, so the only safe fix is to
# never restate a list field that Talos's own default already
# populates -- this patch therefore omits `exemptions` entirely and only
# touches the scalar `defaults.audit`/`defaults.warn` fields.
PODSECURITY_QUIET_WARNINGS_PATCH: dict = {
    "cluster": {
        "apiServer": {
            "admissionControl": [
                {
                    "name": "PodSecurity",
                    "configuration": {
                        "apiVersion": "pod-security.admission.config.k8s.io/v1alpha1",
                        "kind": "PodSecurityConfiguration",
                        "defaults": {
                            "audit": "baseline",
                            "warn": "baseline",
                        },
                    },
                }
            ]
        }
    }
}


def gen_config(
    cluster_name: str,
    cp_endpoint_ip: str,
    output_dir: Path,
    talos_version: str,
    allow_scheduling_on_control_plane: bool = False,
    coredns_image: str | None = None,
) -> None:
    """Writes controlplane.yaml, worker.yaml, talosconfig into output_dir.

    --talos-version pins the generated config schema to the lab's own
    Talos version (not whatever talosctl's own default is) -- without it,
    a talosctl newer than the node's Talos OS emits config keys the node
    doesn't recognize yet (e.g. `grubUseUKICmdline` added well after
    v1.7.x), and apply-config fails with "unknown keys found".

    allow_scheduling_on_control_plane sets cluster.allowSchedulingOnControlPlanes
    via --config-patch-control-plane (a strategic-merge object, not JSON6902 --
    JSON6902 patches aren't supported against this multi-document config
    format and fail at gen-config time). Talos taints control-plane nodes
    NoSchedule by default same as upstream Kubernetes; --single-node labs
    need this patch or the sole node can't run any workloads at all.

    Every lab always disables Talos's built-in Flannel CNI
    (cluster.network.cni.name: "none") via a plain --config-patch, applied
    to BOTH controlplane.yaml and worker.yaml (cluster-wide config has to
    match on every node) -- see addons.yaml's `cni` section for why:
    Flannel has no NetworkPolicy support, so talos-lab always installs
    Cilium via Helm instead, post-bootstrap. coredns_image, if given,
    overrides the image Talos's own (still Talos-managed, not replaced)
    CoreDNS deployment uses -- addons.yaml's `coredns.image` is the only
    thing that sets this.

    Every lab also always applies PODSECURITY_QUIET_WARNINGS_PATCH, lowering
    the PodSecurity admission plugin's warn/audit levels to match its
    enforce level (baseline) -- see that constant's comment for why.
    Enforcement itself is unchanged (still baseline, kube-system still the
    only exemption), so this only silences noisy warnings on pods that
    already pass; it does not relax what's actually admitted.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cluster_patch: dict = {"cluster": {"network": {"cni": {"name": "none"}}}}
    cluster_patch["cluster"]["apiServer"] = PODSECURITY_QUIET_WARNINGS_PATCH["cluster"]["apiServer"]
    if coredns_image:
        cluster_patch["cluster"]["coreDNS"] = {"image": coredns_image}
    args = [
        "gen",
        "config",
        cluster_name,
        f"https://{cp_endpoint_ip}:6443",
        "--output-dir",
        str(output_dir),
        "--talos-version",
        talos_version,
        "--config-patch",
        json.dumps(cluster_patch),
        "--force",
    ]
    if allow_scheduling_on_control_plane:
        args += ["--config-patch-control-plane", SINGLE_NODE_CONTROL_PLANE_PATCH]
    _run(args)


def apply_config(node_ip: str, config_file: Path, talosconfig: Path) -> None:
    _run(
        [
            "apply-config",
            "--insecure",
            "--nodes",
            node_ip,
            "--file",
            str(config_file),
            "--talosconfig",
            str(talosconfig),
        ]
    )


def bootstrap(
    cp_ip: str,
    talosconfig: Path,
    timeout_seconds: int = BOOTSTRAP_TIMEOUT_SECONDS,
) -> None:
    """apply-config reboots the node to lay down its new config to disk, so
    its API port isn't reachable immediately -- retry instead of assuming
    the node from the maintenance-mode apply-config call is still there."""
    args = ["bootstrap", "--nodes", cp_ip, "--endpoints", cp_ip, "--talosconfig", str(talosconfig)]
    deadline = time.monotonic() + timeout_seconds
    last_error: TalosctlError | None = None
    while time.monotonic() < deadline:
        result = subprocess.run(["talosctl", *args], capture_output=True)
        if result.returncode == 0:
            return
        last_error = TalosctlError(args, result.returncode)
        time.sleep(BOOTSTRAP_POLL_INTERVAL_SECONDS)
    raise last_error or TalosctlError(args, 1)


def wait_for_kubeconfig(
    cp_ip: str,
    talosconfig: Path,
    output_path: Path,
    timeout_seconds: int = BOOTSTRAP_TIMEOUT_SECONDS,
) -> None:
    """The API server takes time to come up post-bootstrap; retry until
    `talosctl kubeconfig` succeeds instead of guessing a fixed sleep."""
    deadline = time.monotonic() + timeout_seconds
    last_error: TalosctlError | None = None
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "talosctl",
                "kubeconfig",
                str(output_path),
                "--nodes",
                cp_ip,
                "--endpoints",
                cp_ip,
                "--talosconfig",
                str(talosconfig),
                "--force",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        last_error = TalosctlError(["kubeconfig"], result.returncode)
        time.sleep(BOOTSTRAP_POLL_INTERVAL_SECONDS)
    raise last_error or TalosctlError(["kubeconfig"], 1)
