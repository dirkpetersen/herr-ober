#!/usr/bin/env python3
"""Ober logs command - view service logs."""

import subprocess
import sys

import click
from rich.console import Console

console = Console()


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
    default=50,
    help="Number of lines to show.",
)
@click.option(
    "--service",
    type=click.Choice(["http", "bgp", "all"]),
    default="all",
    help="Service to show logs for.",
)
@click.pass_context
def logs(ctx: click.Context, follow: bool, lines: int, service: str) -> None:
    """View Ober service logs.

    Shows logs from systemd journal for Ober services.
    Use -f to tail logs in real-time.
    """
    # Build journalctl command
    cmd = ["journalctl"]

    # Add service filter
    if service == "http":
        cmd.extend(["-u", "ober-http"])
    elif service == "bgp":
        cmd.extend(["-u", "ober-bgp"])
    else:
        cmd.extend(["-u", "ober-http", "-u", "ober-bgp"])

    # Add options
    if follow:
        cmd.append("-f")
    else:
        cmd.extend(["-n", str(lines)])

    # Show newest first (unless following)
    if not follow:
        cmd.append("--no-pager")

    # Output with colors if terminal supports it
    if sys.stdout.isatty():
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
