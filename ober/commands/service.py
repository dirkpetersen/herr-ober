#!/usr/bin/env python3
"""Ober service commands - start, stop, restart."""

import time

import click
from rich.console import Console

from ober.config import OberConfig
from ober.system import ServiceInfo, SystemInfo, run_command

console = Console()


@click.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """Start Ober services.

    Starts HAProxy and ExaBGP services. HAProxy starts first,
    then ExaBGP begins announcing routes.
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Starting services requires root access.")
        console.print("Run with: sudo ober start")
        ctx.exit(1)

    config = OberConfig.load()

    # Check if config exists
    if not config.haproxy_config_path.exists():
        console.print("[red]Error:[/red] HAProxy configuration not found.")
        console.print("Run 'ober config' first to configure the system.")
        ctx.exit(1)

    console.print("Starting Ober services...")

    # Enable and start HAProxy
    try:
        run_command(["systemctl", "enable", "ober-http"])
        run_command(["systemctl", "start", "ober-http"])
        console.print("[green]Started ober-http (HAProxy)[/green]")
    except Exception as e:
        console.print(f"[red]Failed to start ober-http:[/red] {e}")
        ctx.exit(1)

    # Wait for HAProxy to be ready
    time.sleep(1)

    # Enable and start BGP or Keepalived based on ha_mode
    if config.ha_mode == "bgp":
        if config.bgp_config_path.exists() and config.bgp.neighbors:
            try:
                run_command(["systemctl", "enable", "ober-bgp"])
                run_command(["systemctl", "start", "ober-bgp"])
                console.print("[green]Started ober-bgp (ExaBGP)[/green]")
            except Exception as e:
                console.print(f"[red]Failed to start ober-bgp:[/red] {e}")
                ctx.exit(1)
        else:
            console.print("[yellow]Skipping ober-bgp (not configured)[/yellow]")
    else:  # keepalived mode
        # Start keepalived if config exists (peers not required for single-node testing)
        if config.keepalived_config_path.exists():
            try:
                run_command(["systemctl", "enable", "ober-ha"])
                run_command(["systemctl", "start", "ober-ha"])
                console.print("[green]Started ober-ha (Keepalived)[/green]")
            except Exception as e:
                console.print(f"[red]Failed to start ober-ha:[/red] {e}")
                ctx.exit(1)
        else:
            console.print("[yellow]Skipping ober-ha (not configured)[/yellow]")

    # Pull the blocklist from ober watch, if one is configured (failure is not fatal)
    if config.watch.url:
        try:
            run_command(["systemctl", "enable", "--now", "ober-blocklist.timer"])
            console.print("[green]Started ober-blocklist.timer (blocklist sync)[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to start blocklist sync: {e}")

    console.print()
    console.print("[bold green]Services started![/bold green]")
    console.print("Run 'ober status' to check service health")


@click.command()
@click.option(
    "--force",
    is_flag=True,
    help="Force immediate stop without graceful shutdown.",
)
@click.pass_context
def stop(ctx: click.Context, force: bool) -> None:
    """Stop Ober services.

    Performs graceful shutdown: withdraws BGP routes first,
    waits for connections to drain, then stops HAProxy.
    Use --force for immediate shutdown.
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Stopping services requires root access.")
        console.print("Run with: sudo ober stop")
        ctx.exit(1)

    console.print("Stopping Ober services...")

    config = OberConfig.load()

    bgp_service = ServiceInfo.from_service_name("ober-bgp")
    ka_service = ServiceInfo.from_service_name("ober-ha")
    http_service = ServiceInfo.from_service_name("ober-http")

    if not force:
        # Graceful shutdown: stop HA service first
        if config.ha_mode == "bgp" and bgp_service.is_active:
            console.print("Withdrawing BGP routes...")
            try:
                run_command(["systemctl", "stop", "ober-bgp"])
                console.print("[green]Stopped ober-bgp (routes withdrawn)[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning:[/yellow] Failed to stop ober-bgp: {e}")
        elif config.ha_mode == "keepalived" and ka_service.is_active:
            console.print("Releasing VIPs...")
            try:
                run_command(["systemctl", "stop", "ober-ha"])
                console.print("[green]Stopped ober-ha (VIPs released)[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning:[/yellow] Failed to stop ober-ha: {e}")

        # Wait for connections to drain
        if http_service.is_active:
            console.print("Waiting for connections to drain (5s)...")
            time.sleep(5)

    # Stop HAProxy
    if http_service.is_active:
        try:
            run_command(["systemctl", "stop", "ober-http"])
            console.print("[green]Stopped ober-http (HAProxy)[/green]")
        except Exception as e:
            console.print(f"[red]Failed to stop ober-http:[/red] {e}")
            ctx.exit(1)
    else:
        console.print("[dim]ober-http was not running[/dim]")

    # Stop HA services if not already stopped
    if bgp_service.is_active:
        try:
            run_command(["systemctl", "stop", "ober-bgp"])
            console.print("[green]Stopped ober-bgp[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to stop ober-bgp: {e}")

    if ka_service.is_active:
        try:
            run_command(["systemctl", "stop", "ober-ha"])
            console.print("[green]Stopped ober-ha[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Failed to stop ober-ha: {e}")

    console.print()
    console.print("[bold green]Services stopped![/bold green]")


