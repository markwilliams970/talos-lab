"""Merges each lab's kubeconfig into the single global ~/.kube/config
and manages the talos-lab-<name> context, using `kubectl config` for
every mutation so we never hand-roll kubeconfig YAML merge semantics.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from talos_lab import paths

CONTEXT_PREFIX = "talos-lab-"


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
