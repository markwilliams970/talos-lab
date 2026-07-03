"""OpenTofu is the infrastructure engine. This module never touches
libvirt or VM state itself -- it renders .tf files from templates and
shells out to the `tofu` binary. All infra logic lives in the
rendered .tf files, not here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from talos_lab.exceptions import TofuError

TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_tofu_files(tofu_dir: Path, context: dict[str, Any]) -> None:
    tofu_dir.mkdir(parents=True, exist_ok=True)
    for template_name, out_name in (
        ("main.tf.j2", "main.tf"),
        ("variables.tf.j2", "variables.tf"),
        ("outputs.tf.j2", "outputs.tf"),
    ):
        rendered = _env.get_template(template_name).render(**context)
        (tofu_dir / out_name).write_text(rendered)


def render_network_xml(network_dir: Path, context: dict[str, Any]) -> None:
    """Renders network/libvirt-network.xml as a human-readable record.

    Not applied by tofu directly -- actual network creation goes
    through the libvirt_network resource in main.tf. This file exists
    purely so the directory layout matches ~/.talos-lab/<lab>/network/
    and is inspectable/portable outside of tofu state.
    """
    network_dir.mkdir(parents=True, exist_ok=True)
    rendered = _env.get_template("network.xml.j2").render(**context)
    (network_dir / "libvirt-network.xml").write_text(rendered)


def _run(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    result = subprocess.run(["tofu", *args], cwd=cwd, env=env)
    if result.returncode != 0:
        raise TofuError(args, result.returncode)
    return result


def init(tofu_dir: Path) -> None:
    _run(["init", "-input=false"], cwd=tofu_dir)


def apply(tofu_dir: Path, talos_image_path: str) -> None:
    _run(
        ["apply", "-auto-approve", "-input=false"],
        cwd=tofu_dir,
        extra_env={"TF_VAR_talos_image_path": talos_image_path},
    )


def destroy(tofu_dir: Path, talos_image_path: str) -> None:
    _run(
        ["destroy", "-auto-approve", "-input=false"],
        cwd=tofu_dir,
        extra_env={"TF_VAR_talos_image_path": talos_image_path},
    )


def output(tofu_dir: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["tofu", "output", "-json"],
        cwd=tofu_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(result.stdout)
    return {key: value["value"] for key, value in raw.items()}
