"""Talos bootstrap orchestration: gen config -> apply-config -> bootstrap
-> kubeconfig retrieval. Every step is a plain `talosctl` subprocess call;
this module owns none of the Talos machine-config semantics itself.
"""

from __future__ import annotations

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


def gen_config(
    cluster_name: str,
    cp_endpoint_ip: str,
    output_dir: Path,
    talos_version: str,
    allow_scheduling_on_control_plane: bool = False,
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
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "gen",
        "config",
        cluster_name,
        f"https://{cp_endpoint_ip}:6443",
        "--output-dir",
        str(output_dir),
        "--talos-version",
        talos_version,
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
