#!/usr/bin/env python3
"""Ober config command - interactive configuration wizard."""

import hashlib
import ipaddress
import os
import re
import socket
import sys
from pathlib import Path

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    KeepalivedConfig,
    OberConfig,
    SecurityConfig,
    VIPConfig,
)
from ober.system import SystemInfo

console = Console()


def _get_aws_credentials_path() -> Path:
    """Get path to AWS credentials file, checking both current user and sudo user."""
    # First try current user's home
    current_creds = Path.home() / ".aws" / "credentials"
    if current_creds.exists():
        return current_creds

    # If running via sudo, try the original user's home
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd

        try:
            user_info = pwd.getpwnam(sudo_user)
            sudo_creds = Path(user_info.pw_dir) / ".aws" / "credentials"
            if sudo_creds.exists():
                return sudo_creds
        except KeyError:
            pass

    # Fallback to standard location
    return Path.home() / ".aws" / "credentials"


def get_vip_owner(vip: str, nodes: list[str], local_hostname: str) -> tuple[str, int]:
    """Determine which node owns a VIP using consistent hashing.

    Args:
        vip: VIP address
        nodes: List of node hostnames/IPs (including self)
        local_hostname: This node's hostname/IP

    Returns:
        Tuple of (owner_hostname, priority) where priority is 100 if owner, 90 otherwise
    """
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
    help="Validate and preview changes without applying.",
)
@click.pass_context
def config(ctx: click.Context, dry_run: bool) -> None:
    """Interactive configuration wizard.

    Configures BGP parameters, VIPs, backends, certificates, and other settings.
    Can be run multiple times to update configuration (idempotent).
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    if not system.is_root and not dry_run:
        console.print("[red]Error:[/red] Configuration requires root access.")
        console.print("Run with: sudo ober config")
        console.print("Or use --dry-run to preview changes")
        ctx.exit(1)

    # Load existing config or create new
    config = OberConfig.load()

    console.print()
    console.print("[bold]Ober Configuration Wizard[/bold]")
    console.print()

    # Auto-detect local IP
    local_ip = system.get_local_ip() or ""

    try:
        # Section 0: HA Mode Selection
        console.print("[bold cyan]1. HA Mode Selection[/bold cyan]")
        ha_mode = _configure_ha_mode(config.ha_mode)
        config.ha_mode = ha_mode

        # Section 1: BGP or Keepalived Configuration (conditional)
        console.print()
        if ha_mode == "bgp":
            console.print("[bold cyan]2. BGP Configuration[/bold cyan]")
            bgp_config = _configure_bgp(config.bgp, local_ip)
            config.bgp = bgp_config
        else:
            console.print("[bold cyan]2. Keepalived Configuration[/bold cyan]")
            ka_config = _configure_keepalived(config.keepalived, local_ip, system.hostname)
            config.keepalived = ka_config

        # Section 3: VIP Configuration
        console.print()
        console.print("[bold cyan]3. VIP Configuration[/bold cyan]")
        # Pass peers for validation if in keepalived mode
        peers = ka_config.peers if ha_mode == "keepalived" else None
        vips = _configure_vips(config.vips, ha_mode, peers)
        config.vips, config.security = _configure_security(config.security, vips)

        # Section 4: Backend Configuration
        console.print()
        console.print("[bold cyan]4. Backend Configuration[/bold cyan]")
        backends = _configure_backends(config.backends)
        config.backends = backends

        # Section 5: Certificate Configuration
        console.print()
        console.print("[bold cyan]5. Certificate Configuration[/bold cyan]")
        certs = _configure_certs(config.certs)
        config.certs = certs

        # Section 6: Additional Settings
        console.print()
        console.print("[bold cyan]6. Additional Settings[/bold cyan]")
        config.log_retention_days, config.stats_port = _configure_additional(
            config.log_retention_days, config.stats_port
        )

    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Configuration cancelled.[/yellow]")
        ctx.exit(1)

    # Preview/Apply
    console.print()
    if dry_run:
        console.print("[bold]Configuration Preview (dry-run):[/bold]")
        _print_config_summary(config)
        console.print()
        console.print("[yellow]Dry run - no changes applied.[/yellow]")
    else:
        _print_config_summary(config)
        console.print()

        confirm = inquirer.confirm(
            "Apply this configuration?",
            default=True,
        )
        if confirm:
            _apply_configuration(config)
            console.print()
            console.print("[bold green]Configuration applied![/bold green]")
            console.print()
            _print_config_files(config)
            console.print()
            console.print("Next steps:")
            if config.ha_mode == "bgp":
                console.print("  1. Run [bold]ober test[/bold] to validate BGP connectivity")
            else:
                console.print(
                    "  1. Run [bold]ober test[/bold] to validate keepalived configuration"
                )
            console.print("  2. Run [bold]ober start[/bold] to start services")
        else:
            console.print("[yellow]Configuration not applied.[/yellow]")


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


def _configure_keepalived(
    current: KeepalivedConfig, _local_ip: str, _hostname: str
) -> KeepalivedConfig:
    """Configure keepalived settings."""
    questions = [
        inquirer.Text(
            "peers",
            message="Peer node IPs (comma-separated, Slurm hostlist supported)",
            default=",".join(current.peers) if current.peers else "",
        ),
        inquirer.Text(
            "interface",
            message="Network interface for VIPs (leave empty for auto-detect)",
            default=current.interface or "",
        ),
        inquirer.Confirm(
            "use_multicast",
            message="Use multicast mode? (default: unicast)",
            default=current.use_multicast,
        ),
        inquirer.Text(
            "advert_int",
            message="Advertisement interval (seconds)",
            default=str(current.advert_int or 1),
            validate=lambda _, x: x.isdigit() and int(x) >= 1,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    # Parse peers (support Slurm hostlist format)
    peers_str = answers["peers"]
    peers = _parse_hostlist(peers_str)

    # Validate minimum peers
    if len(peers) < 1:
        console.print(
            "[yellow]Warning:[/yellow] At least 2 total nodes (1 peer) recommended for HA"
        )

    return KeepalivedConfig(
        peers=peers,
        interface=answers["interface"],
        use_multicast=answers["use_multicast"],
        advert_int=int(answers["advert_int"]),
    )


def _configure_bgp(current: BGPConfig, local_ip: str) -> BGPConfig:
    """Configure BGP settings."""
    questions = [
        inquirer.Text(
            "local_as",
            message="Local AS number",
            default=str(current.local_as or 65001),
            validate=lambda _, x: x.isdigit() and 1 <= int(x) <= 4294967295,
        ),
        inquirer.Text(
            "peer_as",
            message="Peer AS number (router)",
            default=str(current.peer_as or 65000),
            validate=lambda _, x: x.isdigit() and 1 <= int(x) <= 4294967295,
        ),
        inquirer.Text(
            "neighbors",
            message="BGP neighbor IP(s) (comma-separated)",
            default=",".join(current.neighbors) if current.neighbors else "",
        ),
        inquirer.Text(
            "router_id",
            message="Router ID",
            default=current.router_id or local_ip,
        ),
        inquirer.Text(
            "local_address",
            message="Local address",
            default=current.local_address or local_ip,
        ),
        inquirer.Text(
            "hold_time",
            message="Hold time (seconds)",
            default=str(current.hold_time or 3),
            validate=lambda _, x: x.isdigit() and int(x) >= 1,
        ),
        inquirer.Confirm(
            "bfd_enabled",
            message="Enable BFD (Bidirectional Forwarding Detection)?",
            default=current.bfd_enabled,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    # Parse neighbors
    neighbors = [n.strip() for n in answers["neighbors"].split(",") if n.strip()]

    # Validate neighbors
    for neighbor in neighbors:
        if not _validate_ip(neighbor):
            console.print(f"[yellow]Warning:[/yellow] Invalid IP: {neighbor}")

    return BGPConfig(
        local_as=int(answers["local_as"]),
        peer_as=int(answers["peer_as"]),
        neighbors=neighbors,
        router_id=answers["router_id"],
        local_address=answers["local_address"],
        hold_time=int(answers["hold_time"]),
        bfd_enabled=answers["bfd_enabled"],
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

            # Keep exposure/interface of a VIP that was already configured
            previous = next((v for v in current if v.address == vip), None)
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
    """Configure backend server settings."""
    backends: list[BackendConfig] = []

    # Ask if user wants to configure backends
    has_backends = inquirer.confirm(
        "Configure S3/Ceph RGW backends?",
        default=len(current) > 0,
    )

    if not has_backends:
        return backends

    # Configure backend groups
    while True:
        backend_name = inquirer.text(
            message="Backend group name",
            default=f"s3_backend_{len(backends) + 1}",
        )

        servers_str = inquirer.text(
            message="Backend servers (comma-separated, host:port)",
            default="",
        )

        servers = [s.strip() for s in servers_str.split(",") if s.strip()]

        health_path = inquirer.text(
            message="Health check path",
            default="/",
        )

        health_interval = inquirer.text(
            message="Health check interval (ms)",
            default="1000",
            validate=lambda _, x: x.isdigit() and int(x) >= 100,
        )

        backends.append(
            BackendConfig(
                name=backend_name,
                servers=servers,
                health_check_path=health_path,
                health_check_interval=int(health_interval),
            )
        )

        add_more = inquirer.confirm(
            "Add another backend group?",
            default=False,
        )
        if not add_more:
            break

    return backends


def _configure_certs(current: CertConfig) -> CertConfig:
    """Configure certificate settings."""
    # Determine default based on current config
    if current.path:
        default_method = "file"
    elif current.route53_enabled:
        default_method = "route53"
    elif current.acme_enabled:
        default_method = "acme"
    else:
        default_method = "skip"

    questions = [
        inquirer.List(
            "cert_method",
            message="Certificate method",
            choices=[
                ("Provide certificate file path", "file"),
                ("Let's Encrypt via Route53 DNS-01", "route53"),
                ("Skip certificate configuration", "skip"),
            ],
            default=default_method,
        ),
    ]

    answers = inquirer.prompt(questions)
    if not answers:
        raise KeyboardInterrupt

    if answers["cert_method"] == "file":
        # Get default cert path from config
        from ober.config import OberConfig

        config = OberConfig.load()
        default_cert_path = str(config.certs_path / "server.pem")

        cert_path = inquirer.text(
            message="Certificate file path (PEM format, must include private key)",
            default=current.path or default_cert_path,
        )
        return CertConfig(path=cert_path)

    elif answers["cert_method"] == "route53":
        # Ensure boto3 is installed
        if not _ensure_boto3_installed():
            console.print("[red]Error:[/red] Could not install boto3. Please install manually:")
            console.print("  pip install boto3")
            return CertConfig()
        return _configure_route53_acme(current)

    return CertConfig()


def _ensure_boto3_installed() -> bool:
    """Ensure boto3 is installed, install if needed."""
    import subprocess
    import sys

    # Check if boto3 is already available
    try:
        import boto3  # type: ignore[import-untyped] # noqa: F401

        console.print("[green]boto3 already installed[/green]")
        return True
    except ImportError:
        pass

    # Try to install boto3
    console.print("[yellow]Installing boto3...[/yellow]")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "boto3"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            console.print("[green]boto3 installed successfully[/green]")

            # Verify installation by importing
            try:
                import boto3 as _boto3  # type: ignore[import-untyped] # noqa: F401

                console.print("[green]boto3 import verified[/green]")
                return True
            except ImportError as e:
                console.print(f"[red]boto3 installed but import failed: {e}[/red]")
                return False
        else:
            console.print("[red]Failed to install boto3[/red]")
            console.print(
                f"[yellow]Command:[/yellow] {sys.executable} -m pip install --upgrade boto3"
            )
            if result.stdout:
                console.print("[yellow]stdout:[/yellow]")
                console.print(result.stdout)
            if result.stderr:
                console.print("[yellow]stderr:[/yellow]")
                console.print(result.stderr)
            return False
    except Exception as e:
        console.print(f"[red]Error installing boto3: {e}[/red]")
        return False


def _configure_route53_acme(current: CertConfig) -> CertConfig:
    """Configure Let's Encrypt with Route53 DNS-01 challenge."""
    # Prompt for AWS profile
    aws_profile = inquirer.text(
        message="AWS profile name",
        default=current.route53_profile or "default",
    )

    # Try to list hosted zones with profile
    hosted_zones = _list_route53_hosted_zones(aws_profile)

    # If profile failed, prompt for manual credential entry
    if not hosted_zones:
        console.print("[yellow]Warning:[/yellow] Could not find AWS profile or credentials.")
        use_manual = inquirer.confirm(
            "Enter credentials manually?",
            default=True,
        )

        if use_manual:
            aws_access_key = inquirer.text(
                message="AWS Access Key ID",
            )
            aws_secret_key = inquirer.password(
                message="AWS Secret Access Key",
            )
            aws_region = inquirer.text(
                message="AWS Region (e.g., us-east-1)",
                default="us-east-1",
            )

            # Try to list zones with manual credentials
            hosted_zones = _list_route53_hosted_zones_with_creds(
                aws_access_key, aws_secret_key, aws_region
            )

            if not hosted_zones:
                console.print("[red]Error:[/red] Could not authenticate with provided credentials.")

    # Now select/enter zone and domain
    if hosted_zones:
        # Build choices from hosted zones
        zone_choices = [(f"{z['Name']} ({z['Id']})", z["Id"]) for z in hosted_zones]

        if len(hosted_zones) == 1:
            # Only one zone, use it directly
            hosted_zone_id = hosted_zones[0]["Id"]
            zone_name = hosted_zones[0]["Name"].rstrip(".")
            console.print(f"Using hosted zone: {zone_name} ({hosted_zone_id})")
        else:
            # Multiple zones, prompt user to select
            selected = inquirer.list_input(
                message="Select Route53 hosted zone",
                choices=zone_choices,
                default=current.route53_hosted_zone_id if current.route53_hosted_zone_id else None,
            )
            hosted_zone_id = selected

        # Get the domain name from selected zone
        selected_zone = next((z for z in hosted_zones if z["Id"] == hosted_zone_id), None)
        zone_name = selected_zone["Name"].rstrip(".") if selected_zone else ""

        domain = inquirer.text(
            message="Domain name for certificate",
            default=current.acme_domain or zone_name,
        )
    else:
        # No zones found - get manual input
        hosted_zone_id = inquirer.text(
            message="Route53 Hosted Zone ID (e.g., Z1234567890ABC)",
            default=current.route53_hosted_zone_id,
        )
        domain = inquirer.text(
            message="Domain name for certificate",
            default=current.acme_domain,
        )

    acme_email = inquirer.text(
        message="Email for Let's Encrypt notifications",
        default=current.acme_email,
    )

    return CertConfig(
        acme_enabled=True,
        acme_email=acme_email,
        acme_domain=domain,
        route53_enabled=True,
        route53_profile=aws_profile,
        route53_hosted_zone_id=hosted_zone_id,
    )


