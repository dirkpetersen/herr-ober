#!/usr/bin/env python3
"""Ober upgrade command - check and install updates."""

import json
import subprocess
from typing import Any

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console
from rich.table import Table

from ober.config import OberConfig
from ober.system import (
    OSFamily,
    SystemInfo,
    get_exabgp_version,
    get_haproxy_version,
    run_command,
)

console = Console()


@click.command()
@click.option(
    "--check-only",
    is_flag=True,
    help="Only check for updates, don't install.",
)
@click.pass_context
def upgrade(ctx: click.Context, check_only: bool) -> None:
    """Check for and install component updates.

    Checks for available updates to HAProxy and ExaBGP.
    Shows what would be updated and requires confirmation before installing.
    """
    parent_ctx = ctx.obj
    json_output = parent_ctx.json_output if parent_ctx else False
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not check_only and not system.is_root:
        console.print("[red]Error:[/red] Upgrade requires root access.")
        console.print("Run with: sudo ober upgrade")
        console.print("Or use --check-only to just check for updates")
        ctx.exit(1)

    config = OberConfig.load()

    console.print("Checking for updates...")
    console.print()

    updates: dict[str, Any] = {
        "haproxy": _check_haproxy_update(system),
        "exabgp": _check_exabgp_update(config),
    }

    if json_output:
        click.echo(json.dumps(updates, indent=2))
        return

    # Display update information
    table = Table(title="Available Updates", show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Current")
    table.add_column("Available")
    table.add_column("Status")

    has_updates = False

    for component, info in updates.items():
        current = info.get("current", "not installed")
        available = info.get("available", "unknown")
        update_available = info.get("update_available", False)

        if update_available:
            status = "[green]Update available[/green]"
            has_updates = True
        elif current == "not installed":
            status = "[yellow]Not installed[/yellow]"
        else:
            status = "[dim]Up to date[/dim]"

        table.add_row(component, current or "-", available or "-", status)

    console.print(table)
    console.print()

    if check_only:
        if has_updates:
            console.print("Run 'sudo ober upgrade' to install updates")
        return

    if not has_updates:
        console.print("[green]All components are up to date![/green]")
        return

    # Confirm upgrade
    confirm = inquirer.confirm(
        "Install available updates?",
        default=True,
    )

    if not confirm:
        console.print("[yellow]Upgrade cancelled.[/yellow]")
        return

    # Perform upgrades
    console.print()
    console.print("Installing updates...")

    if updates["haproxy"].get("update_available"):
        console.print("Upgrading HAProxy...")
        _upgrade_haproxy(system)

    if updates["exabgp"].get("update_available"):
        console.print("Upgrading ExaBGP...")
        _upgrade_exabgp(config)

    console.print()
    console.print("[bold green]Upgrade complete![/bold green]")
    console.print()
    console.print("Note: Services may need to be restarted.")
    console.print("Run 'ober restart' to apply changes.")


def _check_haproxy_update(system: SystemInfo) -> dict[str, Any]:
    """Check for HAProxy updates."""
    current = get_haproxy_version()

    result: dict[str, Any] = {
        "current": current,
        "available": None,
        "update_available": False,
    }

    if system.os_family == OSFamily.DEBIAN:
        # Check apt for available version
        try:
            output = subprocess.run(
                ["apt-cache", "policy", "haproxy"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if output.returncode == 0:
                for line in output.stdout.split("\n"):
                    if "Candidate:" in line:
                        available = line.split("Candidate:")[1].strip()
                        # Extract just the version number
                        if ":" in available:
                            available = available.split(":")[1]
                        available = available.split("-")[0]
                        result["available"] = available

                        if current and available and available != current:
                            result["update_available"] = True
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    elif system.os_family == OSFamily.RHEL:
        # Check dnf for available version
        try:
            output = subprocess.run(
                ["dnf", "info", "haproxy"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if output.returncode == 0:
                for line in output.stdout.split("\n"):
                    if line.startswith("Version"):
                        available = line.split(":")[1].strip()
                        result["available"] = available
                        if current and available and available != current:
                            result["update_available"] = True
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return result


def _check_exabgp_update(config: OberConfig) -> dict[str, Any]:
    """Check for ExaBGP updates."""
    current = get_exabgp_version()

    result: dict[str, Any] = {
        "current": current,
        "available": None,
        "update_available": False,
    }

    # Check PyPI for latest version
    pip_path = config.venv_path / "bin" / "pip"
    if not pip_path.exists():
        return result

    try:
        output = subprocess.run(
            [str(pip_path), "index", "versions", "exabgp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if output.returncode == 0:
            # Parse output for available versions
            # Format: "exabgp (X.Y.Z)"
            for line in output.stdout.split("\n"):
                if "Available versions:" in line:
                    versions = line.split(":")[1].strip()
                    if versions:
                        latest = versions.split(",")[0].strip()
                        result["available"] = latest
                        if current and latest and latest != current:
                            result["update_available"] = True
                    break
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: try pip list --outdated
    if result["available"] is None:
        try:
            output = subprocess.run(
                [str(pip_path), "list", "--outdated", "--format=json"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if output.returncode == 0:
                packages = json.loads(output.stdout)
                for pkg in packages:
                    if pkg["name"].lower() == "exabgp":
                        result["available"] = pkg["latest_version"]
                        result["update_available"] = True
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

    return result


def _upgrade_haproxy(system: SystemInfo) -> None:
    """Upgrade HAProxy using the system package manager."""
    if system.os_family == OSFamily.DEBIAN:
        run_command(["apt-get", "update"], check=False)
        run_command(["apt-get", "install", "-y", "haproxy"])
        console.print("[green]HAProxy upgraded[/green]")

    elif system.os_family == OSFamily.RHEL:
        run_command(["dnf", "upgrade", "-y", "haproxy"])
        console.print("[green]HAProxy upgraded[/green]")


def _upgrade_exabgp(config: OberConfig) -> None:
    """Upgrade ExaBGP in the virtual environment."""
    pip_path = config.venv_path / "bin" / "pip"
    if pip_path.exists():
        run_command([str(pip_path), "install", "--upgrade", "exabgp"])
        console.print("[green]ExaBGP upgraded[/green]")
    else:
        console.print("[yellow]ExaBGP venv not found, skipping[/yellow]")
