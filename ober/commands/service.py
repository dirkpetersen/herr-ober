#!/usr/bin/env python3
"""Ober service commands - start, stop, restart."""

import time
from collections.abc import Callable

import click
from rich.console import Console

from ober.commands.maintenance import is_maintenance, read_json, set_maintenance
from ober.config import OberConfig, exit_if_invalid
from ober.system import ServiceInfo, SystemInfo, run_command
from ober.watch.runtime import HAProxyRuntime

console = Console()

ROTATION_TIMEOUT = 15  # seconds to wait for the BGP withdraw or the VIP move


def _wait_until(cond: Callable[[], bool], timeout: int) -> bool:
    """Poll cond once a second for up to timeout seconds."""
    for _ in range(max(0, timeout)):
        if cond():
            return True
        time.sleep(1)
    return cond()


def _out_of_rotation(config: OberConfig) -> bool:
    """True when this node announces no route (BGP) or owns no VIP (keepalived)."""
    if config.ha_mode == "bgp":
        return read_json(config.health_state_path).get("announced") == []
    return "MASTER" not in read_json(config.vrrp_state_path).values()


def _curr_conns(runtime: HAProxyRuntime) -> int:
    """Current client connections from 'show info' (the CLI connection is not counted)."""
    for line in runtime.send("show info").splitlines():
        if line.startswith("CurrConns:"):
            return int(line.split(":", 1)[1])
    raise OSError("no CurrConns in 'show info'")


def _drain(config: OberConfig, ha_active: bool) -> bool:
    """Enter maintenance, wait until the node is out of rotation, then drain connections.

    Returns:
        True when this call turned maintenance on, so the caller turns it off again.
    """
    was_on = is_maintenance(config.maintenance_path)
    set_maintenance(config.maintenance_path, True)
    if ha_active:
        console.print("Maintenance on, waiting for this node to leave rotation...")
        if _wait_until(lambda: _out_of_rotation(config), ROTATION_TIMEOUT):
            console.print("[green]Node is out of rotation[/green]")
        else:
            console.print(
                f"[yellow]Warning:[/yellow] node still in rotation after {ROTATION_TIMEOUT}s"
                " (no peer to take over?), continuing"
            )

    console.print(f"Draining connections (up to {config.drain_timeout}s)...")
    runtime = HAProxyRuntime(config.admin_socket_path, timeout=2)
    left = -1

    def idle() -> bool:
        nonlocal left
        left = _curr_conns(runtime)
        return left == 0

    try:
        if _wait_until(idle, config.drain_timeout):
            console.print("[green]All connections closed[/green]")
        else:
            console.print(f"[yellow]Warning:[/yellow] {left} connections still open, stopping")
    except (OSError, ValueError) as e:
        console.print(f"[yellow]Warning:[/yellow] cannot read connections ({e}), not draining")
    return not was_on


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
    exit_if_invalid(config, ctx)

    # Check if config exists
    if not config.haproxy_config_path.exists():
        console.print("[red]Error:[/red] HAProxy configuration not found.")
        console.print("Run 'ober config' first to configure the system.")
        ctx.exit(1)

    console.print("Starting Ober services...")

    # Disable the other mode's unit first: its ober-http.service.wants link would start it too
    other = "ober-ha" if config.ha_mode == "bgp" else "ober-bgp"
    run_command(["systemctl", "disable", "--now", other], check=False)

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

    Performs graceful shutdown: enters maintenance (BGP withdraw or VRRP
    priority drop), waits until the node is out of rotation, waits up to
    drain_timeout seconds for open connections to close, then stops.
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

    clear_maintenance = False
    if not force and http_service.is_active:
        ha_active = bgp_service.is_active if config.ha_mode == "bgp" else ka_service.is_active
        clear_maintenance = _drain(config, ha_active)

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

    # Stop blocklist sync so it does not touch a stopped HAProxy
    try:
        run_command(["systemctl", "stop", "ober-blocklist.timer"], check=False)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Failed to stop ober-blocklist.timer: {e}")

    # Only clear maintenance that stop itself set, so 'ober start' brings the node back
    if clear_maintenance:
        set_maintenance(config.maintenance_path, False)

    console.print()
    console.print("[bold green]Services stopped![/bold green]")


