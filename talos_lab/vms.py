"""Power lifecycle for a lab's VMs via `virsh`. This only starts/stops the
libvirt domains that already exist -- it never touches Terraform state,
Talos config, or the kube context. Domain names mirror what main.tf.j2
assigns (`<lab>-controlplane`, `<lab>-worker-<i>`).
"""

from __future__ import annotations

import subprocess
import time

from talos_lab.exceptions import VirshError

SHUTDOWN_POLL_INTERVAL_SECONDS = 3
SHUTDOWN_TIMEOUT_SECONDS = 60


def domain_names(lab_name: str, worker_count: int) -> list[str]:
    return [f"{lab_name}-controlplane"] + [
        f"{lab_name}-worker-{i}" for i in range(worker_count)
    ]


def domain_state(domain: str) -> str:
    result = subprocess.run(["virsh", "domstate", domain], capture_output=True, text=True)
    if result.returncode != 0:
        raise VirshError(["domstate", domain], result.returncode, result.stderr.strip())
    return result.stdout.strip().lower()


def domain_state_or_none(domain: str) -> str | None:
    """Like domain_state(), but for `talos-lab status` -- callers there
    need to distinguish "not created yet" (a normal, expected state at
    early bootstrap stages) from an actual virsh error, without raising.
    """
    try:
        return domain_state(domain)
    except VirshError:
        return None


def start_domain(domain: str) -> None:
    if domain_state(domain) == "running":
        return  # already up -- idempotent no-op
    result = subprocess.run(["virsh", "start", domain], capture_output=True, text=True)
    if result.returncode != 0:
        raise VirshError(["start", domain], result.returncode, result.stderr.strip())


def stop_domain(domain: str, force: bool = False) -> None:
    if domain_state(domain) == "shut off":
        return  # already down -- idempotent no-op

    if force:
        result = subprocess.run(["virsh", "destroy", domain], capture_output=True, text=True)
        if result.returncode != 0:
            raise VirshError(["destroy", domain], result.returncode, result.stderr.strip())
        return

    result = subprocess.run(["virsh", "shutdown", domain], capture_output=True, text=True)
    if result.returncode != 0:
        raise VirshError(["shutdown", domain], result.returncode, result.stderr.strip())

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if domain_state(domain) == "shut off":
            return
        time.sleep(SHUTDOWN_POLL_INTERVAL_SECONDS)
    raise VirshError(
        ["shutdown", domain],
        1,
        f"'{domain}' did not shut down within {SHUTDOWN_TIMEOUT_SECONDS}s; retry with --force",
    )
