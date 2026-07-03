"""Single source of truth for every path under ~/.talos-lab."""

from pathlib import Path

ROOT = Path.home() / ".talos-lab"

REGISTRY_FILE = ROOT / "registry.json"
VERSION_FILE = ROOT / "version.json"
TEMPLATES_DIR = ROOT / "templates"
VM_PROFILES_FILE = TEMPLATES_DIR / "vm-profiles.yaml"
IMAGES_DIR = ROOT / "images"

KUBE_CONFIG = Path.home() / ".kube" / "config"


def lab_dir(name: str) -> Path:
    return ROOT / name


def lab_state_file(name: str) -> Path:
    return lab_dir(name) / "state.json"


def lab_config_file(name: str) -> Path:
    return lab_dir(name) / "config.yaml"


def lab_tofu_dir(name: str) -> Path:
    return lab_dir(name) / "tofu"


def lab_talos_dir(name: str) -> Path:
    return lab_dir(name) / "talos"


def lab_network_dir(name: str) -> Path:
    return lab_dir(name) / "network"


def lab_kubeconfig_file(name: str) -> Path:
    return lab_dir(name) / "kubeconfig"


def lab_talosconfig_file(name: str) -> Path:
    return lab_talos_dir(name) / "talosconfig"


def lab_inventory_file(name: str) -> Path:
    return lab_dir(name) / "inventory.json"


def ensure_root_dirs() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def ensure_lab_dirs(name: str) -> None:
    for d in (
        lab_dir(name),
        lab_tofu_dir(name),
        lab_talos_dir(name),
        lab_network_dir(name),
    ):
        d.mkdir(parents=True, exist_ok=True)
