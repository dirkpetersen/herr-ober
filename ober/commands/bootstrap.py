#!/usr/bin/env python3
"""Ober bootstrap command - automated installation."""

import sys
import venv
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ober.config import OberConfig
from ober.system import OSFamily, SystemInfo, run_command

console = Console()


def _is_in_venv() -> bool:
    """Check if ober is running inside a virtual environment."""
    return sys.prefix != sys.base_prefix


def _get_current_venv_path() -> Path | None:
    """Get the path to the current venv if running in one."""
    if _is_in_venv():
        return Path(sys.prefix)
    return None


KERNEL_TUNING = """# Herr Ober kernel tuning for 50GB/s throughput
# Maximize Network Backlogs (Prevent drops during micro-bursts)
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 65535

# Huge TCP Buffers (128MB per socket)
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728

# Congestion Control (BBR)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Local Port Range
net.ipv4.ip_local_port_range = 1024 65535

# Panic on OOM (Trigger Watchdog faster)
vm.panic_on_oom = 1
kernel.panic = 10
"""

HAPROXY_SERVICE = """[Unit]
Description=Herr Ober HTTP (HAProxy)
After=network.target
Documentation=https://github.com/dirkpetersen/ober

[Service]
Type=forking
PIDFile=/run/ober-http.pid
ExecStart=/usr/sbin/haproxy -D -f {config_path} -p /run/ober-http.pid
ExecReload=/bin/kill -USR2 $MAINPID
Restart=always
RestartSec=1s
KillMode=mixed

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run /var/log

[Install]
WantedBy=multi-user.target
"""

EXABGP_SERVICE = """[Unit]
Description=Herr Ober BGP (ExaBGP)
After=network.target ober-http.service
Documentation=https://github.com/dirkpetersen/ober
# SAFETY LINK: If HAProxy dies, kill BGP immediately
BindsTo=ober-http.service

[Service]
Type=simple
ExecStart={venv_path}/bin/exabgp {config_path}
Restart=on-failure
RestartSec=1s

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
"""


@click.command()
@click.argument("path", required=False, type=click.Path())
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.pass_context
def bootstrap(ctx: click.Context, path: str | None, yes: bool) -> None:
    """Bootstrap Ober installation.

    Automatically installs HAProxy, ExaBGP, applies kernel tuning,
    and generates configuration templates.

    If ober is installed via pipx, ExaBGP and configs will be in the same venv.
    Otherwise, you will be prompted for an installation path (or use PATH argument).
    """
    parent_ctx = ctx.obj
    system = parent_ctx.system if parent_ctx else SystemInfo()

    # Check prerequisites
    if not system.is_supported:
        console.print(f"[red]Error:[/red] {system.os_name} {system.os_version} is not supported.")
        console.print("Supported: Ubuntu, Debian, RHEL 10+")
        ctx.exit(1)

    if not system.is_root:
        console.print("[red]Error:[/red] Bootstrap requires root access.")
        console.print("Run with: sudo ober bootstrap")
        ctx.exit(1)

    # Determine venv and install paths
    current_venv = _get_current_venv_path()
    use_existing_venv = current_venv is not None

    if use_existing_venv and current_venv is not None:
        # ober is running in a venv (e.g., pipx), use it for ExaBGP and configs too
        venv_path: Path = current_venv
        # For pipx installs, everything goes in the venv (unless custom path specified)
        install_path = Path(path) if path else venv_path
        console.print(f"[bold]Detected venv:[/bold] {venv_path}")
        console.print("ExaBGP and configs will be installed in the existing venv.")
    else:
        # Not in a venv, require explicit path
        if not path:
            console.print("[red]Error:[/red] Ober is not running in a venv (e.g., pipx).")
            console.print()
            console.print("Please specify an installation path:")
            console.print("  sudo ober bootstrap /path/to/install")
            console.print()
            console.print("Example:")
            console.print("  sudo ober bootstrap /opt/ober")
            ctx.exit(1)

        install_path = Path(path)

        venv_path = install_path / "venv"
        console.print(f"[bold]Installation path:[/bold] {install_path}")
        console.print(f"[bold]Venv path:[/bold] {venv_path}")

        if not yes:
            console.print()
            if not click.confirm(
                f"This will create a new Python venv at {venv_path}. Continue?",
                default=True,
            ):
                console.print("Aborted.")
                ctx.exit(0)

    config = OberConfig(install_path=install_path)
    # Override venv_path if using existing venv
    config._venv_path_override = venv_path if use_existing_venv else None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        # Step 1: Create directories
        task = progress.add_task("Creating directories...", total=None)
        config.ensure_directories()
        progress.update(task, completed=True, description="[green]Created directories[/green]")

        # Step 2: Apply kernel tuning
        task = progress.add_task("Applying kernel tuning...", total=None)
        _apply_kernel_tuning()
        progress.update(task, completed=True, description="[green]Applied kernel tuning[/green]")

        # Step 3: Install HAProxy
        task = progress.add_task("Installing HAProxy...", total=None)
        _install_haproxy(system)
        progress.update(task, completed=True, description="[green]Installed HAProxy[/green]")

        # Step 4: Setup venv and install ExaBGP
        if use_existing_venv:
            task = progress.add_task("Installing ExaBGP in existing venv...", total=None)
            _install_exabgp(venv_path)
            progress.update(
                task, completed=True, description="[green]Installed ExaBGP in existing venv[/green]"
            )
        else:
            task = progress.add_task("Creating Python venv...", total=None)
            _setup_venv(venv_path)
            progress.update(task, completed=True, description="[green]Created Python venv[/green]")

            task = progress.add_task("Installing ExaBGP...", total=None)
            _install_exabgp(venv_path)
            progress.update(task, completed=True, description="[green]Installed ExaBGP[/green]")

        # Step 5: Create systemd services
        task = progress.add_task("Creating systemd services...", total=None)
        _create_systemd_services(config, venv_path)
        progress.update(task, completed=True, description="[green]Created systemd services[/green]")

        # Step 6: Create initial config templates
        task = progress.add_task("Creating config templates...", total=None)
        _create_config_templates(config, system, venv_path)
        progress.update(task, completed=True, description="[green]Created config templates[/green]")

        # Step 7: Configure watchdog
        task = progress.add_task("Configuring watchdog...", total=None)
        _configure_watchdog()
        progress.update(task, completed=True, description="[green]Configured watchdog[/green]")

        # Step 8: Open firewall ports
        task = progress.add_task("Opening firewall ports...", total=None)
        _open_firewall_ports(system)
        progress.update(task, completed=True, description="[green]Opened firewall ports[/green]")

    console.print()
    console.print("[bold green]Bootstrap complete![/bold green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Run [bold]'sudo ober config'[/bold] to configure BGP, VIPs, and backends")
    console.print("  2. Run [bold]'sudo ober doctor'[/bold] to verify installation")
    console.print("  3. Run [bold]'sudo ober start'[/bold] to start services")


