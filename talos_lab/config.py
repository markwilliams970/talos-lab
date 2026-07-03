"""Global Talos version pin + VM profile templates.

Both are seeded into ~/.talos-lab on first use from the defaults
shipped inside the package, then owned by the user.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from talos_lab import paths

# Last-resort fallback only, used if talosctl isn't on PATH yet at seed
# time. Prefer _detect_talosctl_version() below -- a hardcoded version
# here inevitably goes stale and, worse, can silently mismatch whatever
# talosctl the user actually has installed. That skew is not cosmetic:
# a talosctl far newer than the pinned Talos OS version generates config
# fields the node's OS doesn't recognize (apply-config fails outright)
# and, more insidiously, can generate cluster PKI/bootstrap material the
# node's older components reject, which surfaces later as a cascade of
# "Unauthorized" errors between kubelet/apiserver/scheduler -- looking
# nothing like a version problem unless you already know to suspect one.
FALLBACK_TALOS_VERSION = "v1.7.6"

_PACKAGE_DEFAULT_PROFILES = Path(__file__).parent / "templates" / "vm-profiles.yaml"


def _detect_talosctl_version() -> str | None:
    try:
        result = subprocess.run(
            ["talosctl", "version", "--client"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"Tag:\s*(v\S+)", result.stdout)
    return match.group(1) if match else None


def _default_talos_version() -> str:
    return _detect_talosctl_version() or FALLBACK_TALOS_VERSION


def _seed_defaults() -> None:
    paths.ensure_root_dirs()
    if not paths.VERSION_FILE.exists():
        set_talos_version(_default_talos_version())
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