def _check_config(ctx: click.Context, cmd: list[str], name: str) -> None:
    """Run a config check and exit 1 with its output when it fails."""
    try:
        result = run_command(cmd, check=False)
    except Exception as e:
        console.print(f"[red]Cannot check the {name} configuration:[/red] {e}")
        ctx.exit(1)
    if result.returncode != 0:
        console.print(f"[red]The {name} configuration is not valid. Nothing was changed.[/red]")
        console.print(result.stdout + result.stderr, markup=False)
        ctx.exit(1)


def _haproxy_check_cmd(config: OberConfig) -> list[str]:
    return ["/usr/sbin/haproxy", "-c", "-f", str(config.haproxy_config_path)]


@click.command()
@click.option(
    "--hard",
    is_flag=True,
    help="Full restart: drops all connections and the BGP session or VIPs.",
)
@click.option("--reload-only", is_flag=True, hidden=True, help="Deprecated: this is the default.")
@click.pass_context
def restart(ctx: click.Context, hard: bool, reload_only: bool) -> None:
    """Restart Ober services with a validated, zero-downtime reload.

    By default validates the HAProxy config and reloads HAProxy without downtime (and
    keepalived in keepalived mode). A failed check or reload keeps the running process.
    Use --hard for a full restart, for example to load a new ExaBGP config or binary.
    """
    del reload_only  # kept so old scripts keep working
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Restarting services requires root access.")
        console.print("Run with: sudo ober restart")
        ctx.exit(1)

    config = OberConfig.load()
    ha_unit = "ober-bgp" if config.ha_mode == "bgp" else "ober-ha"

    if not ServiceInfo.from_service_name("ober-http").is_active:
        console.print("[yellow]Warning:[/yellow] HAProxy is not running. Starting instead.")
        ctx.invoke(start)
        return

    # Never stop a working HAProxy for a config it cannot load
    _check_config(ctx, _haproxy_check_cmd(config), "HAProxy")

    if hard:
        ha_active = ServiceInfo.from_service_name(ha_unit).is_active
        clear_maintenance = _drain(config, ha_active)
        console.print("Restarting Ober services...")
        try:
            run_command(["systemctl", "restart", "ober-http"])
            console.print("[green]Restarted ober-http (HAProxy)[/green]")
        except Exception as e:
            console.print(f"[red]Failed to restart ober-http:[/red] {e}")
            ctx.exit(1)
        finally:
            if clear_maintenance:
                set_maintenance(config.maintenance_path, False)

        # The restart of ober-http also stops (BindsTo) and restarts the HA unit; make sure
        # it runs again, also on older units without WantedBy=ober-http.service.
        if ServiceInfo.from_service_name(ha_unit).is_enabled:
            try:
                run_command(["systemctl", "start", ha_unit])
                console.print(f"[green]Started {ha_unit}[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning:[/yellow] Failed to start {ha_unit}: {e}")
        console.print()
        console.print("[bold green]Services restarted![/bold green]")
        return

    try:
        run_command(["systemctl", "reload", "ober-http"])
    except Exception as e:
        # No fallback to a restart: it would load the same config and take the node down
        console.print(f"[red]Failed to reload HAProxy, the running process is kept:[/red] {e}")
        console.print("See 'journalctl -u ober-http' for details.")
        ctx.exit(1)
    console.print("[green]HAProxy configuration reloaded (zero-downtime)[/green]")

    if config.ha_mode == "keepalived":
        if ServiceInfo.from_service_name("ober-ha").is_active:
            _check_config(
                ctx,
                ["/usr/sbin/keepalived", "-t", "-f", str(config.keepalived_config_path)],
                "keepalived",
            )
            try:
                run_command(["systemctl", "reload", "ober-ha"])
                console.print("[green]Keepalived configuration reloaded[/green]")
            except Exception as e:
                console.print(f"[red]Failed to reload ober-ha:[/red] {e}")
                ctx.exit(1)
    else:
        console.print("[dim]ExaBGP keeps running. Use 'ober restart --hard' for BGP changes.[/dim]")
