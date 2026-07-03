"""Talos disk image management.

STUB: Talos release asset names/paths change between versions and I
won't hardcode a URL I can't verify against the actual release for an
arbitrary version. Wire this up to whatever image source you use
(Talos Image Factory, a local mirror, a Packer pipeline, ...) before
running `create` for real -- see the raised NotImplementedError below.
"""

from __future__ import annotations

from pathlib import Path

from talos_lab import paths
from talos_lab.exceptions import ImageNotFoundError


def image_path(talos_version: str) -> Path:
    return paths.IMAGES_DIR / f"talos-{talos_version}-nocloud-amd64.qcow2"


def ensure_image(talos_version: str) -> Path:
    path = image_path(talos_version)
    if path.exists():
        return path

    raise ImageNotFoundError(
        f"Talos image for {talos_version} not found at {path}.\n"
        "Download the nocloud qcow2 image for this version (e.g. from the "
        "Talos Image Factory or the siderolabs/talos release assets) and "
        f"place it at {path}, then re-run `talos-lab create`."
    )
