"""registry.json (all labs) + per-lab state.json (bootstrap progress).

All writes are atomic (write to a tempfile in the same directory, then
os.replace) so a crash mid-write never corrupts the file a running
lab's resumability depends on.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from talos_lab import paths
from talos_lab.exceptions import LabExistsError, LabNotFoundError

DEFAULT_LAB_STATE = {
    "tofu_state_done": False,
    "config_applied": False,
    "talos_bootstrapped": False,
    "cni_installed": False,
    "kubeconfig_ready": False,
    "addons_installed": False,
    "control_plane_ip": None,
    "worker_ips": [],
}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    with open(path) as f:
        return json.load(f)


# ---- registry (all labs) -----------------------------------------------

def load_registry() -> dict[str, Any]:
    return _read_json(paths.REGISTRY_FILE, {"labs": {}})


def save_registry(registry: dict[str, Any]) -> None:
    _atomic_write_json(paths.REGISTRY_FILE, registry)


def register_lab(name: str, meta: dict[str, Any]) -> None:
    registry = load_registry()
    if name in registry["labs"]:
        raise LabExistsError(name)
    registry["labs"][name] = meta
    save_registry(registry)


def unregister_lab(name: str) -> None:
    registry = load_registry()
    registry["labs"].pop(name, None)
    save_registry(registry)


def get_lab_meta(name: str) -> dict[str, Any]:
    registry = load_registry()
    if name not in registry["labs"]:
        raise LabNotFoundError(name)
    return registry["labs"][name]


def lab_exists(name: str) -> bool:
    return name in load_registry()["labs"]


def used_network_indices() -> set[int]:
    registry = load_registry()
    return {
        meta["network_index"]
        for meta in registry["labs"].values()
        if "network_index" in meta
    }


# ---- per-lab bootstrap state --------------------------------------------

def load_lab_state(name: str) -> dict[str, Any]:
    # Merge over defaults (not just fall back to them) so a lab created
    # before a new state flag was added doesn't KeyError on it -- it
    # picks up the flag's default instead of needing a migration.
    raw = _read_json(paths.lab_state_file(name), DEFAULT_LAB_STATE)
    merged = {**DEFAULT_LAB_STATE, **raw}
    # cni_installed is the one flag that can't just default to False on a
    # lab that predates it: such a lab's cluster is already bootstrapped
    # and live with whatever CNI Talos originally applied (Flannel,
    # before talos-lab always disabled it) -- swapping CNI on an
    # already-running cluster isn't a safe "just resume" step the way
    # every other flag here is, it would fight with the live Flannel
    # setup instead of filling in a gap. Grandfather such labs in as
    # already-settled instead of trying to install Cilium alongside it.
    if "cni_installed" not in raw and raw.get("talos_bootstrapped"):
        merged["cni_installed"] = True
    return merged


def save_lab_state(name: str, state: dict[str, Any]) -> None:
    _atomic_write_json(paths.lab_state_file(name), state)


def update_lab_state(name: str, **kwargs: Any) -> dict[str, Any]:
    state = load_lab_state(name)
    state.update(kwargs)
    save_lab_state(name, state)
    return state