def _list_route53_hosted_zones(profile: str) -> list[dict[str, str]]:
    """List Route53 hosted zones using AWS CLI or boto3."""
    import subprocess

    # Try using AWS CLI first (doesn't require boto3 installed)
    try:
        result = subprocess.run(
            ["aws", "route53", "list-hosted-zones", "--profile", profile, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            import json

            data = json.loads(result.stdout)
            zones = []
            for zone in data.get("HostedZones", []):
                # Extract just the zone ID (remove /hostedzone/ prefix)
                zone_id = zone["Id"].replace("/hostedzone/", "")
                zones.append(
                    {
                        "Id": zone_id,
                        "Name": zone["Name"],
                    }
                )
            console.print(f"[green]Found {len(zones)} hosted zone(s) via AWS CLI[/green]")
            return zones
        else:
            console.print(f"[yellow]AWS CLI failed:[/yellow] {result.stderr}")
    except FileNotFoundError:
        console.print("[yellow]AWS CLI not found in PATH[/yellow]")
    except subprocess.TimeoutExpired:
        console.print("[yellow]AWS CLI timeout[/yellow]")
    except Exception as e:
        console.print(f"[yellow]AWS CLI error: {e}[/yellow]")

    # Fallback: try boto3 if available
    console.print("[yellow]Trying boto3...[/yellow]")
    try:
        import boto3  # type: ignore[import-untyped]
        from botocore.session import Session as BotocoreSession  # type: ignore[import-untyped]

        # Use explicit credentials file path to handle sudo scenarios
        creds_file = _get_aws_credentials_path()
        console.print(f"[yellow]Using credentials from: {creds_file}[/yellow]")

        # Create botocore session pointing to the right credentials file
        botocore_session = BotocoreSession()
        botocore_session.set_config_variable("credentials_file", str(creds_file))

        session = boto3.Session(profile_name=profile, botocore_session=botocore_session)
        client = session.client("route53")
        response = client.list_hosted_zones()
        zones = []
        for zone in response.get("HostedZones", []):
            zone_id = zone["Id"].replace("/hostedzone/", "")
            zones.append(
                {
                    "Id": zone_id,
                    "Name": zone["Name"],
                }
            )
        console.print(f"[green]Found {len(zones)} hosted zone(s) via boto3[/green]")
        return zones
    except Exception as e:
        console.print(f"[yellow]boto3 error: {e}[/yellow]")

    console.print("[yellow]Warning:[/yellow] Could not list Route53 hosted zones.")
    console.print("[yellow]You can enter the hosted zone ID manually.[/yellow]")
    return []


def _list_route53_hosted_zones_with_creds(
    access_key: str, secret_key: str, region: str
) -> list[dict[str, str]]:
    """List Route53 hosted zones using explicit AWS credentials."""
    try:
        import boto3  # type: ignore[import-untyped]

        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        client = session.client("route53")
        response = client.list_hosted_zones()
        zones = []
        for zone in response.get("HostedZones", []):
            zone_id = zone["Id"].replace("/hostedzone/", "")
            zones.append(
                {
                    "Id": zone_id,
                    "Name": zone["Name"],
                }
            )
        console.print(f"[green]Found {len(zones)} hosted zone(s) with manual credentials[/green]")
        return zones
    except Exception as e:
        console.print(f"[yellow]boto3 error with manual credentials: {e}[/yellow]")

    return []


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
            if not (0 <= cidr <= 32):
                return False, f"Invalid CIDR prefix: /{cidr_part} (must be 0-32)"
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
        console.print(f"  BFD: {'enabled' if config.bgp.bfd_enabled else 'disabled'}")
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
                f"  [yellow]Note: only the first backend ({config.backends[0].name}) "
                "receives S3 traffic.[/yellow]"
            )
    else:
        console.print("  [dim]none configured[/dim]")
        console.print(
            "  [bold yellow]Warning: no backends configured; the S3 frontend will NOT be "
            "generated and HAProxy will serve no S3 traffic.[/bold yellow]"
        )

    console.print()
    console.print("[cyan]Certificates:[/cyan]")
    if config.certs.path:
        console.print(f"  Path: {config.certs.path}")
    elif config.certs.route53_enabled:
        console.print("  Method: Let's Encrypt (Route53 DNS-01)")
        console.print(f"  Domain: {config.certs.acme_domain}")
        console.print(f"  AWS Profile: {config.certs.route53_profile}")
        console.print(f"  Hosted Zone: {config.certs.route53_hosted_zone_id}")
        console.print(f"  Email: {config.certs.acme_email}")
    elif config.certs.acme_enabled:
        console.print(f"  ACME: enabled ({config.certs.acme_email})")
    else:
        console.print("  [dim]not configured[/dim]")

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
        # Configure VIP interface (only for BGP mode - keepalived manages VIPs itself)
        _configure_vip_interface(config)
    else:
        _generate_keepalived_config(config)

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


def _vip_host(address: str) -> str:
    """Return the bare IP of a VIP in CIDR notation."""
    return address.split("/")[0]


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
        path.write_text("".join(f"{line}\n" for line in _valid_networks(entries, label)))
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

    internet = [v for v in config.vips if v.exposure == "internet"]
    internal = [v for v in config.vips if v.exposure != "internet"]
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
        f"    bind *:{config.stats_port}",
        "    no log",
        *stats_guard,
        "    stats enable",
        "    stats uri /stats",
        "    stats refresh 10s",
        "    http-request use-service prometheus-exporter if { path /metrics }",
        "    monitor-uri /health",
        "",
    ]

    # S3 frontends (only emitted when a backend exists to route to, since HAProxy
    # refuses to start if default_backend references a missing backend section)
    if config.vips and config.certs.path and config.backends:
        backend_name = config.backends[0].name
        if internal:
            hosts = [_vip_host(v.address) for v in internal]
            binds = " ".join(f"{h}:{https_port}" for h in hosts)
            cfg_lines += _s3_frontend_internal(config, binds, backend_name, hostnames, hosts)
        if internet:
            hosts = [_vip_host(v.address) for v in internet]
            binds = " ".join(f"{h}:{https_port}" for h in hosts)
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
        for i, server in enumerate(backend.servers):
            cfg_lines.append(
                f"    server srv{i + 1} {server} check inter {backend.health_check_interval}ms"
            )
        cfg_lines.append("")

    config.haproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.haproxy_config_path.write_text("\n".join(cfg_lines))


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
                f"    router-id {config.bgp.router_id};",
                f"    local-address {config.bgp.local_address};",
                f"    local-as {config.bgp.local_as};",
                f"    peer-as {config.bgp.peer_as};",
                f"    hold-time {config.bgp.hold_time};",
                "",
                "    family {",
                "        ipv4 unicast;",
                "    }",
                "",
            ]
        )

        if config.bgp.bfd_enabled:
            cfg_lines.extend(
                [
                    "    bfd {",
                    "        enabled;",
                    "    }",
                    "",
                ]
            )

        cfg_lines.extend(
            [
                "    api {",
                "        processes [ announce-routes ];",
                "    }",
                "}",
                "",
            ]
        )

    config.bgp_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.bgp_config_path.write_text("\n".join(cfg_lines))