def _apply_kernel_tuning() -> None:
    """Apply kernel tuning parameters."""
    sysctl_path = Path("/etc/sysctl.d/99-herr-ober.conf")
    sysctl_path.write_text(KERNEL_TUNING)

    # Apply immediately
    run_command(["sysctl", "--system"], check=False)


def _install_haproxy(system: SystemInfo) -> None:
    """Install HAProxy using the appropriate package manager."""
    if system.os_family == OSFamily.DEBIAN:
        # Add HAProxy PPA for latest version (3.3+ with AWS-LC) on Ubuntu
        if "ubuntu" in system.os_name.lower():
            run_command(
                ["add-apt-repository", "-y", "ppa:vbernat/haproxy-3.3"],
                check=False,
            )
        run_command(["apt-get", "update"], check=False)
        run_command(["apt-get", "install", "-y", "haproxy"])
    elif system.os_family == OSFamily.RHEL:
        run_command(["dnf", "install", "-y", "haproxy"])


def _setup_venv(venv_path: Path) -> None:
    """Create Python virtual environment."""
    # Check for existing valid venv by looking for python binary
    python_path = venv_path / "bin" / "python"
    if not python_path.exists():
        # Clear any empty/invalid venv directory
        if venv_path.exists():
            import shutil

            shutil.rmtree(venv_path)
        venv.create(venv_path, with_pip=True)


def _install_exabgp(venv_path: Path) -> None:
    """Install ExaBGP in the virtual environment."""
    python_path = venv_path / "bin" / "python"
    pip_path = venv_path / "bin" / "pip"

    # pipx venvs don't include pip by default, so use python -m pip
    # which works whether pip is installed as a standalone or as a module
    if pip_path.exists():
        run_command([str(pip_path), "install", "exabgp"])
    else:
        # Use python -m pip (works in pipx venvs)
        run_command([str(python_path), "-m", "pip", "install", "exabgp"])


def _create_systemd_services(config: OberConfig, venv_path: Path) -> None:
    """Create systemd service files."""
    systemd_path = Path("/etc/systemd/system")

    # HAProxy service
    http_service = HAPROXY_SERVICE.format(config_path=config.haproxy_config_path)
    (systemd_path / "ober-http.service").write_text(http_service)

    # ExaBGP service
    bgp_service = EXABGP_SERVICE.format(
        venv_path=venv_path,
        config_path=config.bgp_config_path,
    )
    (systemd_path / "ober-bgp.service").write_text(bgp_service)

    # Reload systemd
    run_command(["systemctl", "daemon-reload"])


