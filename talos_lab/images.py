"""Talos disk image management.

`download_image` fetches the nocloud raw disk image straight from the
siderolabs/talos GitHub release assets, decompresses it, and converts it
to qcow2 -- shelling out to curl/xz/qemu-img rather than reimplementing
any of that. Asset naming has held steady across recent Talos releases,
but if a future release renames it, `download_image` will fail with the
URL it tried, which is enough to go fix by hand (see README section 3
for the manual fallback).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from talos_lab import paths
from talos_lab.exceptions import ImageDownloadError, ImageNotFoundError

GITHUB_RELEASE_ASSET_URL = (
    "https://github.com/siderolabs/talos/releases/download/{version}/nocloud-amd64.raw.xz"
)

REQUIRED_TOOLS = ("curl", "xz", "qemu-img")


def normalize_version(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def image_path(talos_version: str) -> Path:
    return paths.IMAGES_DIR / f"talos-{talos_version}-nocloud-amd64.qcow2"


def ensure_image(talos_version: str) -> Path:
    path = image_path(talos_version)
    if path.exists():
        return path

    raise ImageNotFoundError(
        f"Talos image for {talos_version} not found at {path}.\n"
        f"Run `talos-lab get {talos_version}` to fetch it, then re-run `talos-lab create`."
    )


def download_image(talos_version: str) -> Path:
    """Downloads, decompresses, and converts the Talos nocloud disk image
    for `talos_version`, replacing any existing image for that version.
    Caller is responsible for confirming any overwrite before calling this.
    """
    missing_tools = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing_tools:
        raise ImageDownloadError(
            f"missing required tool(s) on PATH: {', '.join(missing_tools)}"
        )

    paths.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    target = image_path(talos_version)
    url = GITHUB_RELEASE_ASSET_URL.format(version=talos_version)

    with tempfile.TemporaryDirectory(dir=paths.IMAGES_DIR) as tmp:
        tmp_dir = Path(tmp)
        raw_xz = tmp_dir / "nocloud-amd64.raw.xz"
        raw = tmp_dir / "nocloud-amd64.raw"
        qcow2_tmp = tmp_dir / "image.qcow2"

        result = subprocess.run(["curl", "-fL", "--progress-bar", "-o", str(raw_xz), url])
        if result.returncode != 0:
            raise ImageDownloadError(
                f"failed to download {url} (exit {result.returncode}). "
                "Check the version exists at "
                f"https://github.com/siderolabs/talos/releases/tag/{talos_version}"
            )

        result = subprocess.run(["xz", "-d", str(raw_xz)])
        if result.returncode != 0:
            raise ImageDownloadError(f"failed to decompress downloaded image (exit {result.returncode})")

        result = subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2_tmp)])
        if result.returncode != 0:
            raise ImageDownloadError(f"failed to convert image to qcow2 (exit {result.returncode})")

        os.replace(qcow2_tmp, target)

    return target
