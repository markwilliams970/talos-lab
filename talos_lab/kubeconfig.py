"""Merges each lab's kubeconfig into the single global ~/.kube/config
and manages the talos-lab-<name> context, using `kubectl config` for
every mutation so we never hand-roll kubeconfig YAML merge semantics.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from talos_lab import paths
from talos_lab.exceptions import ClusterNotReadyError

CONTEXT_PREFIX = "talos-lab-"

NODE_READY_POLL_INTERVAL_SECONDS = 5
NODE_READY_TIMEOUT_SECONDS = 180


def context_name(lab_name: str) -> str:
    return f"{CONTEXT_PREFIX}{lab_name}"


def _rename_kubeconfig_entries(raw_kubeconfig_path: Path, lab_name: str) -> Path:
    """talosctl names cluster/user/context after the Talos cluster name,
    which can collide across labs. Rename all three to talos-lab-<name>
    before merging so each lab gets a stable, unique context."""
    with open(raw_kubeconfig_path) as f:
        kc = yaml.safe_load(f)

    name = context_name(lab_name)
    for cluster in kc.get("clusters", []):
        cluster["name"] = name
    for user in kc.get("users", []):
        user["name"] = name
    for ctx in kc.get("contexts", []):
        ctx["name"] = name
        ctx["context"]["cluster"] = name
        ctx["context"]["user"] = name
    kc["current-context"] = name

    renamed_path = raw_kubeconfig_path.with_suffix(".renamed.yaml")
    with open(renamed_path, "w") as f:
        yaml.safe_dump(kc, f)
    return renamed_path


def merge_into_global(raw_kubeconfig_path: Path, lab_name: str) -> None:
    renamed = _rename_kubeconfig_entries(raw_kubeconfig_path, lab_name)
    paths.KUBE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.KUBE_CONFIG.touch(exist_ok=True)

    merged_env = dict(os.environ)
    merged_env["KUBECONFIG"] = f"{paths.KUBE_CONFIG}:{renamed}"

    fd, tmp_path = tempfile.mkstemp(prefix=".kubeconfig-merge-", suffix=".yaml")
    os.close(fd)
    try:
        subprocess.run(
            ["kubectl", "config", "view", "--flatten"],
            env=merged_env,
            stdout=open(tmp_path, "w"),
            check=True,
        )
        os.replace(tmp_path, paths.KUBE_CONFIG)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    subprocess.run(
        ["kubectl", "config", "use-context", context_name(lab_name)],
        check=True,
    )


def _node_is_ready(node: dict) -> bool:
    conditions = node.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def wait_for_nodes_ready(
    kubeconfig_path: Path,
    expected_count: int,
    timeout_seconds: int = NODE_READY_TIMEOUT_SECONDS,
) -> None:
    """Kubeconfig being fetchable only proves the API server answered once --
    it says nothing about whether every node has actually joined and gone
    Ready yet. Poll `get nodes` against the lab's own kubeconfig (not the
    merged global one, so this doesn't depend on the context switch having
    happened yet) until `expected_count` nodes all report Ready.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "nodes", "-o", "json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            try:
                nodes = json.loads(result.stdout)["items"]
            except (json.JSONDecodeError, KeyError):
                nodes = []
            if len(nodes) >= expected_count and all(_node_is_ready(n) for n in nodes):
                return
        time.sleep(NODE_READY_POLL_INTERVAL_SECONDS)
    raise ClusterNotReadyError(expected_count, timeout_seconds)


def label_worker_nodes(kubeconfig_path: Path) -> None:
    """Talos only auto-labels control-plane nodes with
    node-role.kubernetes.io/control-plane -- workers get no role label at
    all, which is why `kubectl get nodes` shows ROLES=<none> for them.
    Label everything that ISN'T control-plane as worker instead of trying
    to enumerate worker node names ourselves (Talos generates the
    Kubernetes node name, e.g. "talos-9i7-qu3" -- not something talos-lab
    controls or needs to track just for this).
    """
    subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "label",
            "nodes",
            "-l",
            "!node-role.kubernetes.io/control-plane",
            "node-role.kubernetes.io/worker=",
            "--overwrite",
        ],
        check=True,
    )


def label_single_node_as_worker(kubeconfig_path: Path) -> None:
    """--single-node labs have exactly one node serving both roles. It
    already has Talos's auto-applied node-role.kubernetes.io/control-plane
    label; add the worker label too (not exclusive of it, unlike
    label_worker_nodes' selector above) so `kubectl get nodes` ROLES shows
    "control-plane,worker" instead of just "control-plane".
    """
    subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "label",
            "nodes",
            "--all",
            "node-role.kubernetes.io/worker=",
            "--overwrite",
        ],
        check=True,
    )


def use_context(lab_name: str) -> None:
    subprocess.run(
        ["kubectl", "config", "use-context", context_name(lab_name)],
        check=True,
    )


def current_context() -> str | None:
    result = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def remove_context(lab_name: str) -> None:
    name = context_name(lab_name)
    was_current = current_context() == name
    for args in (
        ["config", "delete-context", name],
        ["config", "delete-cluster", name],
        ["config", "unset", f"users.{name}"],
    ):
        subprocess.run(["kubectl", *args], capture_output=True)

    if was_current:
        subprocess.run(["kubectl", "config", "unset", "current-context"], capture_output=True)