@click.command()
@click.option(
    "--reload-only",
    is_flag=True,
    help="Only reload HAProxy config (zero-downtime).",
)
@click.pass_context
def restart(ctx: click.Context, reload_only: bool) -> None:
    """Restart Ober services.

    By default performs a full restart. Use --reload-only for
    zero-downtime HAProxy config reload.
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Restarting services requires root access.")
        console.print("Run with: sudo ober restart")
        ctx.exit(1)

    if reload_only:
        console.print("Reloading HAProxy configuration...")
        http_service = ServiceInfo.from_service_name("ober-http")

        if not http_service.is_active:
            console.print("[yellow]Warning:[/yellow] HAProxy is not running. Starting instead.")
            ctx.invoke(start)
            return

        try:
            run_command(["systemctl", "reload", "ober-http"])
            console.print("[green]HAProxy configuration reloaded (zero-downtime)[/green]")
        except Exception as e:
            console.print(f"[red]Failed to reload HAProxy:[/red] {e}")
            console.print("Attempting full restart...")
            run_command(["systemctl", "restart", "ober-http"])

    else:
        console.print("Restarting Ober services...")

        config = OberConfig.load()

        # Restart HAProxy
        try:
            run_command(["systemctl", "restart", "ober-http"])
            console.print("[green]Restarted ober-http (HAProxy)[/green]")
        except Exception as e:
            console.print(f"[red]Failed to restart ober-http:[/red] {e}")
            ctx.exit(1)

        # Wait for HAProxy to be ready
        time.sleep(1)

        # Restart BGP or Keepalived based on ha_mode
        if config.ha_mode == "bgp":
            bgp_service = ServiceInfo.from_service_name("ober-bgp")
            if bgp_service.is_enabled:
                try:
                    run_command(["systemctl", "restart", "ober-bgp"])
                    console.print("[green]Restarted ober-bgp (ExaBGP)[/green]")
                except Exception as e:
                    console.print(f"[yellow]Warning:[/yellow] Failed to restart ober-bgp: {e}")
        else:
            ka_service = ServiceInfo.from_service_name("ober-ha")
            if ka_service.is_enabled:
                try:
                    run_command(["systemctl", "restart", "ober-ha"])
                    console.print("[green]Restarted ober-ha (Keepalived)[/green]")
                except Exception as e:
                    console.print(f"[yellow]Warning:[/yellow] Failed to restart ober-ha: {e}")

    console.print()
    console.print("[bold green]Services restarted![/bold green]")
