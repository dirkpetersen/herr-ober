#!/usr/bin/env python3
"""Ober bootstrap command - automated installation."""

import contextlib
import os
import pwd
import re
import sys
import venv
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ober import config as ober_config
from ober.config import OberConfig
from ober.system import OSFamily, SystemInfo, check_command_exists, run_command

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

# HAProxy binds VIPs that keepalived may not have assigned to this node yet
net.ipv4.ip_nonlocal_bind = 1

# Local Port Range
net.ipv4.ip_local_port_range = 1024 65535

# VIPs on lo-vip (BGP mode): answer ARP only for addresses on the receiving interface and
# use a source address of that interface, so a node never claims a VIP on its LAN.
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2

# Connection tracking: a stateful firewall tracks every client and RGW connection.
# The kernel default (often 262144) fills at this rate and then drops new connections.
# 1M entries use at most about 320 MB. nf_conntrack is loaded at boot (modules-load.d).
net.netfilter.nf_conntrack_max = 1048576
net.netfilter.nf_conntrack_buckets = 262144
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 30

# Panic on OOM (Trigger Watchdog faster)
vm.panic_on_oom = 1
kernel.panic = 10
"""

# Tested ExaBGP range; keep in sync with the [bgp] extra in pyproject.toml
EXABGP_SPEC = "exabgp>=4.2,<5"

# Master-worker mode (-Ws) as in the distro unit: USR2 really reloads, stdout reaches the journal,
# and a config that fails 'haproxy -c' fails the reload while the old workers keep serving.
HAPROXY_SERVICE = """[Unit]
Description=Herr Ober HTTP (HAProxy)
After=network-online.target
Wants=network-online.target
Documentation=https://github.com/dirkpetersen/herr-ober

[Service]
Type=notify
PIDFile=/run/ober-http.pid
ExecStartPre=/usr/sbin/haproxy -Ws -f {config_path} -c
ExecStart=/usr/sbin/haproxy -Ws -f {config_path} -p /run/ober-http.pid -S /run/ober/master.sock
ExecReload=/usr/sbin/haproxy -Ws -f {config_path} -c
ExecReload=/bin/kill -USR2 $MAINPID
Restart=always
RestartSec=1s
KillMode=mixed
SuccessExitStatus=143
# /run/ober holds the admin socket used by 'ober blocklist sync'
RuntimeDirectory=ober
RuntimeDirectoryMode=0700
# keep it while HAProxy is stopped: blocklist sync still updates the file
RuntimeDirectoryPreserve=yes

# Security. No NoNewPrivileges here: on SELinux hosts (RHEL, Amazon Linux) it blocks the
# switch to the haproxy_t domain, and then HAProxy cannot create its sockets in /run/ober.
ProtectSystem=strict
ProtectHome={protect_home}
ReadWritePaths=/run /var/log

[Install]
WantedBy=multi-user.target
"""

EXABGP_SERVICE = """[Unit]
Description=Herr Ober BGP (ExaBGP)
After=network.target ober-http.service
Documentation=https://github.com/dirkpetersen/herr-ober
# SAFETY LINK: If HAProxy stops, stop BGP immediately (routes are withdrawn)
BindsTo=ober-http.service

[Service]
Type=simple
ExecStart={venv_path}/bin/exabgp {config_path}
Restart=on-failure
RestartSec=1s

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome={protect_home}
# The health process writes the announced routes here (read by ober status and ober stop)
ReadWritePaths=-{state_dir}

[Install]
# ober-http.service: when systemd restarts a crashed HAProxy, this unit starts again too
WantedBy=multi-user.target ober-http.service
"""

KEEPALIVED_SERVICE = """[Unit]
Description=Herr Ober HA (Keepalived)
After=network-online.target ober-http.service
Wants=network-online.target
Documentation=https://github.com/dirkpetersen/herr-ober
# SAFETY LINK: If HAProxy stops, stop keepalived immediately (the VIP fails over)
BindsTo=ober-http.service

[Service]
Type=forking
PIDFile=/run/keepalived.pid
ExecStart=/usr/sbin/keepalived -f {config_path} -p /run/keepalived.pid
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=1s

# Security. No NoNewPrivileges: on SELinux hosts it blocks the switch to keepalived_t, and
# then keepalived cannot open its VRRP raw socket or run ping for chk_gateway.
ProtectSystem=strict
ProtectHome={protect_home}
# /run for the PID file; state_dir for the VRRP state that the notify hook writes
ReadWritePaths=/run -{state_dir}

