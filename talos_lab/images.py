"""Talos disk image management.

`download_image` fetches the metal raw disk image straight from the
siderolabs/talos GitHub release assets, decompresses it, and converts it
to qcow2 -- shelling out to curl/zstd/xz/qemu-img rather than
reimplementing any of that.

Asset naming is NOT stable across Talos releases: older releases (e.g.
v1.7.x) published a `nocloud-amd64.raw.xz` asset that newer releases have
dropped entirely, keeping only `metal-amd64.raw.<ext>` (which does exist
across both old and new releases we've checked -- v1.7.6 and v1.13.5).
Compression format also changed from xz to zstd along the way. So we
target "metal" and just try both compression extensions, taking whichever
one the release actually has. If a future release changes naming again,
this will fail with the URLs it tried, which is enough to go fix by hand
(see README section 3 for the manual fallback).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from talos_lab import paths
from talos_lab.exceptions import ImageDownloadError, ImageNotFoundError

ASSET_URL_TEMPLATE = "https://github.com/siderolabs/talos/releases/download/{version}/metal-amd64.raw.{ext}"

# (file extension, decompression tool) -- tried in this order.
COMPRESSION_FORMATS = (("zst", "zstd"), ("xz", "xz"))

REQUIRED_TOOLS = ("curl", "zstd", "xz", "qemu-img")


def normalize_version(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def image_path(talos_version: str) -> Path:
    return paths.IMAGES_DIR / f"talos-{talos_version}.qcow2"


def ensure_image(talos_version: str) -> Path:
    path = image_path(talos_version)
    if path.exists():
        return path

    raise ImageNotFoundError(
        f"Talos image for {talos_version} not found at {path}.\n"
        f"Run `taloslab get {talos_version}` to fetch it, then re-run `taloslab create`."
    )


def download_image(talos_version: str) -> Path:
    """Downloads, decompresses, and converts the Talos metal disk image
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

    with tempfile.TemporaryDirectory(dir=paths.IMAGES_DIR) as tmp:
        tmp_dir = Path(tmp)
        qcow2_tmp = tmp_dir / "image.qcow2"

        compressed = None
        decompress_tool = None
        tried_urls = []
        for ext, tool in COMPRESSION_FORMATS:
            url = ASSET_URL_TEMPLATE.format(version=talos_version, ext=ext)
            tried_urls.append(url)
            candidate = tmp_dir / f"metal-amd64.raw.{ext}"
            result = subprocess.run(["curl", "-fL", "--progress-bar", "-o", str(candidate), url])
            if result.returncode == 0:
                compressed = candidate
                decompress_tool = tool
                break

        if compressed is None:
            raise ImageDownloadError(
                f"failed to download a Talos image for {talos_version} -- tried:\n"
                + "\n".join(f"  {u}" for u in tried_urls)
                + f"\nCheck the release assets at https://github.com/siderolabs/talos/releases/tag/{talos_version}"
            )

        result = subprocess.run([decompress_tool, "-d", str(compressed)])
        if result.returncode != 0:
            raise ImageDownloadError(f"failed to decompress downloaded image (exit {result.returncode})")
        raw = compressed.with_suffix("")  # strips the .zst/.xz we just decompressed

        result = subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2_tmp)])
        if result.returncode != 0:
            raise ImageDownloadError(f"failed to convert image to qcow2 (exit {result.returncode})")

        os.replace(qcow2_tmp, target)

    return target


def import_image(source: Path, talos_version: str) -> Path:
    """Copies a manually-downloaded disk image (e.g. from
    https://factory.talos.dev) from `source` into the local image store
    for `talos_version`, replacing any existing image for that version.
    Caller is responsible for confirming any overwrite before calling this.
    """
    if not source.is_file():
        raise ImageDownloadError(f"no such file: {source}")

    if shutil.which("qemu-img") is None:
        raise ImageDownloadError("missing required tool on PATH: qemu-img")

    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ImageDownloadError(
            f"qemu-img couldn't read {source} (exit {result.returncode}): {result.stderr.strip()}"
        )
    actual_format = json.loads(result.stdout).get("format")
    if actual_format != "qcow2":
        raise ImageDownloadError(
            f"{source} is format '{actual_format}', not qcow2 -- convert it first, e.g.:\n"
            f"  qemu-img convert -O qcow2 {source} {source.with_suffix('.qcow2')}"
        )

    paths.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    target = image_path(talos_version)

    with tempfile.TemporaryDirectory(dir=paths.IMAGES_DIR) as tmp:
        qcow2_tmp = Path(tmp) / "image.qcow2"
        shutil.copyfile(source, qcow2_tmp)
        os.replace(qcow2_tmp, target)

    return target
