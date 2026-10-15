#!/usr/bin/env python3
"""Ober uninstall command - clean removal."""

import contextlib
import shutil
import sys
from pathlib import Path

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.config import OberConfig
from ober.system import ServiceInfo, SystemInfo, check_command_exists, run_command

console = Console()


def _is_pipx_install() -> Path | None:
    """Check if ober is installed via pipx and return the venv path."""
    if sys.prefix == sys.base_prefix:
        return None  # Not in a venv

    venv_path = Path(sys.prefix)
    # Check if this looks like a pipx venv (contains pipx in path)
    if "pipx" in str(venv_path) and "herr-ober" in str(venv_path):
        return venv_path
    return None


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

    # Check if ober created a local venv (not pipx-managed)
    local_venv = config.install_path / "venv"
    has_local_venv = local_venv.exists()

    # Check if installed via pipx
    pipx_venv = _is_pipx_install()
    has_pipx = pipx_venv is not None and check_command_exists("pipx")

    # Check for /usr/local/bin/ober symlink
    ober_symlink = Path("/usr/local/bin/ober")
    has_symlink = ober_symlink.exists() or ober_symlink.is_symlink()

    console.print()
    console.print("[bold yellow]Ober Uninstall[/bold yellow]")
    console.print()
    console.print("This will remove:")
    console.print("  - Systemd services (ober-http, ober-bgp, ober-ha)")
    console.print(f"  - Installation directory: {config.install_path}")
    if has_local_venv:
        console.print(f"  - Python venv (with ExaBGP): {local_venv}")
    if not keep_config:
        console.print(f"  - Configuration: {config.config_path}")
    console.print("  - Kernel tuning: /etc/sysctl.d/99-herr-ober.conf")
    console.print("  - VIP interface configuration")
    if has_symlink:
        console.print(f"  - Symlink: {ober_symlink}")
    if has_pipx:
        console.print(f"  - pipx package: herr-ober ({pipx_venv})")
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
    for service_name in ["ober-bgp", "ober-ha", "ober-http"]:
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
    for service_file in ["ober-http.service", "ober-bgp.service", "ober-ha.service"]:
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

    # Remove install-path pointer (unless config is being kept)
    if not keep_config:
        from ober import config as ober_config

        pointer = ober_config.INSTALL_POINTER_PATH
        if pointer.exists():
            with contextlib.suppress(OSError):
                pointer.unlink()
                console.print(f"  [green]Removed {pointer}[/green]")
            with contextlib.suppress(OSError):
                pointer.parent.rmdir()  # only if empty

    # Step 6: Remove user secrets
    secrets_path = Path.home() / ".ober"
    if secrets_path.exists() and not keep_config:
        shutil.rmtree(secrets_path)
        console.print(f"  [green]Removed {secrets_path}[/green]")

    # Step 7: Remove /usr/local/bin/ober symlink
    if has_symlink:
        console.print("Removing symlink...")
        try:
            ober_symlink.unlink()
            console.print(f"  [green]Removed {ober_symlink}[/green]")
        except OSError as e:
            console.print(f"  [yellow]Could not remove {ober_symlink}: {e}[/yellow]")

    # Step 8: Remove pipx package (must be last - removes running code!)
    if has_pipx:
        console.print("Removing pipx package...")
        console.print("[bold green]Uninstall complete![/bold green]")
        if keep_config:
            console.print()
            console.print(f"Configuration preserved at: {config.config_path}")
        # Use subprocess directly to avoid issues after package removal
        import os
        import subprocess

        env = dict(os.environ)
        if (
            pipx_venv is not None
            and pipx_venv.parent.name == "venvs"
            and not str(pipx_venv).startswith(("/root/", "/home/"))
        ):
            env["PIPX_HOME"] = str(pipx_venv.parent.parent)  # e.g. /opt/pipx
            env["PIPX_BIN_DIR"] = "/usr/local/bin"
        subprocess.run(["pipx", "uninstall", "herr-ober"], check=False, env=env)
        # Exit immediately - we can't continue after removing ourselves
        return

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