[Install]
# ober-http.service: when systemd restarts a crashed HAProxy, this unit starts again too
WantedBy=multi-user.target ober-http.service
"""


@click.command()
@click.argument("path", required=False, type=click.Path())
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option(
    "--firewall",
    is_flag=True,
    help="Enable ufw or firewalld and allow only SSH, 443/tcp and VRRP.",
)
@click.pass_context
def bootstrap(ctx: click.Context, path: str | None, yes: bool, firewall: bool) -> None:
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
        console.print("Supported: Ubuntu, Debian, RHEL 10+, Amazon Linux 2023/2027")
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
        if path:
            install_path = Path(path).expanduser().resolve()
        else:
            existing = ober_config._read_install_pointer()
            if existing is not None:
                install_path = existing
                console.print(f"[bold]Reusing existing install path:[/bold] {install_path}")
            else:
                install_path = venv_path
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

        install_path = Path(path).expanduser().resolve()

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
        _write_install_pointer(install_path)
        progress.update(task, completed=True, description="[green]Created directories[/green]")

        # Step 2: Apply kernel tuning
        task = progress.add_task("Applying kernel tuning...", total=None)
        _apply_kernel_tuning()
        progress.update(task, completed=True, description="[green]Applied kernel tuning[/green]")

        # Step 3: Install HAProxy and Keepalived
        task = progress.add_task("Installing HAProxy and Keepalived...", total=None)
        _install_packages(system)
        _disable_distro_units()
        _allow_haproxy_backend_ports()
        progress.update(
            task, completed=True, description="[green]Installed HAProxy and Keepalived[/green]"
        )

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
        _own_state_dir(config)
        progress.update(task, completed=True, description="[green]Created systemd services[/green]")

        # Step 6: Create initial config templates
        task = progress.add_task("Creating config templates...", total=None)
        _create_config_templates(config, system, venv_path)
        progress.update(task, completed=True, description="[green]Created config templates[/green]")

        # Step 7: Configure watchdog
        task = progress.add_task("Configuring watchdog...", total=None)
        _configure_watchdog()
        progress.update(task, completed=True, description="[green]Configured watchdog[/green]")

        # Step 8: Host firewall, only when asked for
        if firewall:
            task = progress.add_task("Configuring firewall...", total=None)
            _open_firewall_ports()
            progress.update(task, completed=True, description="[green]Configured firewall[/green]")

    if not firewall:
        console.print(
            "[yellow]Note:[/yellow] firewall not changed. Allow inbound 443/tcp and, in "
            "keepalived mode, VRRP (IP protocol 112), or re-run with --firewall."
        )

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
    # Load nf_conntrack before systemd-sysctl at boot, so the conntrack keys apply
    Path("/etc/modules-load.d/herr-ober.conf").write_text("nf_conntrack\n")

    # Apply immediately
    run_command(["modprobe", "nf_conntrack"], check=False)
    run_command(["sysctl", "--system"], check=False)


def _install_packages(system: SystemInfo) -> None:
    """Install HAProxy and keepalived using the appropriate package manager."""
    if system.os_family == OSFamily.DEBIAN:
        # Add HAProxy PPA for latest version (3.3+ with AWS-LC) on Ubuntu
        if "ubuntu" in system.os_name.lower():
            ppa = run_command(["add-apt-repository", "-y", "ppa:vbernat/haproxy-3.3"], check=False)
            if ppa.returncode != 0:  # e.g. no PPA build yet for a new Ubuntu release
                console.print(
                    "[yellow]Warning:[/yellow] cannot add ppa:vbernat/haproxy-3.3, using the "
                    "distro HAProxy. Check its version with 'haproxy -v'."
                )
        run_command(["apt-get", "update"], check=False)
        # iputils-ping: the optional keepalived chk_gateway script runs /usr/bin/ping.
        run_command(["apt-get", "install", "-y", "haproxy", "keepalived", "iputils-ping"])
    elif system.os_family == OSFamily.RHEL:
        run_command(
            [
                "dnf",
                "install",
                "-y",
                "haproxy",
                "keepalived",
                "iputils",
                "policycoreutils-python-utils",
            ]
        )


def _own_state_dir(config: OberConfig) -> None:
    """Give the state dir to 'nobody', the user ExaBGP and its health process drop to.

    The health process writes ober-health.json there. Root processes (the keepalived notify
    hook, ober status) can still write and read it.
    """
    state_dir = config.health_state_path.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    nobody = pwd.getpwnam("nobody")
    os.chown(state_dir, nobody.pw_uid, nobody.pw_gid)


def _allow_haproxy_backend_ports() -> None:
    """On SELinux hosts, let haproxy_t reach RGW and let keepalived run the ober scripts.

    Without haproxy_connect_any the policy allows only the http port types, so backends on
    ports such as 7480 or 8080 are refused. No-op when SELinux is off or missing.
    """
    if not check_command_exists("getenforce"):
        return
    result = run_command(["getenforce"], check=False)
    if result.stdout.strip() == "Disabled":
        return
    run_command(["setsebool", "-P", "haproxy_connect_any", "1"])
    # shortcut: keepalived_t blocks our check/notify scripts (connect to the stats port, write
    # the state dir, exec ping, watch the maintenance flag). A permissive keepalived_t is no
    # weaker than before 0.4, when keepalived ran as init_t. Upgrade: ship a policy module.
    if check_command_exists("semanage"):
        run_command(["semanage", "permissive", "-a", "keepalived_t"], check=False)
    else:
        console.print(
            "[yellow]Warning:[/yellow] semanage not found (policycoreutils-python-utils); "
            "keepalived mode needs 'semanage permissive -a keepalived_t' on SELinux hosts."
        )


def _disable_distro_units() -> None:
    """Stop the distro haproxy/keepalived units; ober-http and ober-ha replace them.

    The Debian package enables and starts haproxy.service, and package upgrades restart that
    unit instead of ober-http.
    """
    run_command(["systemctl", "disable", "--now", "haproxy", "keepalived"], check=False)


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
        run_command([str(pip_path), "install", EXABGP_SPEC])
    else:
        # Use python -m pip (works in pipx venvs)
        run_command([str(python_path), "-m", "pip", "install", EXABGP_SPEC])


def _is_under_home(path: Path) -> bool:
    """Return True if path is inside /root or /home."""
    candidates = {str(path)}
    with contextlib.suppress(OSError, RuntimeError):
        candidates.add(str(path.resolve()))
    return any(
        p == base or p.startswith(base + "/") for p in candidates for base in ("/root", "/home")
    )


def _write_install_pointer(install_path: Path) -> None:
    """Record the install path so later commands (and ExaBGP) can find the config."""
    pointer = ober_config.INSTALL_POINTER_PATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(f"{install_path}\n")


def _create_systemd_services(
    config: OberConfig, venv_path: Path, systemd_path: Path = Path("/etc/systemd/system")
) -> list[str]:
    """Write (or rewrite) the systemd service files and reload systemd.

    Bootstrap and ``ober upgrade`` both call this, so nodes get new unit templates.

    Returns:
        Names of the ober-http/ober-bgp/ober-ha units whose content changed.
    """

    protect_home = "true"
    if _is_under_home(config.install_path) or _is_under_home(venv_path):
        protect_home = "read-only"
        console.print(
            "[yellow]Warning:[/yellow] Ober is installed under /root or /home; "
            "using ProtectHome=read-only. Reinstall globally (PIPX_HOME=/opt/pipx "
            "PIPX_BIN_DIR=/usr/local/bin) to avoid this."
        )

    units = {
        "ober-http.service": HAPROXY_SERVICE.format(
            config_path=config.haproxy_config_path, protect_home=protect_home
        ),
        "ober-bgp.service": EXABGP_SERVICE.format(
            venv_path=venv_path,
            config_path=config.bgp_config_path,
            protect_home=protect_home,
            state_dir=config.health_state_path.parent,
        ),
        "ober-ha.service": KEEPALIVED_SERVICE.format(
            config_path=config.keepalived_config_path,
            protect_home=protect_home,
            state_dir=config.vrrp_state_path.parent,
        ),
    }
    changed = []
    for name, content in units.items():
        unit_path = systemd_path / name
        if not unit_path.exists() or unit_path.read_text() != content:
            unit_path.write_text(content)
            changed.append(name)

    # ober watch (analysis host) and blocklist sync (proxy nodes); enabled on demand
    from ober.watch.units import write_units

    write_units(config, systemd_path, role="all", protect_home=protect_home)

    # Reload systemd
    run_command(["systemctl", "daemon-reload"])
    return changed


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
    _write_if_missing(config.haproxy_config_path, haproxy_cfg)

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
    _write_if_missing(config.bgp_config_path, exabgp_cfg)

    # Create minimal keepalived config
    keepalived_cfg = """# Herr Ober Keepalived Configuration
