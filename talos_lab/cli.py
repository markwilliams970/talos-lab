"""Thin Typer CLI. No orchestration logic lives here -- every command
just parses args, calls into commands.py, and lets TalosLabError
subclasses turn into clean exit(1) messages."""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from talos_lab import commands
from talos_lab.exceptions import TalosLabError

app = typer.Typer(
    name="talos-lab",
    help="Thin orchestration CLI over OpenTofu + libvirt + Talos Linux",
    pretty_exceptions_enable=False,
)
version_app = typer.Typer(help="Manage the global Talos version pin")
app.add_typer(version_app, name="version")

console = Console()


@app.command()
def create(
    name: str = typer.Argument(..., help="Lab name"),
    worker_count: int = typer.Argument(..., help="Number of worker nodes"),
    control_plane_profile: str = typer.Option("medium", "--cp-profile", help="VM profile for the control plane"),
    worker_profile: str = typer.Option("medium", "--worker-profile", help="VM profile for workers"),
) -> None:
    """Create (or resume) a lab: VMs, network, Talos bootstrap, kubeconfig."""
    commands.create_lab(name, worker_count, control_plane_profile, worker_profile)


@app.command(name="list")
def list_cmd() -> None:
    """List all labs and mark the active kube context."""
    commands.list_labs()


@app.command()
def use(name: str = typer.Argument(..., help="Lab name")) -> None:
    """Switch kubectl context to this lab."""
    commands.use_lab(name)


@app.command()
def start(name: str = typer.Argument(..., help="Lab name")) -> None:
    """Power on a lab's VMs (control plane + workers)."""
    commands.start_lab(name)


@app.command()
def stop(
    name: str = typer.Argument(..., help="Lab name"),
    force: bool = typer.Option(False, "--force", help="Hard power-off (virsh destroy) instead of a graceful shutdown"),
) -> None:
    """Power off a lab's VMs (control plane + workers)."""
    commands.stop_lab(name, force=force)


@app.command()
def delete(name: str = typer.Argument(..., help="Lab name")) -> None:
    """Tear down a lab's VMs, network, state, and kube context."""
    commands.delete_lab(name)


@version_app.command("set")
def version_set(talos_version: str = typer.Argument(..., help="e.g. v1.7.6")) -> None:
    commands.version_set(talos_version)


@version_app.command("show")
def version_show() -> None:
    commands.version_show()


@app.command()
def get(
    version: str = typer.Argument(
        None, help="Talos version to fetch, e.g. 1.8.0 or v1.8.0. Defaults to the pinned version."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Overwrite an existing image without prompting"),
) -> None:
    """Fetch the Talos golden image for a version into ~/.talos-lab/images."""
    commands.get_image(version, assume_yes=yes)


def main() -> None:
    try:
        app()
    except TalosLabError as e:
        console.print(f"[red]error:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