def _create_config_templates(config: OberConfig, system: SystemInfo, venv_path: Path) -> None:
    """Create initial configuration templates."""
    # Get local IP for defaults
    local_ip = system.get_local_ip() or "0.0.0.0"

    # Create minimal HAProxy config
    haproxy_cfg = f"""# Herr Ober HAProxy Configuration
# Generated by ober bootstrap - run 'ober config' to customize

global
    log stdout format raw local0 info
    maxconn 100000
    tune.ssl.default-dh-param 2048

defaults
    mode http
    log global
    option httplog
    option dontlognull
    timeout connect 5s
    timeout client 30s
    timeout server 30s
    timeout http-request 10s
    timeout http-keep-alive 10s

# Stats endpoint for Prometheus
frontend stats
    bind *:{config.stats_port}
    stats enable
    stats uri /stats
    stats refresh 10s
    http-request use-service prometheus-exporter if {{ path /metrics }}
    monitor-uri /health

# S3 Frontend - Configure with 'ober config'
# frontend s3_front
#     bind *:443 ssl crt {config.certs_path}/server.pem
#     default_backend s3_back

# S3 Backend - Configure with 'ober config'
# backend s3_back
#     balance leastconn
#     option httpchk GET /
#     server rgw1 127.0.0.1:7480 check
"""
    config.haproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.haproxy_config_path.write_text(haproxy_cfg)

    # Create minimal ExaBGP config
    exabgp_cfg = f"""# Herr Ober ExaBGP Configuration
# Generated by ober bootstrap - run 'ober config' to customize

process announce-routes {{
    run {venv_path}/bin/python -m ober.commands.health;
    encoder text;
}}

# Configure neighbors with 'ober config'
# neighbor 10.0.0.1 {{
#     router-id {local_ip};
#     local-address {local_ip};
#     local-as 65001;
#     peer-as 65000;
#     hold-time 3;
#
#     family {{
#         ipv4 unicast;
#     }}
#
#     api {{
#         processes [ announce-routes ];
#     }}
# }}
"""
    config.bgp_config_path.parent.mkdir(parents=True, exist_ok=True)
    config.bgp_config_path.write_text(exabgp_cfg)

    # Save main config with venv_path
    config.bgp.local_address = local_ip
    config.bgp.router_id = local_ip
    config._venv_path_override = venv_path
    config.save()


def _configure_watchdog() -> None:
    """Configure systemd watchdog."""
    # Check if systemd.conf has watchdog settings
    systemd_conf = Path("/etc/systemd/system.conf")
    if systemd_conf.exists():
        content = systemd_conf.read_text()
        if "RuntimeWatchdogSec" not in content:
            # Append watchdog settings
            with open(systemd_conf, "a") as f:
                f.write("\n# Herr Ober watchdog settings\n")
                f.write("RuntimeWatchdogSec=10s\n")
                f.write("ShutdownWatchdogSec=2min\n")


def _open_firewall_ports(system: SystemInfo) -> None:
    """Open firewall ports 80 (HTTP) and 443 (HTTPS)."""
    if system.os_family == OSFamily.DEBIAN:
        # Ubuntu/Debian uses ufw or iptables
        # First check if ufw is available and active
        result = run_command(["which", "ufw"], check=False)
        if result.returncode == 0:
            # Try to enable ufw if not already enabled
            run_command(["ufw", "--force", "enable"], check=False)
            # Open ports
            run_command(["ufw", "allow", "80/tcp"], check=False)
            run_command(["ufw", "allow", "443/tcp"], check=False)
        else:
            # Fall back to iptables if ufw is not available
            run_command(
                ["iptables", "-A", "INPUT", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"],
                check=False,
            )
            run_command(
                ["iptables", "-A", "INPUT", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"],
                check=False,
            )
            # Save iptables rules
            run_command(["iptables-save"], check=False)
    elif system.os_family == OSFamily.RHEL:
        # RHEL 10+ uses firewalld
        # Check if firewalld is available
        result = run_command(["which", "firewall-cmd"], check=False)
        if result.returncode == 0:
            # Enable firewalld if not running
            run_command(["systemctl", "start", "firewalld"], check=False)
            run_command(["systemctl", "enable", "firewalld"], check=False)
            # Open ports
            run_command(["firewall-cmd", "--permanent", "--add-port=80/tcp"], check=False)
            run_command(["firewall-cmd", "--permanent", "--add-port=443/tcp"], check=False)
            # Reload firewall
            run_command(["firewall-cmd", "--reload"], check=False)
        else:
            # Fall back to iptables if firewalld is not available
            run_command(
                ["iptables", "-A", "INPUT", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"],
                check=False,
            )
            run_command(
                ["iptables", "-A", "INPUT", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"],
                check=False,
            )
            # Save iptables rules (for RHEL)
            run_command(["iptables-save"], check=False)