# Generated by ober bootstrap - run 'ober config' to customize

global_defs {
    router_id ober_node
}

# Configure with 'ober config'
"""
    _write_if_missing(config.keepalived_config_path, keepalived_cfg)

    # Save main config with venv_path, keeping an existing config intact
    if config.config_path.exists():
        existing = OberConfig.load(config.config_path)
        errors, warnings = existing.validate()
        for warning in warnings:  # unknown keys are dropped by the save below
            console.print(f"[yellow]Warning:[/yellow] {config.config_path}: {warning}")
        if errors:
            # Saving would replace the unreadable parts with defaults
            console.print(
                f"[yellow]Warning:[/yellow] {config.config_path} is not valid and was not "
                "updated. Fix it, then re-run bootstrap ('ober config --dry-run' lists the errors)."
            )
            return
        existing.install_path = config.install_path
        existing._venv_path_override = venv_path
        existing.save()
        console.print(
            f"[yellow]Note:[/yellow] Existing config kept at {config.config_path} "
            "(run 'ober config' to regenerate)."
        )
        return

    config.bgp.local_address = local_ip
    config.bgp.router_id = local_ip
    config._venv_path_override = venv_path
    config.save()


def _write_if_missing(path: Path, content: str) -> None:
    """Write a generated config file unless it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(content)


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


