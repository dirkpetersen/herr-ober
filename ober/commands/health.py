#!/usr/bin/env python3
"""Ober health command - HAProxy health checker for ExaBGP.

This module is spawned by ExaBGP as a process. It continuously checks
HAProxy health and outputs BGP announce/withdraw commands to stdout
which ExaBGP reads and acts upon.
"""

import json
import signal
import sys
import time
from pathlib import Path

import click
import requests

from ober.commands.maintenance import is_maintenance, write_atomic
from ober.config import OberConfig

# Global flags for graceful shutdown and for re-reading the VIPs (SIGHUP from ober apply)
_running = True
_reload = False


def _signal_handler(_signum: int, _frame: object) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _running
    _running = False


def _hup_handler(_signum: int, _frame: object) -> None:
    global _reload
    _reload = True


def _config_vips() -> list[str]:
    return [v.address.split("/")[0] for v in OberConfig.load().vips]


class Hysteresis:
    """Rise/fall counters with a startup hold-down for the announce decision."""

    def __init__(self, rise: int, fall: int, holddown_until: float) -> None:
        self.rise = max(1, rise)
        self.fall = max(1, fall)
        self.holddown_until = holddown_until
        self.up = False
        self.good = 0
        self.bad = 0

    def update(self, healthy: bool, maintenance: bool, now: float) -> str | None:
        """Feed one probe result.

        Returns:
            The reason for a state change, or None when the state stays the same.
        """
        if healthy:
            self.good, self.bad = self.good + 1, 0
        else:
            self.good, self.bad = 0, self.bad + 1
        if self.up and maintenance:
            self.up = False
            return "maintenance mode is on"
        if self.up and self.bad >= self.fall:
            self.up = False
            return f"{self.bad} failed probes in a row"
        if (
            not self.up
            and not maintenance
            and self.good >= self.rise
            and now >= self.holddown_until
        ):
            self.up = True
            return f"{self.good} good probes in a row"
        return None


def _log(msg: str) -> None:
    sys.stderr.write(f"[ober] {msg}\n")
    sys.stderr.flush()


def _write_state(path: Path, announced: list[str]) -> None:
    """Write the announced routes for `ober status` (never fatal)."""
    try:
        write_atomic(path, json.dumps({"announced": announced, "updated": int(time.time())}) + "\n")
    except OSError as e:
        _log(f"Cannot write {path}: {e}")


@click.command()
@click.argument("vip", required=False)
@click.option(
    "--interval",
    default=1.0,
    help="Health check interval in seconds.",
)
@click.option(
    "--timeout",
    default=2.0,
    help="Health check timeout in seconds.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Probe one time and exit 0 (healthy) or 1. Used by the keepalived check.",
)
@click.option("--port", type=int, default=None, help="HAProxy stats port (default: from config).")
@click.pass_context
def health(
    ctx: click.Context,
    vip: str | None,
    interval: float,
    timeout: float,
    once: bool,
    port: int | None,
) -> None:
    """Run health check process for ExaBGP.

    Continuously monitors HAProxy health endpoint and outputs BGP
    announce/withdraw commands. This command is meant to be spawned
    by ExaBGP's process configuration. Routes are announced after
    bgp.health_rise good probes (and not before bgp.health_holddown
    seconds), withdrawn after bgp.health_fall failed probes, and
    withdrawn at once while maintenance mode is on.

    VIP is the virtual IP address to announce/withdraw.
    If not specified, all configured VIPs are used.
    """
    if once:
        if port is None:
            port = OberConfig.load().stats_port
        ctx.exit(0 if _check_health(f"http://127.0.0.1:{port}/health", timeout) else 1)

    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _hup_handler)

    config = OberConfig.load()

    # Determine VIPs to manage
    if vip:
        vips = [vip]
    elif config.vips:
        vips = [v.address.split("/")[0] for v in config.vips]
    else:
        # No VIPs configured, exit
        _log("No VIPs configured, exiting health check")
        ctx.exit(1)

    health_url = f"http://127.0.0.1:{port or config.stats_port}/health"
    routes = [f"{v}/32" for v in vips]
    state_path = config.health_state_path
    state = Hysteresis(
        config.bgp.health_rise,
        config.bgp.health_fall,
        time.monotonic() + config.bgp.health_holddown,
    )

    _log(f"Starting health check for VIPs: {', '.join(vips)}")
    _log(
        f"Health endpoint: {health_url} (rise {state.rise}, fall {state.fall}, "
        f"hold-down {config.bgp.health_holddown}s)"
    )
    _write_state(state_path, [])
    last_write = time.monotonic()

    global _reload
    while _running:
        if _reload and not vip:
            _reload = False
            vips, routes = _reload_vips(vips, state.up)
            last_write = 0.0  # write the new route list at once
        try:
            healthy = _check_health(health_url, timeout)
            maint = is_maintenance(config.maintenance_path)
        except Exception as e:
            _log(f"Health check error: {e}")
            healthy, maint = False, False

        reason = state.update(healthy, maint, time.monotonic())
        if reason:
            for vip_addr in vips:
                (_announce_route if state.up else _withdraw_route)(vip_addr)
            _log(f"{'Announced' if state.up else 'Withdrew'} routes: {reason}")

        # Refresh the state file on change and every 10 s, so readers can see it is live
        if reason or time.monotonic() - last_write >= 10:
            _write_state(state_path, routes if state.up else [])
            last_write = time.monotonic()

        time.sleep(interval)

    # Graceful shutdown - withdraw all routes
    _log("Shutting down, withdrawing routes")

    for vip_addr in vips:
        _withdraw_route(vip_addr)
    _write_state(state_path, [])


def _reload_vips(old: list[str], up: bool) -> tuple[list[str], list[str]]:
    """Re-read the VIPs; withdraw removed ones and, while healthy, announce new ones."""
    try:
        new = _config_vips()
    except Exception as e:  # keep serving the old list rather than die on a bad file
        _log(f"VIP reload failed, keeping {', '.join(old)}: {e}")
        return old, [f"{v}/32" for v in old]
    for addr in old:
        if addr not in new:
            _withdraw_route(addr)
    if up:
        for addr in new:
            if addr not in old:
                _announce_route(addr)
    _log(f"Reloaded VIPs: {', '.join(new) or 'none'}")
    return new, [f"{v}/32" for v in new]


def _check_health(url: str, timeout: float) -> bool:
    """Check HAProxy health endpoint.

    Args:
        url: Health endpoint URL.
        timeout: Request timeout in seconds.

    Returns:
        True if healthy (HTTP 200), False otherwise.
    """
    try:
        response = requests.get(url, timeout=timeout)
        return response.status_code == 200
    except requests.RequestException:
        return False


def _announce_route(vip: str) -> None:
    """Output BGP announce command to stdout.

    Args:
        vip: IP address to announce.
    """
    # ExaBGP text encoder format
    print(f"announce route {vip}/32 next-hop self")
    sys.stdout.flush()


def _withdraw_route(vip: str) -> None:
    """Output BGP withdraw command to stdout.

    Args:
        vip: IP address to withdraw.
    """
    # ExaBGP text encoder format
    print(f"withdraw route {vip}/32 next-hop self")
    sys.stdout.flush()


# Allow running as a module
if __name__ == "__main__":
    health()
