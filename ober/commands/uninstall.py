#!/usr/bin/env python3
"""Ober uninstall command - clean removal."""

import contextlib
import shutil
from pathlib import Path

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.config import OberConfig
from ober.system import ServiceInfo, SystemInfo, run_command

console = Console()


@click.command()
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt.",
)
@click.option(
    "--keep-config",
    is_flag=True,
    help="Keep configuration files.",
)
@click.pass_context
def uninstall(ctx: click.Context, yes: bool, keep_config: bool) -> None:
    """Uninstall Ober and clean up.

    Removes systemd services, configuration files, and the installation
    directory. Requires confirmation unless --yes is specified.

    Use this command if bootstrap fails midway and you need to retry.
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Uninstall requires root access.")
        console.print("Run with: sudo ober uninstall")
        ctx.exit(1)

    config = OberConfig.load()

    console.print()
    console.print("[bold yellow]Ober Uninstall[/bold yellow]")
    console.print()
    console.print("This will remove:")
    console.print("  - Systemd services (ober-http, ober-bgp)")
    console.print(f"  - Installation directory: {config.install_path}")
    if not keep_config:
        console.print(f"  - Configuration: {config.config_path}")
    console.print("  - Kernel tuning: /etc/sysctl.d/99-herr-ober.conf")
    console.print("  - VIP interface configuration")
    console.print()

    if not yes:
        confirm = inquirer.confirm(
            "Are you sure you want to uninstall?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Uninstall cancelled.[/yellow]")
            ctx.exit(0)

    console.print()
    console.print("Uninstalling...")

    # Step 1: Stop services
    console.print("Stopping services...")
    for service_name in ["ober-bgp", "ober-http"]:
        service = ServiceInfo.from_service_name(service_name)
        if service.is_active:
            try:
                run_command(["systemctl", "stop", service_name], check=False)
                console.print(f"  [green]Stopped {service_name}[/green]")
            except Exception:
                pass
        if service.is_enabled:
            with contextlib.suppress(Exception):
                run_command(["systemctl", "disable", service_name], check=False)

    # Step 2: Remove systemd service files
    console.print("Removing systemd services...")
    systemd_path = Path("/etc/systemd/system")
    for service_file in ["ober-http.service", "ober-bgp.service"]:
        service_path = systemd_path / service_file
        if service_path.exists():
            service_path.unlink()
            console.print(f"  [green]Removed {service_path}[/green]")

    # Reload systemd
    run_command(["systemctl", "daemon-reload"], check=False)

    # Step 3: Remove kernel tuning
    console.print("Removing kernel tuning...")
    sysctl_path = Path("/etc/sysctl.d/99-herr-ober.conf")
    if sysctl_path.exists():
        sysctl_path.unlink()
        console.print(f"  [green]Removed {sysctl_path}[/green]")
        run_command(["sysctl", "--system"], check=False)

    # Step 4: Remove VIP interface
    console.print("Removing VIP interface...")
    _remove_vip_interface(system)

    # Step 5: Remove installation directory
    if config.install_path.exists():
        if keep_config:
            # Remove everything except etc/
            console.print("Cleaning installation directory (keeping config)...")
            for item in config.install_path.iterdir():
                if item.name != "etc":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    console.print(f"  [green]Removed {item}[/green]")
        else:
            console.print("Removing installation directory...")
            shutil.rmtree(config.install_path)
            console.print(f"  [green]Removed {config.install_path}[/green]")

    # Step 6: Remove user secrets
    secrets_path = Path.home() / ".ober"
    if secrets_path.exists() and not keep_config:
        shutil.rmtree(secrets_path)
        console.print(f"  [green]Removed {secrets_path}[/green]")

    console.print()
    console.print("[bold green]Uninstall complete![/bold green]")

    if keep_config:
        console.print()
        console.print(f"Configuration preserved at: {config.config_path}")


def _remove_vip_interface(system: SystemInfo) -> None:
    """Remove the VIP dummy interface."""
    from ober.system import OSFamily

    if system.os_family == OSFamily.DEBIAN:
        netplan_path = Path("/etc/netplan/60-vip.yaml")
        if netplan_path.exists():
            netplan_path.unlink()
            console.print(f"  [green]Removed {netplan_path}[/green]")
            run_command(["netplan", "apply"], check=False)

    elif system.os_family == OSFamily.RHEL:
        # Remove nmcli connection
        run_command(
            ["nmcli", "connection", "delete", "lo-vip"],
            check=False,
        )
        console.print("  [green]Removed lo-vip connection[/green]")