def _ssh_ports() -> list[str]:
    """Ports sshd listens on (``port`` and ``listenaddress <addr>:<port>`` of ``sshd -T``).

    Raises:
        click.ClickException: ``sshd -T`` failed; a guessed port could lock out SSH.
    """
    try:
        result = run_command(["sshd", "-T"], check=False)
        detail = (result.stderr or "").strip() if result.returncode != 0 else ""
    except OSError as exc:
        detail = str(exc) or "sshd not found"
    if detail:
        raise click.ClickException(
            f"'sshd -T' failed ({detail}); cannot find the SSH port, the firewall was not changed"
        )
    out = result.stdout or ""
    ports = re.findall(r"^port (\d+)$", out, re.MULTILINE)
    ports += re.findall(r"^listenaddress \S+:(\d+)$", out, re.MULTILINE)
    return sorted(set(ports), key=int) or ["22"]


def _firewall_commands() -> list[list[str]] | None:
    """Commands that allow 443/tcp and VRRP, and SSH plus enable when the firewall was off.

    A firewall that is already active keeps its own SSH rules (they can be narrower than
    "allow from anywhere") and is not enabled again. All rules are persistent and safe to add
    again. Port 80 is not opened (HAProxy binds only 443). BGP needs no inbound rule (ExaBGP
    connects out, replies are tracked). The stats port stays closed (health checks are local).
    Returns None when neither ufw nor firewalld exists.
    """
    if check_command_exists("ufw"):
        status = run_command(["ufw", "status"], check=False)
        active = "Status: active" in (status.stdout or "")
        ssh = [] if active else [["ufw", "allow", f"{port}/tcp"] for port in _ssh_ports()]
        return [
            *ssh,
            ["ufw", "allow", "443/tcp"],
            ["ufw", "allow", "proto", "vrrp", "from", "any", "to", "any"],
            *([] if active else [["ufw", "--force", "enable"]]),
        ]
    if check_command_exists("firewall-cmd"):
        running = run_command(["systemctl", "is-active", "--quiet", "firewalld"], check=False)
        if running.returncode == 0:
            rules = ["--add-service=https", "--add-protocol=vrrp"]
            return [
                *(["firewall-cmd", "--permanent", r] for r in rules),
                ["firewall-cmd", "--reload"],
            ]
        # firewall-offline-cmd edits the permanent config before firewalld first starts
        rules = [f"--add-port={port}/tcp" for port in _ssh_ports()]
        rules += ["--add-service=https", "--add-protocol=vrrp"]
        return [
            *(["firewall-offline-cmd", r] for r in rules),
            ["systemctl", "enable", "--now", "firewalld"],
        ]
    return None


def _open_firewall_ports() -> None:
    """Enable the host firewall with only the ports Herr Ober needs (``--firewall``).

    Raises:
        click.ClickException: A rule failed; the firewall is then not enabled.
    """
    commands = _firewall_commands()
    if commands is None:
        console.print("[yellow]Warning:[/yellow] neither ufw nor firewalld found; skipped.")
        return
    for cmd in commands:
        result = run_command(cmd, check=False)
        if result.returncode != 0:
            raise click.ClickException(
                f"'{' '.join(cmd)}' failed ({result.stderr.strip()}); the firewall was not "
                "enabled by ober. Check SSH access before you enable it by hand."
            )
