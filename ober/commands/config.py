#!/usr/bin/env python3
"""Ober config command - interactive configuration wizard."""

import dataclasses
import hashlib
import ipaddress
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.commands.maintenance import set_maintenance
from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    KeepalivedConfig,
    OberConfig,
    SecurityConfig,
    VIPConfig,
    exit_if_invalid,
    server_name,
    write_atomic,
)
from ober.system import SystemInfo

console = Console()


def get_vip_owner(
    vip: str, nodes: list[str], local_hostname: str, owner: str = ""
) -> tuple[str, int]:
    """Determine which node owns a VIP: the explicit owner, else consistent hashing.

    Args:
        vip: VIP address
        nodes: List of node hostnames/IPs (including self)
        local_hostname: This node's hostname/IP
        owner: Explicit owner from ``vips[].owner`` ("" = md5 rule)

    Returns:
        Tuple of (owner_hostname, priority) where priority is 100 if owner, 90 otherwise
    """
    if not owner:
        sorted_nodes = sorted(nodes)
        # Use deterministic hash (md5) to ensure consistent results across nodes
        vip_hash = int(hashlib.md5(vip.encode()).hexdigest(), 16) % len(sorted_nodes)
        owner = sorted_nodes[vip_hash]

    is_owner = owner == local_hostname
    priority = 100 if is_owner else 90

    return owner, priority


def get_vrrp_router_id(vip: str) -> int:
    """Get VRRP router ID (1-255) from VIP using hash.

    Args:
        vip: VIP address (e.g., "10.0.0.100")

    Returns:
        Router ID between 1 and 255
    """
    # Use deterministic hash (md5) to ensure consistent results across nodes
    return (int(hashlib.md5(vip.encode()).hexdigest(), 16) % 255) + 1


def vrrp_router_ids(vips: list[str]) -> dict[str, int]:
    """Give each VIP a unique ``virtual_router_id`` (1-255), the same on all nodes.

    Over the sorted VIPs, each one takes its md5 ID, or the next free ID on a collision.

    Raises:
        ValueError: More than 255 VIPs.
    """
    ids: dict[str, int] = {}
    for vip in sorted(set(vips)):
        if len(ids) == 255:
            raise ValueError("keepalived supports at most 255 VIPs (virtual_router_id 1-255)")
        vrid = get_vrrp_router_id(vip)
        while vrid in ids.values():
            vrid = vrid % 255 + 1
        ids[vip] = vrid
    return ids


def keepalived_peers(config: OberConfig, local_ip: str) -> list[str]:
    """Peers without duplicates and without this node (one ober.yaml can list all nodes)."""
    return [p for p in dict.fromkeys(config.keepalived.peers) if p != local_ip]


def _parse_hostlist(hostlist: str) -> list[str]:
    """Parse comma-separated hostlist, supporting Slurm hostlist syntax.

    Examples:
        "10.0.0.[1-3]" -> ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        "node[01-03]" -> ["node01", "node02", "node03"]
        "10.0.0.1,10.0.0.2" -> ["10.0.0.1", "10.0.0.2"]
    """
    import re

    hosts = []
    for item in hostlist.split(","):
        item = item.strip()
        if not item:
            continue

        # Check for range syntax [start-end]
        match = re.match(r"^(.*)\[(\d+)-(\d+)\](.*)$", item)
        if match:
            prefix, start, end, suffix = match.groups()
            start_num = int(start)
            end_num = int(end)
            width = len(start)  # Preserve zero-padding

            for i in range(start_num, end_num + 1):
                num_str = str(i).zfill(width)
                hosts.append(f"{prefix}{num_str}{suffix}")
        else:
            hosts.append(item)

    return hosts


def _detect_default_interface() -> str:
    """Detect the default network interface.

    Returns:
        Interface name (e.g., "eth0", "ens3") or first available non-loopback interface
    """
    import re

    from ober.system import run_command

    # Try to get default route interface
    try:
        result = run_command(
            ["ip", "route", "show", "default"],
            check=False,
        )
        if result.returncode == 0:
            match = re.search(r"dev\s+(\S+)", result.stdout)
            if match:
                return match.group(1)
    except Exception:
        pass

    # Fallback: try to find any non-loopback interface
    try:
        result = run_command(
            ["ip", "-o", "link", "show"],
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                match = re.search(r"^\d+:\s+(\S+):", line)
                if match:
                    iface = match.group(1)
                    # Skip loopback and other virtual interfaces
                    if not iface.startswith(("lo", "docker", "veth", "br-")):
                        return iface
    except Exception:
        pass

    # Final fallback
    return "eth0"


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and preview changes without applying. Without a TTY, uses ober.yaml as is.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="No prompts: validate the existing ober.yaml and render all configs from it.",
)
@click.pass_context
def config(ctx: click.Context, dry_run: bool, non_interactive: bool) -> None:
    """Interactive configuration wizard.

    Configures BGP parameters, VIPs, backends, certificates, and other settings.
    Every prompt is prefilled from ober.yaml, so pressing Enter keeps a value.
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root and not dry_run:
        console.print("[red]Error:[/red] Configuration requires root access.")
        console.print("Run with: sudo ober config")
        console.print("Or use --dry-run to preview changes")
        ctx.exit(1)

    # Load existing config or create new; never overwrite a file we cannot read correctly
    config = OberConfig.load()
    exit_if_invalid(config, ctx)

    interactive = not non_interactive and sys.stdin.isatty()
    if not interactive:
        if not dry_run and not non_interactive:
            console.print("[red]Error:[/red] No terminal for the prompts.")
            console.print("Use --non-interactive to render from ober.yaml, or --dry-run.")
            ctx.exit(1)
        if not config.config_path.exists():
            console.print(f"[red]Error:[/red] {config.config_path} not found.")
            ctx.exit(1)
    else:
        try:
            _run_wizard(config, system)
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow]Configuration cancelled.[/yellow]")
            ctx.exit(1)
        errors, _ = config.validate()
        if errors:
            console.print("[red]Error:[/red] The configuration is not valid:")
            for error in errors:
                console.print(f"  - {error}")
            ctx.exit(1)

    # Preview/Apply
    console.print()
    if dry_run:
        console.print("[bold]Configuration Preview (dry-run):[/bold]")
        _print_config_summary(config)
        console.print()
        console.print("[yellow]Dry run - no changes applied.[/yellow]")
        return

    _print_config_summary(config)
    console.print()
    if interactive and not inquirer.confirm("Apply this configuration?", default=True):
        console.print("[yellow]Configuration not applied.[/yellow]")
        return

    try:
        _apply_configuration(config)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        ctx.exit(1)
    console.print()
    console.print("[bold green]Configuration applied![/bold green]")
    console.print()
    _print_config_files(config)
    console.print()
    console.print("Next steps:")
    if config.ha_mode == "bgp":
        console.print("  1. Run [bold]ober test[/bold] to validate BGP connectivity")
    else:
        console.print("  1. Run [bold]ober test[/bold] to validate keepalived configuration")
    console.print("  2. Run [bold]ober start[/bold] to start services")


def _run_wizard(config: OberConfig, system: SystemInfo) -> None:
    """Ask every question, prefilled from ``config``, and update ``config`` in place."""
    console.print()
    console.print("[bold]Ober Configuration Wizard[/bold]")
    console.print()

    console.print("[bold cyan]1. HA Mode Selection[/bold cyan]")
    config.ha_mode = _configure_ha_mode(config.ha_mode)
    config.local_ip = _configure_local_ip(config.local_ip, system.get_local_ip() or "")

    console.print()
    peers = None
    if config.ha_mode == "bgp":
        console.print("[bold cyan]2. BGP Configuration[/bold cyan]")
        config.bgp = _configure_bgp(config.bgp)
    else:
        console.print("[bold cyan]2. Keepalived Configuration[/bold cyan]")
        config.keepalived = _configure_keepalived(config.keepalived)
        peers = config.keepalived.peers

    console.print()
    console.print("[bold cyan]3. VIP Configuration[/bold cyan]")
    vips = _configure_vips(config.vips, config.ha_mode, peers)
    config.vips, config.security = _configure_security(config.security, vips)

    console.print()
    console.print("[bold cyan]4. Backend Configuration[/bold cyan]")
    config.backends = _configure_backends(config.backends)

    console.print()
    console.print("[bold cyan]5. Certificate Configuration[/bold cyan]")
    config.certs = _configure_certs(config.certs, config.certs_path)

    console.print()
    console.print("[bold cyan]6. Additional Settings[/bold cyan]")
    config.log_retention_days, config.stats_port = _configure_additional(
        config.log_retention_days, config.stats_port
    )


def _ip_or_empty(text: str) -> bool:
    """Prompt validator: an IP address or an empty answer."""
    return not text.strip() or _validate_ip(text.strip())


def _configure_local_ip(current: str, detected: str) -> str:
    """Ask for this node's IP (keepalived unicast_src_ip, BGP router-id default)."""
    answer = inquirer.text(
        message=f"This node's IP for VRRP/BGP (empty = auto-detect, now {detected or 'unknown'})",
        default=current,
        validate=lambda _, x: _ip_or_empty(x),
    )
    return str(answer).strip()


