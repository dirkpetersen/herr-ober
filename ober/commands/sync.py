#!/usr/bin/env python3
"""Ober sync command - update external system whitelists."""

import socket
from typing import Any

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.config import OberConfig
from ober.system import SystemInfo

console = Console()


def expand_hostlist(hostlist: str) -> list[str]:
    """Expand a Slurm-style hostlist to individual hosts.

    Args:
        hostlist: Slurm hostlist string (e.g., "node[01-03]" or "10.0.0.[1-5]")

    Returns:
        List of expanded hostnames/IPs.
    """
    try:
        import hostlist as hl  # type: ignore[import-untyped]

        return list(hl.expand_hostlist(hostlist))
    except ImportError:
        # Fallback: treat as comma-separated
        return [h.strip() for h in hostlist.split(",") if h.strip()]


def resolve_host(host: str) -> str | None:
    """Resolve a hostname to an IP address.

    Args:
        host: Hostname or IP address.

    Returns:
        IP address or None if resolution fails.
    """
    # Check if already an IP
    try:
        socket.inet_pton(socket.AF_INET, host)
        return host
    except OSError:
        pass

    # Try to resolve hostname
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        return None


@click.command()
@click.option(
    "--routers",
    help="Switches/routers hostlist or IPs (comma-separated).",
)
@click.option(
    "--frontend-http",
    "frontend_http",
    help="Frontend systems allowed HTTP access (hostlist or IPs).",
)
@click.option(
    "--backend-http",
    "backend_http",
    help="Backend S3/Ceph systems (hostlist or IPs).",
)
@click.pass_context
def sync(
    ctx: click.Context,
    routers: str | None,
    frontend_http: str | None,
    backend_http: str | None,
) -> None:
    """Update external system whitelists.

    Accepts Slurm hostlists or IP addresses. If no options specified,
    prompts for all whitelist categories interactively.

    Examples:
        ober sync --routers "switch[01-04]"
        ober sync --frontend-http "weka[001-100]"
        ober sync --backend-http "rgw[01-08].internal"
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root:
        console.print("[red]Error:[/red] Sync requires root access.")
        console.print("Run with: sudo ober sync")
        ctx.exit(1)

    config = OberConfig.load()

    # If no options provided, prompt interactively
    if not any([routers, frontend_http, backend_http]):
        routers, frontend_http, backend_http = _prompt_whitelists()

    results: dict[str, Any] = {}

    # Process routers
    if routers:
        router_ips = _process_hostlist(routers, "routers")
        results["routers"] = router_ips

    # Process frontend HTTP
    if frontend_http:
        frontend_ips = _process_hostlist(frontend_http, "frontend-http")
        results["frontend_http"] = frontend_ips

    # Process backend HTTP
    if backend_http:
        backend_ips = _process_hostlist(backend_http, "backend-http")
        results["backend_http"] = backend_ips

    # Write whitelist files
    if results:
        _write_whitelists(config, results)

        console.print()
        console.print("[bold green]Whitelists updated![/bold green]")

        # Reload HAProxy if running
        from ober.system import ServiceInfo, run_command

        http_service = ServiceInfo.from_service_name("ober-http")
        if http_service.is_active:
            console.print("Reloading HAProxy configuration...")
            try:
                run_command(["systemctl", "reload", "ober-http"])
                console.print("[green]HAProxy reloaded[/green]")
            except Exception as e:
                console.print(f"[yellow]Warning:[/yellow] Failed to reload HAProxy: {e}")
    else:
        console.print("[yellow]No whitelists to update.[/yellow]")


def _prompt_whitelists() -> tuple[str | None, str | None, str | None]:
    """Prompt user for whitelist entries interactively."""
    console.print()
    console.print("[bold]Whitelist Configuration[/bold]")
    console.print("Enter hostlists or IPs (leave empty to skip)")
    console.print()

    questions = [
        inquirer.Text(
            "routers",
            message="Routers/switches (hostlist or IPs)",
            default="",
        ),
        inquirer.Text(
            "frontend_http",
            message="Frontend HTTP clients (hostlist or IPs)",
            default="",
        ),
        inquirer.Text(
            "backend_http",
            message="Backend S3/Ceph servers (hostlist or IPs)",
            default="",
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        return None, None, None

    return (
        answers["routers"] or None,
        answers["frontend_http"] or None,
        answers["backend_http"] or None,
    )


def _process_hostlist(hostlist: str, category: str) -> list[str]:
    """Process a hostlist and resolve to IPs.

    Args:
        hostlist: Hostlist string.
        category: Category name for logging.

    Returns:
        List of resolved IP addresses.
    """
    console.print(f"Processing {category}...")

    # Expand hostlist
    hosts = expand_hostlist(hostlist)
    console.print(f"  Expanded to {len(hosts)} host(s)")

    # Resolve to IPs
    ips: list[str] = []
    failed: list[str] = []

    for host in hosts:
        ip = resolve_host(host)
        if ip:
            ips.append(ip)
        else:
            failed.append(host)

    if failed:
        console.print(
            f"  [yellow]Warning:[/yellow] Could not resolve: {', '.join(failed[:5])}"
            + (f" (and {len(failed) - 5} more)" if len(failed) > 5 else "")
        )

    console.print(f"  [green]Resolved {len(ips)} IP(s)[/green]")
    return ips


def _write_whitelists(config: OberConfig, data: dict[str, list[str]]) -> None:
    """Write whitelist files and update HAProxy ACLs.

    Args:
        config: Ober configuration.
        data: Dictionary of whitelist category to IP list.
    """
    whitelist_dir = config.install_path / "etc" / "haproxy"
    whitelist_dir.mkdir(parents=True, exist_ok=True)

    for category, ips in data.items():
        if not ips:
            continue

        filename = f"{category.replace('_', '-')}.lst"
        filepath = whitelist_dir / filename

        # Write one IP per line
        filepath.write_text("\n".join(sorted(set(ips))) + "\n")
        console.print(f"  Wrote {filepath}")
