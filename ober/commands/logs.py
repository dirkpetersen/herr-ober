#!/usr/bin/env python3
"""Ober logs command - view service logs."""

import subprocess
import sys

import click
from rich.console import Console

console = Console()

UNITS = {
    "http": "ober-http",
    "bgp": "ober-bgp",
    "bfd": "ober-bfd",
    "ha": "ober-ha",
    "blocklist": "ober-blocklist",
    "watch": "ober-watch",
}


@click.command()
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Follow log output in real-time.",
)
@click.option(
    "-n",
    "--lines",
    type=int,
    default=None,
    help="Number of lines to show (default 50, or all with --since).",
)
@click.option(
    "--service",
    type=click.Choice([*UNITS, "all"]),
    default="all",
    help="Service to show logs for.",
)
@click.option(
    "--since",
    help="Show entries since this time, e.g. '1 hour ago' or '2026-11-24 10:00'.",
)
@click.pass_context
def logs(
    ctx: click.Context, follow: bool, lines: int | None, service: str, since: str | None
) -> None:
    """View Ober service logs.

    Shows logs from systemd journal for Ober services.
    Use -f to tail logs in real-time. Per-request access logs are not in the
    journal: HAProxy sends them to security.log_target (syslog).
    """
    cmd = ["journalctl"]
    for unit in UNITS.values() if service == "all" else [UNITS[service]]:
        cmd.extend(["-u", unit])
    if since:
        cmd.extend(["--since", since])
    if ctx.obj is not None and ctx.obj.json_output:
        cmd.append("--output=json")

    # Add options
    if follow:
        cmd.append("-f")
    elif lines or not since:
        cmd.extend(["-n", str(lines or 50)])

    # Show newest first (unless following)
    if not follow:
        cmd.append("--no-pager")

    # Output with colors if terminal supports it
    if sys.stdout.isatty() and "--output=json" not in cmd:
        cmd.append("--output=short-full")

    try:
        # Run journalctl, passing through stdout/stderr
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print("[red]Error:[/red] journalctl not found.")
        console.print("Logs are stored in systemd journal.")
        ctx.exit(1)
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully when following
        pass
