#!/usr/bin/env python3
"""Ober CLI - High-performance S3 ingress controller."""

import json
from typing import Any

import click
from rich.console import Console

from ober import __version__
from ober.config import OberConfig
from ober.system import (
    SystemInfo,
    get_exabgp_version,
    get_haproxy_version,
)

# Global console for rich output
console = Console()
error_console = Console(stderr=True)


class Context:
    """CLI context object for sharing state."""

    def __init__(self) -> None:
        self.verbose: bool = False
        self.quiet: bool = False
        self.json_output: bool = False
        self.config: OberConfig | None = None
        self.system: SystemInfo = SystemInfo()

    def output(self, message: str, style: str | None = None) -> None:
        """Output a message respecting quiet mode."""
        if not self.quiet:
            console.print(message, style=style)

    def output_json(self, data: Any) -> None:
        """Output data as JSON."""
        click.echo(json.dumps(data, indent=2, default=str))

    def output_error(self, message: str) -> None:
        """Output an error message."""
        if self.json_output:
            self.output_json({"error": message})
        else:
            error_console.print(f"[red]Error:[/red] {message}")

    def output_success(self, message: str) -> None:
        """Output a success message."""
        if not self.quiet and not self.json_output:
            console.print(f"[green]{message}[/green]")

    def output_warning(self, message: str) -> None:
        """Output a warning message."""
        if not self.quiet and not self.json_output:
            console.print(f"[yellow]Warning:[/yellow] {message}")


pass_context = click.make_pass_decorator(Context, ensure=True)


def version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Show version information."""
    if not value or ctx.resilient_parsing:
        return

    versions = {
        "ober": __version__,
        "haproxy": get_haproxy_version() or "not installed",
        "exabgp": get_exabgp_version() or "not yet bootstrapped",
    }

    # Check if we want JSON output
    if ctx.params.get("json_output"):
        click.echo(json.dumps(versions, indent=2))
    else:
        console.print(f"[bold]ober[/bold] version {versions['ober']}")
        console.print(f"  HAProxy: {versions['haproxy']}")
        console.print(f"  ExaBGP:  {versions['exabgp']}")

    ctx.exit()


@click.group()
@click.option(
    "--version",
    is_flag=True,
    callback=version_callback,
    expose_value=False,
    is_eager=True,
    help="Show version information.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output in JSON format.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Minimal output.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Verbose output.",
)
@pass_context
def main(ctx: Context, json_output: bool, quiet: bool, verbose: bool) -> None:
    """Ober - High-performance S3 ingress controller for Ceph RGW clusters.

    Uses HAProxy 3.3 (AWS-LC) for SSL offloading and ExaBGP for Layer 3 HA
    via BGP/ECMP.
    """
    ctx.json_output = json_output
    ctx.quiet = quiet
    ctx.verbose = verbose

    # Load config if it exists
    ctx.config = OberConfig.load()


# Import subcommands (must be after main group is defined)
from ober.commands.bootstrap import bootstrap  # noqa: E402
from ober.commands.config import config  # noqa: E402
from ober.commands.doctor import doctor  # noqa: E402
from ober.commands.health import health  # noqa: E402
from ober.commands.logs import logs  # noqa: E402
from ober.commands.service import restart, start, stop  # noqa: E402
from ober.commands.status import status  # noqa: E402
from ober.commands.sync import sync  # noqa: E402
from ober.commands.test import test  # noqa: E402
from ober.commands.uninstall import uninstall  # noqa: E402
from ober.commands.upgrade import upgrade  # noqa: E402

# Register commands
main.add_command(bootstrap)
main.add_command(config)
main.add_command(doctor)
main.add_command(health)
main.add_command(logs)
main.add_command(restart)
main.add_command(start)
main.add_command(status)
main.add_command(stop)
main.add_command(sync)
main.add_command(test)
main.add_command(uninstall)
main.add_command(upgrade)


if __name__ == "__main__":
    main()