def _generate_keepalived_config(config: OberConfig) -> None:
    """Generate keepalived configuration file."""
    from ober.system import SystemInfo

    system = SystemInfo()
    local_ip = system.get_local_ip() or "0.0.0.0"
    hostname = system.hostname

    # Detect interface if not specified
    interface = config.keepalived.interface
    if not interface:
        interface = _detect_default_interface()

    # Build all nodes list (self + peers)
    all_nodes = [local_ip] + config.keepalived.peers

    cfg_lines = [
        "# Herr Ober Keepalived Configuration",
        "# Generated by ober config",
        "",
        "global_defs {",
        f"    router_id ober_{hostname}",
        "    vrrp_skip_check_adv_addr",
        "    vrrp_garp_master_delay 1",
        "}",
        "",
        "# Health check script",
        "vrrp_script chk_haproxy {",
        f'    script "/usr/bin/curl -sf http://127.0.0.1:{config.stats_port}/health"',
        "    interval 2",
        "    weight -50  # Reduce priority by 50 if health check fails",
        "    fall 2      # Require 2 failures before marking down",
        "    rise 2      # Require 2 successes before marking up",
        "}",
        "",
    ]

    # Generate one vrrp_instance per VIP
    for idx, vip_config in enumerate(config.vips):
        vip = vip_config.address.split("/")[0]  # Strip CIDR notation
        owner, priority = get_vip_owner(vip, all_nodes, local_ip)
        router_id = get_vrrp_router_id(vip)

        cfg_lines.extend(
            [
                f"# VIP {idx + 1}: {vip} (owner: {owner})",
                f"vrrp_instance VI_{idx + 1} {{",
                "    state BACKUP  # All start as BACKUP, preempt determines master",
                f"    interface {interface}",
                f"    virtual_router_id {router_id}",
                f"    priority {priority}",
                f"    advert_int {config.keepalived.advert_int}",
                "    preempt_delay 0",
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
            for peer in config.keepalived.peers:
                cfg_lines.append(f"        {peer}")
            cfg_lines.extend(
                [
                    "    }",
                    "",
                ]
            )

        cfg_lines.extend(
            [
                "    # Track interface - demote priority if interface goes down",
                "    track_interface {",
                f"        {interface} weight -50",
                "    }",
                "",
                "    # Health check - demote priority if HAProxy is down",
                "    track_script {",
                "        chk_haproxy",
                "    }",
                "",
                "    virtual_ipaddress {",
                f"        {vip}/32 dev {interface}",
                "    }",
                "}",
                "",
            ]
        )

    config.keepalived_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.keepalived_config_path.write_text("\n".join(cfg_lines))


def _configure_vip_interface(config: OberConfig) -> None:
    """Configure the VIP dummy interface."""
    from ober.system import OSFamily, SystemInfo

    system = SystemInfo()

    if not config.vips:
        return

    if system.os_family == OSFamily.DEBIAN:
        # Use netplan
        netplan_cfg = {
            "network": {
                "version": 2,
                "tunnels": {
                    "lo-vip": {
                        "mode": "dummy",
                        "addresses": [v.address for v in config.vips],
                    }
                },
            }
        }
        import yaml

        netplan_path = Path("/etc/netplan/60-vip.yaml")
        netplan_path.write_text(yaml.dump(netplan_cfg, default_flow_style=False))

        # Apply netplan
        from ober.system import run_command

        run_command(["netplan", "apply"], check=False)

    elif system.os_family == OSFamily.RHEL:
        # Use nmcli
        from ober.system import run_command

        # Create dummy interface
        run_command(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "dummy",
                "ifname",
                "lo-vip",
                "con-name",
                "lo-vip",
            ],
            check=False,
        )

        # Add IP addresses
        for vip in config.vips:
            run_command(
                ["nmcli", "connection", "modify", "lo-vip", "+ipv4.addresses", vip.address],
                check=False,
            )

        # Bring up the interface
        run_command(["nmcli", "connection", "up", "lo-vip"], check=False)
