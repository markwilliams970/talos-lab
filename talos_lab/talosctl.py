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


def gen_config(cluster_name: str, cp_endpoint_ip: str, output_dir: Path) -> None:
    """Writes controlplane.yaml, worker.yaml, talosconfig into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "gen",
            "config",
            cluster_name,
            f"https://{cp_endpoint_ip}:6443",
            "--output-dir",
            str(output_dir),
        ]
    )


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


def bootstrap(cp_ip: str, talosconfig: Path) -> None:
    _run(
        [
            "bootstrap",
            "--nodes",
            cp_ip,
            "--endpoints",
            cp_ip,
            "--talosconfig",
            str(talosconfig),
        ]
    )


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
