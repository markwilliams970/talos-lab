"""Global Talos version pin + VM profile templates.

Both are seeded into ~/.talos-lab on first use from the defaults
shipped inside the package, then owned by the user.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from talos_lab import paths

DEFAULT_TALOS_VERSION = "v1.7.6"

_PACKAGE_DEFAULT_PROFILES = Path(__file__).parent / "templates" / "vm-profiles.yaml"


def _seed_defaults() -> None:
    paths.ensure_root_dirs()
    if not paths.VERSION_FILE.exists():
        set_talos_version(DEFAULT_TALOS_VERSION)
    if not paths.VM_PROFILES_FILE.exists():
        shutil.copyfile(_PACKAGE_DEFAULT_PROFILES, paths.VM_PROFILES_FILE)


def get_talos_version() -> str:
    _seed_defaults()
    with open(paths.VERSION_FILE) as f:
        return json.load(f)["talos_version"]


def set_talos_version(version: str) -> None:
    paths.ensure_root_dirs()
    with open(paths.VERSION_FILE, "w") as f:
        json.dump({"talos_version": version}, f, indent=2)
        f.write("\n")


def load_vm_profiles() -> dict[str, dict[str, Any]]:
    _seed_defaults()
    with open(paths.VM_PROFILES_FILE) as f:
        return yaml.safe_load(f)


def get_vm_profile(name: str) -> dict[str, Any]:
    profiles = load_vm_profiles()
    if name not in profiles:
        raise ValueError(
            f"unknown VM profile '{name}', available: {', '.join(sorted(profiles))}"
        )
    return profiles[name]