def _configure_ha_mode(current: str) -> str:
    """Configure HA mode selection."""
    questions = [
        inquirer.List(
            "ha_mode",
            message="Select High Availability mode",
            choices=[
                ("BGP/ECMP - Use BGP for route announcement (requires BGP router)", "bgp"),
                (
                    "Keepalived/VRRP - Use VRRP for VIP failover (no BGP required)",
                    "keepalived",
                ),
            ],
            default=current,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    return str(answers["ha_mode"])


def _configure_keepalived(current: KeepalivedConfig) -> KeepalivedConfig:
    """Configure keepalived settings."""
    questions = [
        inquirer.Text(
            "peers",
            message="Peer node IPs (comma-separated, Slurm hostlist supported)",
            default=",".join(current.peers),
            validate=lambda _, x: all(_validate_ip(p) for p in _parse_hostlist(x)),
        ),
        inquirer.Text(
            "interface",
            message="Network interface for VIPs (leave empty for auto-detect)",
            default=current.interface,
        ),
        inquirer.Confirm(
            "use_multicast",
            message="Use multicast mode? (default: unicast)",
            default=current.use_multicast,
        ),
        inquirer.Text(
            "advert_int",
            message="Advertisement interval (seconds)",
            default=str(current.advert_int),
            validate=lambda _, x: x.isdigit() and 1 <= int(x) <= 255,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    # Parse peers (support Slurm hostlist format)
    peers = _parse_hostlist(answers["peers"])

    # Validate minimum peers
    if len(peers) < 1:
        console.print(
            "[yellow]Warning:[/yellow] At least 2 total nodes (1 peer) recommended for HA"
        )

    return dataclasses.replace(
        current,
        peers=peers,
        interface=answers["interface"].strip(),
        use_multicast=answers["use_multicast"],
        advert_int=int(answers["advert_int"]),
    )


def _configure_bgp(current: BGPConfig) -> BGPConfig:
    """Configure BGP settings."""
    questions = [
        inquirer.Text(
            "local_as",
            message="Local AS number",
            default=str(current.local_as),
            validate=lambda _, x: x.isdigit() and 1 <= int(x) <= 4294967295,
        ),
        inquirer.Text(
            "peer_as",
            message="Peer AS number (router)",
            default=str(current.peer_as),
            validate=lambda _, x: x.isdigit() and 1 <= int(x) <= 4294967295,
        ),
        inquirer.Text(
            "neighbors",
            message="BGP neighbor IP(s) (comma-separated)",
            default=",".join(current.neighbors),
            validate=lambda _, x: all(_validate_ip(n.strip()) for n in x.split(",") if n.strip()),
        ),
        inquirer.Text(
            "router_id",
            message="Router ID (empty = this node's IP)",
            default=current.router_id,
            validate=lambda _, x: _ip_or_empty(x),
        ),
        inquirer.Text(
            "local_address",
            message="Local address (empty = this node's IP)",
            default=current.local_address,
            validate=lambda _, x: _ip_or_empty(x),
        ),
        inquirer.Text(
            "hold_time",
            message="Hold time (seconds, 0 or 3-65535)",
            default=str(current.hold_time),
            validate=lambda _, x: x.isdigit() and (int(x) == 0 or 3 <= int(x) <= 65535),
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    return dataclasses.replace(
        current,
        local_as=int(answers["local_as"]),
        peer_as=int(answers["peer_as"]),
        neighbors=[n.strip() for n in answers["neighbors"].split(",") if n.strip()],
        router_id=answers["router_id"].strip(),
        local_address=answers["local_address"].strip(),
        hold_time=int(answers["hold_time"]),
    )


def _configure_vips(
    current: list[VIPConfig], ha_mode: str = "bgp", peers: list[str] | None = None
) -> list[VIPConfig]:
    """Configure Virtual IP settings.

    Args:
        current: Current VIP configuration
        ha_mode: HA mode (bgp or keepalived)
        peers: List of peer nodes (for keepalived mode validation)
    """
    vips: list[VIPConfig] = []

    current_vips = ",".join(v.address for v in current) if current else ""
    questions = [
        inquirer.Text(
            "vips",
            message="VIP address(es) (comma-separated, CIDR notation)",
            default=current_vips,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    for vip in answers["vips"].split(","):
        vip = vip.strip()
        if vip:
            # Add /32 if not specified
            if "/" not in vip:
                vip = f"{vip}/32"

            # Validate VIP address
            is_valid, error_msg = _validate_vip(vip)
            if not is_valid:
                console.print(f"[red]Error:[/red] {error_msg}")
                continue

            # Keep exposure/interface (and spelling) of a VIP that was already configured
            previous = next(
                (v for v in current if v.address == vip or f"{v.address}/32" == vip), None
            )
            vips.append(previous or VIPConfig(address=vip))

    # Validate VIP count for keepalived mode
    if ha_mode == "keepalived" and peers is not None:
        total_nodes = len(peers) + 1  # peers + self
        if len(vips) != total_nodes and len(vips) > 0:
            console.print(
                f"[yellow]Warning:[/yellow] You have {len(vips)} VIP(s) but {total_nodes} nodes."
            )
            console.print(
                f"[yellow]For optimal load distribution in keepalived mode, "
                f"configure {total_nodes} VIP(s) (one per node).[/yellow]"
            )

    return vips


def _parse_cidrs(text: str) -> tuple[list[str], list[str]]:
    """Split a comma/space separated list into (valid, invalid) IPs or CIDRs."""
    valid: list[str] = []
    invalid: list[str] = []
    for item in text.replace(",", " ").split():
        try:
            ipaddress.ip_network(item, strict=False)
            valid.append(item)
        except ValueError:
            invalid.append(item)
    return valid, invalid


def _configure_security(
    current: SecurityConfig, vips: list[VIPConfig]
) -> tuple[list[VIPConfig], SecurityConfig]:
    """Ask which VIPs face the Internet and, if any, the hardening inputs."""
    if not vips:
        return vips, current
    answers = inquirer.prompt(
        [
            inquirer.Checkbox(
                "internet",
                message="Which VIPs are Internet-facing? (space to select, none = all internal)",
                choices=[v.address for v in vips],
                default=[v.address for v in vips if v.exposure == "internet"],
            )
        ]
    )
    if answers is None:
        raise KeyboardInterrupt
    for vip in vips:
        vip.exposure = "internet" if vip.address in answers["internet"] else "internal"
    if not answers["internet"]:
        return vips, current

    answers = inquirer.prompt(
        [
            inquirer.Text(
                "hostnames",
                message="S3 hostnames served (comma-separated; empty = accept any Host)",
                default=",".join(current.s3_hostnames),
            ),
            inquirer.Text(
                "allowlist",
                message="Allowed source CIDRs for Internet VIPs (everything else is denied)",
                default=",".join(current.allowlist),
            ),
            inquirer.Text(
                "trusted",
                message="Trusted networks (never rate limited or banned)",
                default=",".join(current.trusted_networks),
            ),
            inquirer.Text(
                "log_target",
                message="ober watch syslog target host:port (empty = log to stdout)",
                default=current.log_target,
                validate=lambda _, x: not x or bool(_LOG_TARGET_RE.match(x.strip())),
            ),
        ]
    )
    if answers is None:
        raise KeyboardInterrupt
    current.s3_hostnames = [h.strip() for h in answers["hostnames"].split(",") if h.strip()]
    for key, attr in (("allowlist", "allowlist"), ("trusted", "trusted_networks")):
        valid, invalid = _parse_cidrs(answers[key])
        if invalid:
            console.print(f"[red]Ignoring invalid {key} entries:[/red] {', '.join(invalid)}")
        setattr(current, attr, valid)
    current.log_target = answers["log_target"].strip()
    return vips, current


def _configure_backends(current: list[BackendConfig]) -> list[BackendConfig]:
    """Configure backend server settings. Existing groups are prefilled, never dropped."""
    if current and inquirer.confirm("Keep the current backends unchanged?", default=True):
        return current
    if not current and not inquirer.confirm("Configure S3/Ceph RGW backends?", default=False):
        return current

    backends: list[BackendConfig] = []
    pending = list(current)
    while True:
        base = pending.pop(0) if pending else BackendConfig(name=f"s3_backend_{len(backends) + 1}")

        backend_name = inquirer.text(message="Backend group name", default=base.name)
        servers_str = inquirer.text(
            message="Backend servers (comma-separated, host:port)",
            default=",".join(base.servers),
        )
        health_path = inquirer.text(message="Health check path", default=base.health_check_path)
        health_interval = inquirer.text(
            message="Health check interval (ms)",
            default=str(base.health_check_interval),
            validate=lambda _, x: x.isdigit() and int(x) >= 100,
        )

        backends.append(
            dataclasses.replace(
                base,
                name=backend_name.strip(),
                servers=[s.strip() for s in servers_str.split(",") if s.strip()],
                health_check_path=health_path.strip(),
                health_check_interval=int(health_interval),
            )
        )

        # Only one backend group is supported, so existing groups are edited but none added
        if not pending:
            break

    return backends


def _configure_certs(current: CertConfig, certs_dir: Path) -> CertConfig:
    """Configure certificate settings.

    Let's Encrypt via Route53 is not offered: no ACME client exists yet, and the option
    produced a node without an S3 frontend.
    """
    answers = inquirer.prompt(
        [
            inquirer.List(
                "cert_method",
                message="Certificate method",
                choices=[
                    ("Provide certificate file path", "file"),
                    ("No certificate (the S3 frontend is not generated)", "none"),
                ],
                default="file" if current.path else "none",
            ),
        ]
    )
    if not answers:
        raise KeyboardInterrupt

    if answers["cert_method"] == "none":
        return dataclasses.replace(current, path="")
    cert_path = inquirer.text(
        message="Certificate file path (PEM format, must include private key)",
        default=current.path or str(certs_dir / "server.pem"),
    )
    return dataclasses.replace(current, path=str(cert_path).strip())


def _print_config_files(config: OberConfig) -> None:
    """Print configuration files and service information."""
    console.print("[cyan]Configuration Files:[/cyan]")
    console.print(f"  Ober config:    {config.config_path}")
    console.print(f"  HAProxy config: {config.haproxy_config_path}")

    if config.ha_mode == "bgp":
        console.print(f"  ExaBGP config:  {config.bgp_config_path}")
    else:
        console.print(f"  Keepalived config: {config.keepalived_config_path}")

    console.print()
    console.print("[cyan]Systemd Services:[/cyan]")
    console.print("  ober-http.service (HAProxy)")

    if config.ha_mode == "bgp":
        console.print("  ober-bgp.service (ExaBGP)")
    else:
        console.print("  ober-ha.service (Keepalived)")


def _configure_additional(log_retention: int, stats_port: int) -> tuple[int, int]:
    """Configure additional settings."""
    questions = [
        inquirer.Text(
            "log_retention",
            message="Log retention (days)",
            default=str(log_retention or 7),
            validate=lambda _, x: x.isdigit() and int(x) >= 1,
        ),
        inquirer.Text(
            "stats_port",
            message="HAProxy stats port",
            default=str(stats_port or 8404),
            validate=lambda _, x: x.isdigit() and 1024 <= int(x) <= 65535,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    return int(answers["log_retention"]), int(answers["stats_port"])


def _validate_ip(ip: str) -> bool:
    """Validate an IP address."""
    try:
        socket.inet_pton(socket.AF_INET, ip)
        return True
    except OSError:
        return False


def _validate_vip(vip: str) -> tuple[bool, str]:
    """Validate a VIP address with optional CIDR notation.

    Args:
        vip: VIP address (e.g., "192.168.1.100" or "192.168.1.100/32")

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Split CIDR notation if present
    if "/" in vip:
        ip_part, cidr_part = vip.rsplit("/", 1)
        try:
            cidr = int(cidr_part)
            if cidr != 32:
                return False, f"Invalid CIDR prefix: /{cidr_part} (must be a single address (/32))"
        except ValueError:
            return False, f"Invalid CIDR prefix: /{cidr_part}"
    else:
        ip_part = vip

    # Validate IP address
    if not _validate_ip(ip_part):
        return False, f"Invalid IP address: {ip_part}"

    return True, ""


def _print_config_summary(config: OberConfig) -> None:
    """Print a summary of the configuration."""
    console.print("[bold]Configuration Summary:[/bold]")
    console.print()

    console.print("[cyan]HA Mode:[/cyan]")
    console.print(f"  Mode: {config.ha_mode}")
    console.print()

    if config.ha_mode == "bgp":
        console.print("[cyan]BGP:[/cyan]")
        console.print(f"  Local AS: {config.bgp.local_as}")
        console.print(f"  Peer AS: {config.bgp.peer_as}")
        console.print(f"  Neighbors: {', '.join(config.bgp.neighbors) or 'none'}")
        console.print(f"  Router ID: {config.bgp.router_id}")
        console.print(f"  Local Address: {config.bgp.local_address}")
        console.print(f"  Hold Time: {config.bgp.hold_time}s")
    else:
        console.print("[cyan]Keepalived:[/cyan]")
        console.print(f"  Peers: {', '.join(config.keepalived.peers) or 'none'}")
        console.print(f"  Interface: {config.keepalived.interface or 'auto-detect'}")
        console.print(f"  Mode: {'multicast' if config.keepalived.use_multicast else 'unicast'}")
        console.print(f"  Advertisement Interval: {config.keepalived.advert_int}s")

    console.print()
    console.print("[cyan]VIPs:[/cyan]")
    if config.vips:
        for vip in config.vips:
            console.print(f"  {vip.address}  [dim]({vip.exposure})[/dim]")
    else:
        console.print("  [dim]none configured[/dim]")

    if any(v.exposure == "internet" for v in config.vips):
        sec = config.security
        console.print()
        console.print("[cyan]Internet exposure:[/cyan]")
        console.print(f"  S3 hostnames: {', '.join(sec.s3_hostnames) or 'any'}")
        console.print(f"  Allowlist: {', '.join(sec.allowlist) or 'empty'}")
        console.print(f"  Trusted networks: {', '.join(sec.trusted_networks) or 'none'}")
        console.print(f"  Syslog target: {sec.log_target or 'stdout (ober watch not fed)'}")
        if sec.autoban_enabled:
            console.print(
                f"  Auto-ban: >{sec.autoban_auth_failures} 401/403 in "
                f"{sec.autoban_window_s}s bans for {sec.autoban_ban_s}s"
            )
        if sec.allowlist_enabled and not sec.allowlist:
            console.print(
                "  [bold yellow]Warning: allowlist is enabled but empty; this denies all "
                "Internet traffic (except trusted networks).[/bold yellow]"
            )

    console.print()
    console.print("[cyan]Backends:[/cyan]")
    if config.backends:
        for backend in config.backends:
            console.print(f"  {backend.name}:")
            for server in backend.servers:
                console.print(f"    - {server}")
        if len(config.backends) > 1:
            console.print(
                "  [bold red]Error: only one backend group is supported; apply will fail."
                "[/bold red]"
            )
    else:
        console.print("  [dim]none configured[/dim]")
        console.print(
            "  [bold red]Error: no backends configured; no S3 frontend can be generated, "
            "so apply will fail.[/bold red]"
        )

    console.print()
    console.print("[cyan]Certificates:[/cyan]")
    if config.certs.path:
        console.print(f"  Path: {config.certs.path}")
    else:
        console.print("  [dim]not configured[/dim]")
        if config.certs.route53_enabled or config.certs.acme_enabled:
            console.print("  [yellow]Note: Let's Encrypt/ACME is not implemented yet.[/yellow]")
        console.print(
            "  [bold red]Error: no certificate path; no S3 frontend can be generated, "
            "so apply will fail.[/bold red]"
        )

    console.print()
    console.print("[cyan]Other:[/cyan]")
    console.print(f"  Log Retention: {config.log_retention_days} days")
    console.print(f"  Stats Port: {config.stats_port}")


def _apply_configuration(
    config: OberConfig,
    systemd_dir: Path = Path("/etc/systemd/system"),
    run_dir: Path = Path("/run/ober"),
    is_root: bool | None = None,
) -> None:
    """Apply the configuration to the system.

    Args:
        config: Configuration to apply.
        systemd_dir: Where unit files and drop-ins live (injectable for tests).
        run_dir: Runtime directory of the HAProxy admin socket (injectable for tests).
        is_root: Override root detection; systemd/run-dir changes happen only as root.
    """
    # Save main config
    config.save()

    # Generate HAProxy config (+ the -f files it references)
    _write_security_lists(config)
    _generate_haproxy_config(config)

    # Generate BGP or Keepalived config based on ha_mode
    if config.ha_mode == "bgp":
        _generate_exabgp_config(config)
    else:
        _generate_keepalived_config(config)
    # BGP mode puts the VIPs on lo-vip; keepalived mode removes it (keepalived adds the VIPs)
    _configure_vip_interface(config)

    if os.geteuid() == 0 if is_root is None else is_root:
        _ensure_runtime_dir(config, systemd_dir, run_dir)


_RUNTIME_DROPIN = """[Service]
RuntimeDirectory=ober
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
"""


def _ensure_runtime_dir(config: OberConfig, systemd_dir: Path, run_dir: Path) -> None:
    """Make the HAProxy admin socket directory exist on nodes bootstrapped before 0.4.

    ``ober upgrade`` never rewrites unit files, so an existing ``ober-http.service`` lacks
    ``RuntimeDirectory=ober`` and HAProxy would fail on ``stats socket /run/ober/...``. A
    drop-in adds it without touching the unit; watch/blocklist units are (re)written too.
    """
    from ober.system import run_command
    from ober.watch.units import write_units

    dropin_dir = systemd_dir / "ober-http.service.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / "10-ober-runtime.conf").write_text(_RUNTIME_DROPIN)

    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o700)

    write_units(config, systemd_dir, role="all")
    run_command(["systemctl", "daemon-reload"], check=False)


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_LOG_TARGET_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]+:\d{1,5}$")

# Per-request sample caps keep one JSON log line far below the 8192 byte syslog limit.
_LOG_VARS_INIT = (
    ("host", 'str("")'),
    ("bucket", 'str("")'),
    ("akey", 'str("")'),
    ("ua", 'str("")'),
    ("presigned", "always_false"),
)


def _write_if_changed(path: Path, text: str) -> None:
    """Write only a changed file, so a no-op apply does not look like a pending reload."""
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    write_atomic(path, text)  # HAProxy or keepalived may start while we write


def _vip_host(address: str) -> str:
    """Return the bare IP of a VIP in CIDR notation."""
    return address.split("/")[0]


def _vip_cidr(address: str) -> str:
    """Return a VIP as ``ip/32`` (netplan rejects a bare address)."""
    return f"{_vip_host(address)}/32"


def _validated_hostnames(names: list[str]) -> list[str]:
    """Lower-case and validate S3 hostnames (they are pasted into haproxy.cfg)."""
    result: list[str] = []
    for name in names:
        name = name.strip().strip(".").lower()
        if not name:
            continue
        if not _HOSTNAME_RE.match(name):
            raise ValueError(f"Invalid S3 hostname: {name!r}")
        result.append(name)
    return result


def _haproxy_log_format(frontend: str) -> str:
    """Build the log-format value: one JSON object whose keys match ``LOG_FIELDS``.

    Uses HAProxy's native ``%{+json}o`` encoding (typed, escaped, empty strings stay
    empty). Only redacted values are logged: bucket (never key or query), the access key
    ID and a presigned flag. They come from ``sess.o_*`` variables set in http-request
    rules (and initialised per connection, so early failures still log valid JSON).
    """
    fields = [
        ("ts", "Ts"),
        ("node", "H"),
        ("frontend", f"[str({frontend})]"),
        ("client_ip", "ci"),
        ("method", "HM"),
        ("host", "[var(sess.o_host)]"),
        ("bucket", "[var(sess.o_bucket)]"),
        ("access_key", "[var(sess.o_akey)]"),
        ("presigned:bool", "[var(sess.o_presigned)]"),
        ("status", "ST"),
        ("bytes_in", "[bytes_in]"),
        ("bytes_out", "[bytes_out]"),
        ("duration_ms", "Tt"),
        ("user_agent", "[var(sess.o_ua)]"),
        ("tls_version", "[ssl_fc_protocol]"),
    ]
    return "%{+json}o " + " ".join(f"%({name}){expr}" for name, expr in fields)


def _conn_log_var_lines() -> list[str]:
    """Initialise log variables per connection (covers failures before the first request)."""
    return [f"    tcp-request connection set-var(sess.o_{n}) {i}" for n, i in _LOG_VARS_INIT]


def _s3_request_lines(
    hostnames: list[str], vip_hosts: list[str], strip_forwarded: bool = False
) -> list[str]:
    """http-request rules that extract the redacted log fields and add proxy headers.

    Nothing here rewrites Host, the path or any header the client may have signed.
    With ``strip_forwarded`` (untrusted clients) client-supplied forwarding headers are
    removed first so the backend only sees the address HAProxy observed.
    """
    lines = [
        "    # Redacted log fields (reset per request: keep-alive connections reuse sess vars)"
    ]
    for name, init in _LOG_VARS_INIT:
        lines.append(f"    http-request set-var(sess.o_{name}) {init}")
    # Cap length, then replace non-printable and non-ASCII bytes (the JSON encoder leaves them raw).
    cap = "bytes(0,{}),regsub([^[:print:]],?,g)".format
    lines += [
        f"    http-request set-var(sess.o_host) req.fhdr(host),field(1,:),lower,{cap(255)}",
        f"    http-request set-var(sess.o_ua) req.fhdr(user-agent),{cap(256)}",
    ]
    # Path-style bucket: first path segment only (never the key or query string), and only
    # when Host is one of our names or VIP addresses. Otherwise the first segment may be an
    # object key of some unrelated request (e.g. a DNS name pointing at us) and is not logged.
    path_hosts = sorted({*hostnames, *vip_hosts})
    if path_hosts:
        lines.append(
            f"    http-request set-var(sess.o_bucket) path,field(2,/),{cap(128)} "
            f"if {{ var(sess.o_host) -m str {' '.join(path_hosts)} }}"
        )
    # Virtual-host bucket: prefix of <bucket>.<s3 hostname>; longest suffix wins (last).
    for name in sorted(hostnames, key=len):
        dotted = name.replace(".", "[.]")
        lines.append(
            f"    http-request set-var(sess.o_bucket) "
            f"var(sess.o_host),regsub([.]{dotted}$,),{cap(128)} "
            f"if {{ var(sess.o_host) -m end .{name} }}"
        )
    lines += [
        # Access key ID, most specific wins: presign query < SigV2 < SigV4/4A header. Each
        # extraction is guarded and cut at the first '/' or ':' so the signature is never kept.
        f"    http-request set-var(sess.o_akey) url_param(X-Amz-Credential),url_dec,field(1,/),"
        f"{cap(128)}",
        f"    http-request set-var(sess.o_akey) url_param(AWSAccessKeyId),url_dec,{cap(128)} "
        "if { url_param(AWSAccessKeyId) -m found }",
        f"    http-request set-var(sess.o_akey) req.fhdr(authorization),word(2,' '),field(1,:),"
        f"{cap(128)} if {{ req.fhdr(authorization) -m reg ^AWS\\ [^:\\ ]+: }}",
        f"    http-request set-var(sess.o_akey) req.fhdr(authorization),word(2,' '),field(2,'='),"
        f"field(1,/),{cap(128)} "
        "if { req.fhdr(authorization) -m reg "
        "^AWS4-(HMAC-SHA256|ECDSA-P256-SHA256)\\ Credential=[^/,\\ ]+/ }",
        # Presigned flag only; the signature value itself is never logged.
        "    http-request set-var(sess.o_presigned) always_true "
        "if { url_param(X-Amz-Signature) -m found } || { url_param(Signature) -m found }",
    ]
    if strip_forwarded:
        lines += [
            "    http-request del-header X-Forwarded-For",
            "    http-request del-header Forwarded",
            "    http-request del-header X-Real-IP",
        ]
    lines += [
        "    option forwardfor",
        "    http-request set-header X-Forwarded-Proto https",
    ]
    return lines


def _internal_path(config: OberConfig) -> Path:
    """Sources allowed on internal VIPs (``config.py`` of the data model is not extended)."""
    return config.install_path / "etc" / "haproxy" / "internal.lst"


def _s3_frontend_internal(
    config: OberConfig, binds: str, backend: str, hostnames: list[str], vip_hosts: list[str]
) -> list[str]:
    """S3 frontend for VIPs with exposure "internal" (trusted clients only)."""
    lines = [
        "# S3 frontend for internal VIPs",
        "frontend s3_internal",
        f"    bind {binds} ssl crt {config.certs.path} alpn http/1.1",
        f"    tcp-request connection reject if {{ src -f {config.blocklist_path} }}",
    ]
    if config.security.internal_networks:
        # Empty internal_networks = no restriction (pre-0.4 behavior)
        lines.append(
            f"    tcp-request connection reject unless {{ src -f {_internal_path(config)} }} "
            f"or {{ src -f {config.trusted_path} }}"
        )
    lines += [
        *_conn_log_var_lines(),
        *_s3_request_lines(hostnames, vip_hosts),
        f'    log-format "{_haproxy_log_format("s3_internal")}"',
        f"    default_backend {backend}",
        "",
    ]
    return lines


def _s3_frontend_internet(
    config: OberConfig, binds: str, backend: str, hostnames: list[str], vip_hosts: list[str]
) -> list[str]:
    """S3 frontend for VIPs with exposure "internet": cheap early rejects, then ACLs."""
    sec = config.security
    trusted = f"{{ src -f {config.trusted_path} }}"
    lines = [
        "# S3 frontend for internet-facing VIPs (hardened)",
        "frontend s3_internet",
        f"    bind {binds} ssl crt {config.certs.path} alpn http/1.1",
        # Cap so Internet clients cannot starve the internal VIPs of the global maxconn
        f"    maxconn {sec.internet_maxconn}",
    ]

    track = sec.autoban_enabled or sec.conn_limit_per_ip > 0
    if track:
        store = []
        expire = 60
        if sec.conn_limit_per_ip > 0:
            store.append("conn_cur")
        if sec.autoban_enabled:
            store += ["gpc(2)", f"gpc_rate(2,{sec.autoban_window_s}s)", "gpt(1)"]
            expire = sec.autoban_ban_s + sec.autoban_window_s
        lines += [
            "    # Per-IP counters: conn_cur; gpc0 = signed failures, gpc1 = unsigned failures",
            "    # (count + rate each); gpt0 = ban expiry (epoch s). ipv6 keys hold IPv4 as ::ffff:a.b.c.d",
            f"    stick-table type ipv6 size 1m expire {expire}s store {','.join(store)}",
        ]

    lines.append("    # 1. Cheap rejects before the TLS handshake")
    lines.append(f"    tcp-request connection reject if {{ src -f {config.blocklist_path} }}")
    if sec.allowlist_enabled:
        lines.append(
            f"    tcp-request connection reject unless {{ src -f {config.allowlist_path} }} "
            f"or {trusted}"
        )
    lines.append("    tcp-request connection set-var(sess.now) date")
    lines += _conn_log_var_lines()
    if track:
        # Only trusted networks are exempt. The allowlist grants access, not immunity:
        # in phase 1 every client is allowlisted. Put campus NAT ranges in both lists.
        lines.append(f"    tcp-request connection track-sc0 src unless {trusted}")
    if sec.autoban_enabled:
        lines.append("    tcp-request connection reject if { sc_get_gpt(0,0),sub(sess.now) gt 0 }")
    if sec.conn_limit_per_ip > 0:
        lines.append(
            f"    tcp-request connection reject if {{ sc_conn_cur(0) gt {sec.conn_limit_per_ip} }}"
        )

    lines.append("    # 2. Redacted log fields and proxy headers")
    if sec.autoban_enabled:
        lines += [
            "    http-request set-var(txn.now) date",
            # Keep-alive connections of a banned IP are cut at the next request.
            "    http-request reject if { sc_get_gpt(0,0),sub(txn.now) gt 0 }",
        ]
    lines += _s3_request_lines(hostnames, vip_hosts, strip_forwarded=True)

    lines.append("    # 3. Policy ACLs (Host/path/signed headers are never rewritten)")
    if hostnames:
        for name in hostnames:
            lines.append(f"    acl s3_host var(sess.o_host) -m str {name}")
            lines.append(f"    acl s3_host var(sess.o_host) -m end .{name}")
        lines.append("    http-request set-var(txn.deny) int(1) unless s3_host")
    lines += [
        "    http-request set-var(txn.deny) int(1) "
        "unless { method GET HEAD PUT POST DELETE OPTIONS }",
    ]
    if sec.deny_admin_api:
        lines += [
            # One anchored regex on the decoded path: //admin, /./admin and /%2fadmin too
            "    http-request set-var(txn.deny) int(1) "
            "if { path,url_dec -i -m reg ^/+([.]/+)*admin(/|$) }",
        ]
    if sec.deny_anonymous:
        lines += [
            "    http-request set-var(txn.deny) int(1) if { req.hdr_cnt(authorization) eq 0 } "
            "!{ url_param(X-Amz-Signature) -m found } !{ url_param(Signature) -m found } "
            "!{ method OPTIONS }",
        ]
    # Several Authorization headers: which one RGW honours is ambiguous. fhdr_cnt counts header
    # lines (hdr_cnt would also split the commas inside a SigV4 value).
    lines.append(
        "    http-request set-var(txn.deny) int(1) if { req.fhdr_cnt(authorization) gt 1 }"
    )
    violation = "{ var(txn.deny) -m found }"
    signed = "{ var(sess.o_akey),length gt 0 }"
    if sec.autoban_enabled:
        lines += [
            "    # Policy denies count as signed (gpc0) or unsigned (gpc1) by the request",
            f"    http-request sc-inc-gpc(0,0) if {violation} {signed}",
            f"    http-request sc-inc-gpc(1,0) if {violation} !{signed}",
            f"    http-request sc-set-gpt(0,0) date({sec.autoban_ban_s}) "
            f"if {violation} {signed} {{ sc_gpc_rate(0,0) gt {sec.autoban_auth_failures} }}",
            f"    http-request sc-set-gpt(0,0) date({sec.autoban_ban_s}) "
            f"if {violation} !{signed} {{ sc_gpc_rate(1,0) gt {sec.autoban_unsigned_failures} }}",
        ]
    lines.append(f"    http-request deny deny_status 403 if {violation}")

    if sec.autoban_enabled:
        denied = "{ status 401 403 }"
        probe = "!{ method HEAD OPTIONS }"
        lines += [
            "    # 4. Auto-ban on backend 401/403. Signed requests (not HEAD/OPTIONS, which",
            "    # legitimately 403) count against autoban_auth_failures; unsigned ones against",
            "    # the much higher autoban_unsigned_failures (many users behind one NAT).",
            f"    http-response sc-inc-gpc(0,0) if {denied} {signed} {probe}",
            f"    http-response sc-inc-gpc(1,0) if {denied} !{signed}",
            f"    http-response sc-set-gpt(0,0) date({sec.autoban_ban_s}) "
            f"if {denied} {signed} {probe} {{ sc_gpc_rate(0,0) gt {sec.autoban_auth_failures} }}",
            f"    http-response sc-set-gpt(0,0) date({sec.autoban_ban_s}) "
            f"if {denied} !{signed} {{ sc_gpc_rate(1,0) gt {sec.autoban_unsigned_failures} }}",
        ]

    lines += [
        f'    log-format "{_haproxy_log_format("s3_internet")}"',
        f"    default_backend {backend}",
        "",
    ]
    return lines


def _valid_networks(entries: list[str], label: str) -> list[str]:
    """Keep syntactically valid IPs/CIDRs; warn about the rest.

    A typo in an allowlist must not stop HAProxy from starting (it refuses to load
    a ``-f`` file with a bad line), so invalid entries are skipped, not written.
    """
    valid: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            console.print(f"[yellow]Warning: skipping invalid {label} entry {entry!r}[/yellow]")
            continue
        valid.append(entry)
    return valid


def _write_security_lists(config: OberConfig) -> None:
    """Write the HAProxy ``-f`` files for the S3 frontends.

    allowlist/trusted/internal are rewritten from config (always, so the files exist even
    when empty). The blocklist is only created when missing: ober watch manages it at runtime.
    """
    config.allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    for path, entries, label in (
        (config.allowlist_path, config.security.allowlist, "allowlist"),
        (config.trusted_path, config.security.trusted_networks, "trusted network"),
        (_internal_path(config), config.security.internal_networks, "internal network"),
    ):
        _write_if_changed(path, "".join(f"{line}\n" for line in _valid_networks(entries, label)))
    if not config.blocklist_path.exists():
        config.blocklist_path.write_text("")


def _generate_haproxy_config(
    config: OberConfig, socket_path: Path | None = None, https_port: int = 443
) -> None:
    """Generate HAProxy configuration file.

    Args:
        config: Ober configuration.
        socket_path: Override for the admin socket path (tests run unprivileged).
        https_port: Port of the S3 frontends (tests cannot bind 443).
    """
    sec = config.security
    sock = socket_path if socket_path is not None else config.admin_socket_path
    if sec.log_target:
        if not _LOG_TARGET_RE.match(sec.log_target):
            raise ValueError(f"Invalid log target (expected host:port): {sec.log_target!r}")
        log_line = f"    log {sec.log_target} len 8192 format rfc5424 local0"
    else:
        log_line = "    log stdout format raw local0 info"
    hostnames = _validated_hostnames(sec.s3_hostnames)
    missing = [
        name
        for name, value in (
            ("VIPs", config.vips),
            ("certificate path", config.certs.path),
            ("backends", config.backends),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"No S3 frontend can be generated: missing {', '.join(missing)}")
    if len(config.backends) > 1:
        # shortcut: one backend group only; add vip.backend when several clusters are needed
        raise ValueError(
            "Only one backend group is supported, found "
            + ", ".join(b.name for b in config.backends)
        )
    backend_name = config.backends[0].name
    # health.py and the keepalived check poll 127.0.0.1, so it is always bound
    stats_addrs = ["127.0.0.1"] + [a for a in config.stats_bind if a != "127.0.0.1"]
    for addr in stats_addrs:
        try:
            ipaddress.ip_address(addr)
        except ValueError as exc:
            raise ValueError(f"Invalid stats_bind address: {addr!r}") from exc

    internet = [v for v in config.vips if v.exposure == "internet"]
    internal = [v for v in config.vips if v.exposure != "internet"]
    if internet and any(ipaddress.ip_address(a).is_unspecified for a in stats_addrs):
        raise ValueError("stats_bind must not be 0.0.0.0 or :: when a VIP has exposure internet")
    # stats/metrics/health have no auth: never answer on an Internet-exposed VIP address
    stats_guard = (
        [
            "    tcp-request connection reject if { dst "
            + " ".join(_vip_host(v.address) for v in internet)
            + " }"
        ]
        if internet
        else []
    )

    cfg_lines = [
        "# Herr Ober HAProxy Configuration",
        "# Generated by ober config",
        "",
        "global",
        log_line,
        "    maxconn 100000",
        f"    stats socket {sock} mode 600 level admin",
        f"    server-state-file {config.server_state_path}",
        "    ssl-default-bind-options ssl-min-ver TLSv1.2",
        "    ssl-default-bind-ciphersuites "
        "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256",
        "    ssl-default-bind-ciphers "
        "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
        "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
        "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305",
        "    # Session tickets stay on; the cache serves clients that cannot use tickets",
        "    tune.ssl.cachesize 200000",
        "",
        "defaults",
        "    mode http",
        "    load-server-state-from-file global",
        "    log global",
        "    option dontlognull",
        "    timeout connect 5s",
        f"    timeout client {sec.timeout_client_s}s",
        f"    timeout server {sec.timeout_server_s}s",
        "    timeout http-request 10s",
        "    timeout http-keep-alive 10s",
        "",
        f"# Stats endpoint on port {config.stats_port}",
        "frontend stats",
        "    bind " + ",".join(f"{a}:{config.stats_port}" for a in stats_addrs),
        "    no log",
        *stats_guard,
        "    stats enable",
        "    stats uri /stats",
        "    stats refresh 10s",
        "    monitor-uri /health",
        # /health fails when no backend server can serve S3
        f"    monitor fail if {{ nbsrv({backend_name}) lt 1 }}",
        "    http-request use-service prometheus-exporter if { path /metrics }",
        "",
    ]

    if internal:
        hosts = [_vip_host(v.address) for v in internal]
        binds = ",".join(f"{h}:{https_port}" for h in hosts)
        cfg_lines += _s3_frontend_internal(config, binds, backend_name, hostnames, hosts)
    if internet:
        hosts = [_vip_host(v.address) for v in internet]
        binds = ",".join(f"{h}:{https_port}" for h in hosts)
        cfg_lines += _s3_frontend_internet(config, binds, backend_name, hostnames, hosts)

    # Backends
    for backend in config.backends:
        cfg_lines.extend(
            [
                f"backend {backend.name}",
                "    balance leastconn",
                f"    option httpchk GET {backend.health_check_path}",
            ]
        )
        dflt = (
            f"    default-server check inter {backend.health_check_interval}ms"
            f" fall {backend.fall} rise {backend.rise}"
        )
        if backend.slowstart_s:
            dflt += f" slowstart {backend.slowstart_s}s"
        if backend.maxconn:  # requests over the limit wait in the queue (timeout queue)
            dflt += f" maxconn {backend.maxconn}"
        cfg_lines.append(dflt)
        for server in backend.servers:
            cfg_lines.append(f"    server {server_name(server)} {server}")
        cfg_lines.append("")

    config.haproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(config.haproxy_config_path, "\n".join(cfg_lines))


def _generate_exabgp_config(config: OberConfig) -> None:
    """Generate ExaBGP configuration file."""
    cfg_lines = [
        "# Herr Ober ExaBGP Configuration",
        "# Generated by ober config",
        "",
        "process announce-routes {",
        f"    run {sys.executable} -m ober.commands.health;",
        "    encoder text;",
        "}",
        "",
    ]

    for neighbor in config.bgp.neighbors:
        cfg_lines.extend(
            [
                f"neighbor {neighbor} {{",
                # 3.8: explicit values, else the node's local_ip override, else auto-detect
                f"    router-id {config.bgp.router_id or config.node_ip()};",
                f"    local-address {config.bgp.local_address or config.node_ip()};",
                f"    local-as {config.bgp.local_as};",
                f"    peer-as {config.bgp.peer_as};",
                f"    hold-time {config.bgp.hold_time};",
                "",
                "    family {",
                "        ipv4 unicast;",
                "    }",
                "",
                # ExaBGP has no BFD: bgp.bfd_enabled is ignored, failure detection is hold-time
                "    api {",
                "        processes [ announce-routes ];",
                "    }",
                "}",
                "",
            ]
        )

    config.bgp_config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(config.bgp_config_path, "\n".join(cfg_lines))


def _generate_keepalived_config(config: OberConfig) -> None:
    """Generate keepalived configuration file."""
    from ober.system import SystemInfo

    system = SystemInfo()
    # 3.8: the local_ip override wins over the default-route source address
    local_ip = config.local_ip or system.get_local_ip() or "0.0.0.0"
    hostname = system.hostname

    # Detect interface if not specified
    interface = config.keepalived.interface
    if not interface:
        interface = _detect_default_interface()

    # Build all nodes list (self + peers); the same list on every node gives one owner per VIP
    peers = keepalived_peers(config, local_ip)
    all_nodes = [local_ip, *peers]

    cfg_lines = [
        "# Herr Ober Keepalived Configuration",
        "# Generated by ober config",
        "",
        "global_defs {",
        f"    router_id ober_{hostname}",
        "    vrrp_skip_check_adv_addr",
        "    vrrp_garp_master_delay 1",
        # keepalived -t fails on track scripts without script security
        "    enable_script_security",
        "    script_user root",
        "}",
        "",
        "# Health check script (Python entry point, exit 0 = healthy)",
        "vrrp_script chk_haproxy {",
        f'    script "{sys.executable} -m ober.commands.health --once --timeout 1'
        f' --port {config.stats_port}"',
        "    interval 2",
        "    timeout 3",
        "    weight -50  # Reduce priority by 50 if health check fails",
        "    fall 2      # Require 2 failures before marking down",
        "    rise 2      # Require 2 successes before marking up",
        "}",
        "",
        "# Maintenance flag (ober maintenance on|off): 1 x -100 drops priority below all peers",
        "track_file ober_maintenance {",
        f'    file "{config.maintenance_path}"',
        "    weight -100",
        "}",
        "",
    ]
    track_scripts = ["chk_haproxy"]
    if config.keepalived.gateway_check:
        gateway = str(ipaddress.ip_address(config.keepalived.gateway_check))
        track_scripts.append("chk_gateway")
        cfg_lines.extend(
            [
                "# Gateway reachability: an isolated node goes to FAULT and releases its VIPs",
                "vrrp_script chk_gateway {",
                f'    script "/usr/bin/ping -c 1 -W 1 {gateway}"',
                "    interval 2",
                "    timeout 2",
                "    weight 0  # 0 = FAULT state on failure",
                "    fall 3",
                "    rise 2",
                "}",
                "",
            ]
        )
    notify = (
        f"{sys.executable} -m ober.commands.maintenance vrrp-notify"
        f" --state-file {config.vrrp_state_path}"
    )

    # Generate one vrrp_instance per VIP
    router_ids = vrrp_router_ids([_vip_host(v.address) for v in config.vips])
    for idx, vip_config in enumerate(config.vips):
        vip = _vip_host(vip_config.address)
        owner, priority = get_vip_owner(vip, all_nodes, local_ip, vip_config.owner)
        router_id = router_ids[vip]

        cfg_lines.extend(
            [
                f"# VIP {idx + 1}: {vip} (owner: {owner})",
                f"vrrp_instance VI_{idx + 1} {{",
                "    state BACKUP  # All start as BACKUP, preempt determines master",
                f"    interface {interface}",
                f"    virtual_router_id {router_id}",
                f"    priority {priority}",
                f"    advert_int {config.keepalived.advert_int}",
                "    # a recovering owner waits 30 s before it takes the VIP back",
                "    preempt_delay 30",
                "",
            ]
        )

        # Unicast or multicast mode
        if config.keepalived.use_multicast:
            cfg_lines.append("    # Multicast mode (default 224.0.0.18)")
        else:
            cfg_lines.extend(
                [
                    "    # Unicast mode",
                    f"    unicast_src_ip {local_ip}",
                    "    unicast_peer {",
                ]
            )
            for peer in peers:
                cfg_lines.append(f"        {peer}")
            cfg_lines.extend(
                [
                    "    }",
                    "",
                ]
            )

        cfg_lines.extend(
            [
                "    # Health check - demote priority if HAProxy is down",
                "    track_script {",
                *(f"        {name}" for name in track_scripts),
                "    }",
                "",
                "    track_file {",
                "        ober_maintenance",
                "    }",
                "",
                "    # Write VRRP state for ober status",
                f'    notify "{notify}"',
                "",
                "    virtual_ipaddress {",
                f"        {vip}/32 dev {interface}",
                "    }",
                "}",
                "",
            ]
        )

    config.keepalived_config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(config.keepalived_config_path, "\n".join(cfg_lines))
    # keepalived ignores a track_file that does not exist when it parses the config
    if not config.maintenance_path.exists():
        set_maintenance(config.maintenance_path, False)


def _netplan_vip_yaml(config: OberConfig) -> str:
    """Return the netplan file that puts all VIPs on the ``lo-vip`` dummy device."""
    import yaml

    netplan_cfg = {
        "network": {
            "version": 2,
            "dummy-devices": {"lo-vip": {"addresses": [_vip_cidr(v.address) for v in config.vips]}},
        }
    }
    return yaml.dump(netplan_cfg, default_flow_style=False)


def _networkd_vip_files(config: OberConfig) -> dict[str, str]:
    """Return the systemd-networkd files (name -> text) for the ``lo-vip`` dummy device.

    The ``.network`` file matches only ``lo-vip``, so networkd does not touch other links
    (on Amazon Linux, amazon-ec2-net-utils writes the configs for the ENIs).
    """
    addresses = "".join(f"Address={_vip_cidr(v.address)}\n" for v in config.vips)
    return {
        "60-lo-vip.netdev": "[NetDev]\nName=lo-vip\nKind=dummy\n",
        "60-lo-vip.network": (
            "[Match]\nName=lo-vip\n\n"
            "[Link]\nRequiredForOnline=no\n\n"
            f"[Network]\nLinkLocalAddressing=no\n{addresses}"
        ),
    }


def _configure_vip_interface(
    config: OberConfig,
    netplan_path: Path = Path("/etc/netplan/60-vip.yaml"),
    networkd_dir: Path = Path("/etc/systemd/network"),
) -> None:
    """Configure the VIP dummy interface.

    Debian uses netplan and RHEL uses nmcli. When that tool is missing (Amazon Linux has
    no NetworkManager), systemd-networkd is used.

    Raises:
        RuntimeError: If no tool is found or a command fails.
    """
    from ober.system import OSFamily, SystemInfo, check_command_exists, run_command

    system = SystemInfo()

    if system.os_family == OSFamily.UNKNOWN:
        return
    if config.ha_mode != "bgp" or not config.vips:
        # A lo-vip left from BGP mode keeps every VIP on every node and breaks VRRP failover
        from ober.commands.uninstall import _remove_vip_interface

        _remove_vip_interface(system, networkd_dir, netplan_path)
        return

    tool = "netplan" if system.os_family == OSFamily.DEBIAN else "nmcli"
    if not check_command_exists(tool):
        if not check_command_exists("networkctl"):
            raise RuntimeError(
                f"{tool} or networkctl not found: one is needed to put the VIPs on lo-vip"
            )
        tool = "networkctl"

    try:
        if tool == "netplan":
            netplan_path.write_text(_netplan_vip_yaml(config))
            netplan_path.chmod(0o600)  # netplan warns about world-readable files
            run_command(["netplan", "apply"])
        elif tool == "nmcli":
            exists = run_command(["nmcli", "connection", "show", "lo-vip"], check=False)
            if exists.returncode != 0:
                run_command(
                    ["nmcli", "connection", "add", "type", "dummy"]
                    + ["ifname", "lo-vip", "con-name", "lo-vip"]
                )
            # Set the full list, so removed VIPs go away and nothing is added twice
            addresses = ",".join(_vip_cidr(v.address) for v in config.vips)
            run_command(
                ["nmcli", "connection", "modify", "lo-vip", "ipv4.method", "manual"]
                + ["ipv4.addresses", addresses]
            )
            run_command(["nmcli", "connection", "up", "lo-vip"])
        else:
            networkd_dir.mkdir(parents=True, exist_ok=True)
            for name, text in _networkd_vip_files(config).items():
                (networkd_dir / name).write_text(text)
                (networkd_dir / name).chmod(0o644)
            active = run_command(
                ["systemctl", "is-active", "--quiet", "systemd-networkd"], check=False
            )
            run_command(["systemctl", "enable", "--now", "systemd-networkd"])
            if active.returncode == 0:
                # reload creates a new netdev and reapplies changed .network files
                run_command(["networkctl", "reload"])
            else:
                # enable pulls in wait-online (Also=); with only lo-vip it waits ~2 min at boot
                run_command(
                    ["systemctl", "disable", "systemd-networkd-wait-online.service"], check=False
                )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"VIP interface setup failed: {' '.join(exc.cmd)}: {detail}") from exc
