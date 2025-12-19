#!/usr/bin/env python3
"""Ober health command - HAProxy health checker for ExaBGP.

This module is spawned by ExaBGP as a process. It continuously checks
HAProxy health and outputs BGP announce/withdraw commands to stdout
which ExaBGP reads and acts upon.
"""

import signal
import sys
import time

import click
import requests

from ober.config import OberConfig

# Global flag for graceful shutdown
_running = True


def _signal_handler(_signum: int, _frame: object) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _running
    _running = False


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
@click.pass_context
def health(ctx: click.Context, vip: str | None, interval: float, timeout: float) -> None:
    """Run health check process for ExaBGP.

    Continuously monitors HAProxy health endpoint and outputs BGP
    announce/withdraw commands. This command is meant to be spawned
    by ExaBGP's process configuration.

    VIP is the virtual IP address to announce/withdraw.
    If not specified, all configured VIPs are used.
    """
    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    config = OberConfig.load()

    # Determine VIPs to manage
    if vip:
        vips = [vip]
    elif config.vips:
        vips = [v.address.split("/")[0] for v in config.vips]
    else:
        # No VIPs configured, exit
        sys.stderr.write("[ober] No VIPs configured, exiting health check\n")
        sys.stderr.flush()
        ctx.exit(1)

    health_url = f"http://127.0.0.1:{config.stats_port}/health"

    # Track current state
    announced = False

    sys.stderr.write(f"[ober] Starting health check for VIPs: {', '.join(vips)}\n")
    sys.stderr.write(f"[ober] Health endpoint: {health_url}\n")
    sys.stderr.flush()

    while _running:
        try:
            healthy = _check_health(health_url, timeout)

            if healthy and not announced:
                # Announce routes
                for vip_addr in vips:
                    _announce_route(vip_addr)
                announced = True
                sys.stderr.write("[ober] HAProxy healthy, announced routes\n")
                sys.stderr.flush()

            elif not healthy and announced:
                # Withdraw routes
                for vip_addr in vips:
                    _withdraw_route(vip_addr)
                announced = False
                sys.stderr.write("[ober] HAProxy unhealthy, withdrew routes\n")
                sys.stderr.flush()

        except Exception as e:
            sys.stderr.write(f"[ober] Health check error: {e}\n")
            sys.stderr.flush()

            # On error, withdraw routes to be safe
            if announced:
                for vip_addr in vips:
                    _withdraw_route(vip_addr)
                announced = False

        time.sleep(interval)

    # Graceful shutdown - withdraw all routes
    sys.stderr.write("[ober] Shutting down, withdrawing routes\n")
    sys.stderr.flush()

    for vip_addr in vips:
        _withdraw_route(vip_addr)


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
