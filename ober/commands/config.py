#!/usr/bin/env python3
"""Ober config command - interactive configuration wizard."""

import socket
from pathlib import Path

import click
import inquirer  # type: ignore[import-untyped]
from rich.console import Console

from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    OberConfig,
    VIPConfig,
)
from ober.system import SystemInfo

console = Console()


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
        # Section 1: BGP Configuration
        console.print("[bold cyan]1. BGP Configuration[/bold cyan]")
        bgp_config = _configure_bgp(config.bgp, local_ip)
        config.bgp = bgp_config

        # Section 2: VIP Configuration
        console.print()
        console.print("[bold cyan]2. VIP Configuration[/bold cyan]")
        vips = _configure_vips(config.vips)
        config.vips = vips

        # Section 3: Backend Configuration
        console.print()
        console.print("[bold cyan]3. Backend Configuration[/bold cyan]")
        backends = _configure_backends(config.backends)
        config.backends = backends

        # Section 4: Certificate Configuration
        console.print()
        console.print("[bold cyan]4. Certificate Configuration[/bold cyan]")
        certs = _configure_certs(config.certs)
        config.certs = certs

        # Section 5: Additional Settings
        console.print()
        console.print("[bold cyan]5. Additional Settings[/bold cyan]")
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
            console.print("Next steps:")
            console.print("  1. Run [bold]ober test[/bold] to validate BGP connectivity")
            console.print("  2. Run [bold]ober start[/bold] to start services")
        else:
            console.print("[yellow]Configuration not applied.[/yellow]")


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


def _configure_vips(current: list[VIPConfig]) -> list[VIPConfig]:
    """Configure Virtual IP settings."""
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
            vips.append(VIPConfig(address=vip))

    return vips


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
        cert_path = inquirer.text(
            message="Certificate file path (PEM format, must include private key)",
            default=current.path or "/opt/ober/etc/certs/server.pem",
        )
        return CertConfig(path=cert_path)

    elif answers["cert_method"] == "route53":
        return _configure_route53_acme(current)

    return CertConfig()


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
        zone_choices = [(f"{z['Name']} ({z['Id']})", z['Id']) for z in hosted_zones]

        if len(hosted_zones) == 1:
            # Only one zone, use it directly
            hosted_zone_id = hosted_zones[0]['Id']
            zone_name = hosted_zones[0]['Name'].rstrip('.')
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
        selected_zone = next((z for z in hosted_zones if z['Id'] == hosted_zone_id), None)
        zone_name = selected_zone['Name'].rstrip('.') if selected_zone else ""

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

    try:
        # Try using AWS CLI first (doesn't require boto3 installed)
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
                zones.append({
                    "Id": zone_id,
                    "Name": zone["Name"],
                })
            return zones
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Fallback: try boto3 if available
    try:
        import boto3  # type: ignore[import-untyped]
        session = boto3.Session(profile_name=profile)
        client = session.client("route53")
        response = client.list_hosted_zones()
        zones = []
        for zone in response.get("HostedZones", []):
            zone_id = zone["Id"].replace("/hostedzone/", "")
            zones.append({
                "Id": zone_id,
                "Name": zone["Name"],
            })
        return zones
    except Exception:
        pass

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
            zones.append({
                "Id": zone_id,
                "Name": zone["Name"],
            })
        return zones
    except Exception:
        pass

    return []


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


def _print_config_summary(config: OberConfig) -> None:
    """Print a summary of the configuration."""
    console.print("[bold]Configuration Summary:[/bold]")
    console.print()

    console.print("[cyan]BGP:[/cyan]")
    console.print(f"  Local AS: {config.bgp.local_as}")
    console.print(f"  Peer AS: {config.bgp.peer_as}")
    console.print(f"  Neighbors: {', '.join(config.bgp.neighbors) or 'none'}")
    console.print(f"  Router ID: {config.bgp.router_id}")
    console.print(f"  Local Address: {config.bgp.local_address}")
    console.print(f"  Hold Time: {config.bgp.hold_time}s")
    console.print(f"  BFD: {'enabled' if config.bgp.bfd_enabled else 'disabled'}")

    console.print()
    console.print("[cyan]VIPs:[/cyan]")
    if config.vips:
        for vip in config.vips:
            console.print(f"  {vip.address}")
    else:
        console.print("  [dim]none configured[/dim]")

    console.print()
    console.print("[cyan]Backends:[/cyan]")
    if config.backends:
        for backend in config.backends:
            console.print(f"  {backend.name}:")
            for server in backend.servers:
                console.print(f"    - {server}")
    else:
        console.print("  [dim]none configured[/dim]")

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


def _apply_configuration(config: OberConfig) -> None:
    """Apply the configuration to the system."""
    # Save main config
    config.save()

    # Generate HAProxy config
    _generate_haproxy_config(config)

    # Generate ExaBGP config
    _generate_exabgp_config(config)

    # Configure VIP interface
    _configure_vip_interface(config)


def _generate_haproxy_config(config: OberConfig) -> None:
    """Generate HAProxy configuration file."""
    cfg_lines = [
        "# Herr Ober HAProxy Configuration",
        "# Generated by ober config",
        "",
        "global",
        "    log stdout format raw local0 info",
        "    maxconn 100000",
        "    tune.ssl.default-dh-param 2048",
        "",
        "defaults",
        "    mode http",
        "    log global",
        "    option httplog",
        "    option dontlognull",
        "    timeout connect 5s",
        "    timeout client 30s",
        "    timeout server 30s",
        "    timeout http-request 10s",
        "    timeout http-keep-alive 10s",
        "",
        f"# Stats endpoint on port {config.stats_port}",
        "frontend stats",
        f"    bind *:{config.stats_port}",
        "    stats enable",
        "    stats uri /stats",
        "    stats refresh 10s",
        "    http-request use-service prometheus-exporter if { path /metrics }",
        "    monitor-uri /health",
        "",
    ]

    # S3 Frontend
    if config.vips and config.certs.path:
        vip_binds = " ".join(f"{v.address.split('/')[0]}:443" for v in config.vips)
        cfg_lines.extend(
            [
                "# S3 Frontend",
                "frontend s3_front",
                f"    bind {vip_binds} ssl crt {config.certs.path}",
                "    default_backend s3_back",
                "",
            ]
        )

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
        f"    run {config.venv_path}/bin/python -m ober.commands.health;",
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
