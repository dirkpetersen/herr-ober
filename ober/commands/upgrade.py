#!/usr/bin/env python3
"""Ober upgrade command - check and install updates."""

import json
import subprocess
from typing import Any

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console
from rich.table import Table

from ober.commands.bootstrap import (
    EXABGP_SPEC,
    _allow_haproxy_backend_ports,
    _create_systemd_services,
)
from ober.config import OberConfig
from ober.system import (
    OSFamily,
    ServiceInfo,
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

    if config.config_path.exists():
        _rerender_units(config)

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
    if updates["haproxy"].get("update_available"):
        # The master process re-executes the new binary on reload
        console.print("Run 'sudo ober restart' to load the new HAProxy (zero-downtime reload).")
    if updates["exabgp"].get("update_available"):
        console.print("Run 'sudo ober restart --hard' to load the new ExaBGP.")


def _rerender_units(config: OberConfig) -> None:
    """Rewrite the systemd units from the current templates (nodes bootstrapped earlier)."""
    config.ensure_directories()  # older installs lack var/run, which the units now list
    changed = _create_systemd_services(config, config.venv_path)
    _allow_haproxy_backend_ports()  # the new ober-http unit runs HAProxy as haproxy_t
    unit = "ober-bgp" if config.ha_mode == "bgp" else "ober-ha"
    if ServiceInfo.from_service_name(unit).is_enabled:
        # Adds the ober-http.service.wants link of a new [Install] section
        run_command(["systemctl", "enable", unit], check=False)
    if changed:
        console.print(f"[yellow]Updated systemd units:[/yellow] {', '.join(changed)}")
        if "ober-http.service" in changed:
            console.print(
                "Run 'sudo ober restart --hard' once, in a maintenance window, "
                "so HAProxy runs with the new unit."
            )


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

    pip = _venv_pip(config)
    if pip is None:
        return result

    try:
        output = subprocess.run(
            [*pip, "index", "versions", "exabgp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if output.returncode == 0:
            # Format: "Available versions: 5.0.15, ..., 4.2.25, ..." (newest first)
            for line in output.stdout.split("\n"):
                if "Available versions:" in line:
                    versions = [v.strip() for v in line.split(":")[1].split(",")]
                    # shortcut: matches EXABGP_SPEC (4.2 <= v < 5) by prefix; update with the spec
                    latest = next((v for v in versions if v.startswith("4.")), None)
                    result["available"] = latest
                    # A missing ExaBGP (for example after "pipx reinstall") is installed too
                    if latest and latest != current:
                        result["update_available"] = True
                    break
    except (subprocess.TimeoutExpired, FileNotFoundError):
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


def _venv_pip(config: OberConfig) -> list[str] | None:
    """Return the pip command of the ober venv, or None when the venv is missing.

    Uses ``python -m pip`` because pipx venvs have no ``bin/pip``.
    """
    python = config.venv_path / "bin" / "python"
    return [str(python), "-m", "pip"] if python.exists() else None


def _upgrade_exabgp(config: OberConfig) -> None:
    """Upgrade ExaBGP in the virtual environment, inside the tested version range."""
    pip = _venv_pip(config)
    if pip is None:
        console.print("[yellow]ExaBGP venv not found, skipping[/yellow]")
        return
    run_command([*pip, "install", "--upgrade", EXABGP_SPEC])
    console.print("[green]ExaBGP upgraded[/green]")
