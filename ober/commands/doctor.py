#!/usr/bin/env python3
"""Ober doctor command - diagnostic checks."""

import os
import time
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from ober.certs import check_cert_expiry
from ober.commands.maintenance import is_maintenance
from ober.commands.status import (
    load_config_safe,
    local_ipv4_addresses,
    read_vrrp_state,
)
from ober.config import OberConfig
from ober.system import (
    OSFamily,
    ServiceInfo,
    SystemInfo,
    check_command_exists,
    get_exabgp_version,
    get_haproxy_version,
)

console = Console()


@click.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Run diagnostic checks on the system.

    Checks prerequisites before bootstrap and validates configuration
    after installation. Reports any issues found.
    """
    parent_ctx = ctx.obj
    json_output = parent_ctx.json_output if parent_ctx else False

    system = SystemInfo()
    config, config_error = load_config_safe()

    checks: list[dict[str, Any]] = [
        _check_os(system),
        _check_python(system),
        _check_root(system),
        _check_haproxy(),
    ]
    if config.ha_mode == "bgp":
        checks.append(_check_exabgp())
    else:
        checks.append(_check_keepalived())
    checks.append(_check_config(config_error))

    # Only the unit for the current HA mode must run; the other one must stay off.
    ha_unit, other_unit = (
        ("ober-bgp", "ober-ha") if config.ha_mode == "bgp" else ("ober-ha", "ober-bgp")
    )
    http_service = ServiceInfo.from_service_name("ober-http")
    checks.append(_check_service("ober-http"))
    checks.append(_check_service(ha_unit))
    checks.append(_check_unit_off(other_unit, "other HA mode's unit"))
    checks.append(_check_unit_off("haproxy", "distro unit, conflicts with ober-http"))
    checks.append(_check_unit_off("keepalived", "distro unit, conflicts with ober-ha"))
    checks.append(_check_restart_pending(config, http_service))
    if config.config_path.exists() and not config_error:
        checks.append(_check_haproxy_syntax(config))
        checks.append(_check_vips(config))
    if config.certs.path:
        checks.append(_check_cert(config.certs.path))
    checks.append(_check_maintenance(config))
    checks.append(_check_network_tools())

    all_passed = not any(c.get("critical") and not c["passed"] for c in checks)

    if json_output:
        import json

        result = {
            "passed": all_passed,
            "checks": checks,
            "system": {
                "os_name": system.os_name,
                "os_version": system.os_version,
                "os_family": system.os_family.value,
                "hostname": system.hostname,
                "arch": system.arch,
            },
        }
        click.echo(json.dumps(result, indent=2))
    else:
        _print_results(checks, system)

    # Exit with error code if any critical checks failed
    if not all_passed:
        ctx.exit(1)


def _check_os(system: SystemInfo) -> dict[str, Any]:
    """Check operating system compatibility."""
    if system.os_family == OSFamily.UNKNOWN:
        return {
            "name": "Operating System",
            "passed": False,
            "status": "unsupported",
            "message": f"Unknown OS: {system.os_name}. Supported: Ubuntu, Debian, RHEL 10+, Amazon Linux 2023/2027",
            "critical": True,
        }

    if not system.is_supported:
        return {
            "name": "Operating System",
            "passed": False,
            "status": "unsupported",
            "message": f"{system.os_name} {system.os_version} is not supported. RHEL requires 10+, Amazon Linux 2023 or 2027",
            "critical": True,
        }

    return {
        "name": "Operating System",
        "passed": True,
        "status": "supported",
        "message": f"{system.os_name} {system.os_version}",
        "critical": True,
    }


def _check_python(system: SystemInfo) -> dict[str, Any]:
    """Check Python version."""
    if system.check_python_version((3, 12)):
        return {
            "name": "Python Version",
            "passed": True,
            "status": "ok",
            "message": f"Python {system.python_version}",
            "critical": True,
        }
    return {
        "name": "Python Version",
        "passed": False,
        "status": "too old",
        "message": f"Python {system.python_version} (requires 3.12+)",
        "critical": True,
    }


def _check_root(system: SystemInfo) -> dict[str, Any]:
    """Check for root access."""
    if system.is_root:
        return {
            "name": "Root Access",
            "passed": True,
            "status": "ok",
            "message": "Running as root",
            "critical": True,
        }
    return {
        "name": "Root Access",
        "passed": False,
        "status": "missing",
        "message": "Not root: some checks are unknown. Use sudo for bootstrap/config",
        "critical": False,
    }


def _check_haproxy() -> dict[str, Any]:
    """Check HAProxy installation."""
    version = get_haproxy_version()
    if version:
        # Check for AWS-LC build (version 3.3+)
        try:
            major, minor = map(int, version.split(".")[:2])
            if (major, minor) >= (3, 3):
                return {
                    "name": "HAProxy",
                    "passed": True,
                    "status": "installed",
                    "message": f"Version {version}",
                    "critical": False,
                }
            return {
                "name": "HAProxy",
                "passed": True,
                "status": "installed (old)",
                "message": f"Version {version} (recommend 3.3+ with AWS-LC)",
                "critical": False,
            }
        except (ValueError, IndexError):
            return {
                "name": "HAProxy",
                "passed": True,
                "status": "installed",
                "message": f"Version {version}",
                "critical": False,
            }
    return {
        "name": "HAProxy",
        "passed": False,
        "status": "not installed",
        "message": "Run 'ober bootstrap' to install",
        "critical": False,
    }


def _check_exabgp() -> dict[str, Any]:
    """Check ExaBGP installation."""
    version = get_exabgp_version()
    if version:
        return {
            "name": "ExaBGP",
            "passed": True,
            "status": "installed",
            "message": f"Version {version}",
            "critical": False,
        }
    return {
        "name": "ExaBGP",
        "passed": False,
        "status": "not installed",
        "message": "Run 'ober bootstrap' to install",
        "critical": False,
    }


def _check_keepalived() -> dict[str, Any]:
    """Check keepalived installation."""
    from ober.system import get_keepalived_version

    version = get_keepalived_version()
    if version:
        return {
            "name": "Keepalived",
            "passed": True,
            "status": "installed",
            "message": f"Version {version}",
            "critical": False,
        }
    return {
        "name": "Keepalived",
        "passed": False,
        "status": "not installed",
        "message": "Run 'ober bootstrap' to install",
        "critical": False,
    }


def _check_config(config_error: str | None = None) -> dict[str, Any]:
    """Check if configuration exists and loads."""
    config = OberConfig()
    if config_error is None:
        config, config_error = load_config_safe()
    if config_error:
        return {
            "name": "Configuration",
            "passed": False,
            "status": "invalid",
            "message": config_error.splitlines()[0][:200],
            "critical": True,
        }
    if config.config_path.exists():
        return {
            "name": "Configuration",
            "passed": True,
            "status": "found",
            "message": str(config.config_path),
            "critical": False,
        }
    return {
        "name": "Configuration",
        "passed": False,
        "status": "not found",
        "message": "Run 'ober config' to configure",
        "critical": False,
    }


def _check_service(name: str) -> dict[str, Any]:
    """Check systemd service status."""
    service = ServiceInfo.from_service_name(name)

    # Check if service file exists
    service_file = Path(f"/etc/systemd/system/{name}.service")
    service_installed = service_file.exists()

    if service.is_active:
        return {
            "name": f"Service: {name}",
            "passed": True,
            "status": "active",
            "message": f"PID {service.pid}" if service.pid else "running",
            "critical": False,
        }
    if service.is_enabled:
        return {
            "name": f"Service: {name}",
            "passed": False,
            "status": "inactive",
            "message": "Enabled but not running",
            "critical": False,
        }
    if service_installed:
        return {
            "name": f"Service: {name}",
            "passed": False,
            "status": "disabled",
            "message": "Installed but disabled. Run 'ober start' to enable",
            "critical": False,
        }
    return {
        "name": f"Service: {name}",
        "passed": False,
        "status": "not configured",
        "message": "Service not installed",
        "critical": False,
    }


def _check_unit_off(name: str, why: str) -> dict[str, Any]:
    """Check that a unit which must not run is disabled and inactive."""
    service = ServiceInfo.from_service_name(name)
    if service.is_active or service.is_enabled:
        state = "active" if service.is_active else "enabled"
        return {
            "name": f"Unit off: {name}",
            "passed": False,
            "status": state,
            "message": f"{why}. Run 'systemctl disable --now {name}'",
            "critical": False,
        }
    return {
        "name": f"Unit off: {name}",
        "passed": True,
        "status": "off",
        "message": why,
        "critical": False,
    }


def _process_start_time(pid: int) -> float:
    """Return the start time (unix seconds) of a process from /proc."""
    # Field 22 (starttime, clock ticks since boot) is index 19 after the ")" of comm.
    ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    btime = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def _haproxy_loaded_at(config: OberConfig, pid: int) -> float:
    """Return when HAProxy last loaded its config.

    In master-worker mode a reload keeps the master PID, so the worker uptime from the
    runtime socket is the real load time. Fall back to the master start time.
    """
    from ober.watch.runtime import HAProxyRuntime

    try:
        info = HAProxyRuntime(config.admin_socket_path, timeout=2).send("show info")
        for line in info.splitlines():
            if line.startswith("Uptime_sec:"):
                return time.time() - int(line.split(":", 1)[1])
    except (OSError, ValueError):
        pass
    return _process_start_time(pid)


def _check_restart_pending(config: OberConfig, service: ServiceInfo) -> dict[str, Any]:
    """Check for a replaced HAProxy binary or a config newer than the running process."""
    name = "Restart pending"
    if not service.is_active or not service.pid:
        return {"name": name, "passed": True, "status": "n/a", "message": "ober-http not running"}
    try:
        exe = os.readlink(f"/proc/{service.pid}/exe")
        if exe.endswith(" (deleted)"):
            return {
                "name": name,
                "passed": False,
                "status": "restart needed",
                "message": "HAProxy binary changed on disk. Run 'ober restart --hard'",
                "critical": False,
            }
        loaded = _haproxy_loaded_at(config, service.pid)
    except (OSError, ValueError, IndexError, StopIteration) as exc:
        return {
            "name": name,
            "passed": False,
            "status": "unknown",
            "message": f"Cannot inspect PID {service.pid}: {exc}",
            "critical": False,
        }
    path = config.haproxy_config_path
    if path.exists() and path.stat().st_mtime > loaded + 2:  # 2 s slack for clock rounding
        return {
            "name": name,
            "passed": False,
            "status": "reload needed",
            "message": f"{path} changed after HAProxy loaded it. Run 'ober restart'",
            "critical": False,
        }
    return {"name": name, "passed": True, "status": "no", "message": "Running config is current"}


def _check_haproxy_syntax(config: OberConfig) -> dict[str, Any]:
    """Run ``haproxy -c`` on the generated config."""
    from ober.commands.test import _test_haproxy_config

    result = _test_haproxy_config(config)
    return {
        "name": "HAProxy config (haproxy -c)",
        "passed": result["passed"],
        "status": "valid" if result["passed"] else "invalid",
        "message": result["message"],
        "critical": result["message"].startswith("Invalid configuration"),
    }


def _check_vips(config: OberConfig) -> dict[str, Any]:
    """Check VIPs on local interfaces against what the HA mode expects."""
    name = "VIPs present"
    present = local_ipv4_addresses()
    vips = [v.address.split("/")[0] for v in config.vips]
    if present is None:
        return {
            "name": name,
            "passed": False,
            "status": "unknown",
            "message": "'ip -o -4 addr show' failed",
            "critical": False,
        }
    if config.ha_mode == "bgp":
        # BGP mode: every node holds every VIP on lo-vip.
        missing = [v for v in vips if v not in present]
        wrong = [f"{v} missing" for v in missing]
    else:
        # Keepalived: a VIP must be here only while this node is MASTER for it.
        states = read_vrrp_state(config) or {}
        wrong = []
        for v in vips:
            state = states.get(v)
            if state == "MASTER" and v not in present:
                wrong.append(f"{v} missing (MASTER)")
            elif state in ("BACKUP", "FAULT") and v in present:
                wrong.append(f"{v} present ({state})")
    if wrong:
        return {
            "name": name,
            "passed": False,
            "status": "mismatch",
            "message": ", ".join(wrong),
            "critical": False,
        }
    held = [v for v in vips if v in present]
    return {
        "name": name,
        "passed": True,
        "status": "ok",
        "message": f"held here: {', '.join(held) or 'none'}",
        "critical": False,
    }


def _check_cert(path: str) -> dict[str, Any]:
    """Check certificate expiry (warn at 21 days, fail at 7)."""
    result = check_cert_expiry(path)
    return {
        "name": "Certificate expiry",
        "passed": result["level"] == "ok",
        "status": {"ok": "ok", "warn": "expires soon"}.get(result["level"], "expiring"),
        "message": result["message"],
        "critical": result["level"] == "crit",
    }


def _check_maintenance(config: OberConfig) -> dict[str, Any]:
    """Warn when the node is in maintenance mode (out of rotation)."""
    on = is_maintenance(config.maintenance_path)
    return {
        "name": "Maintenance mode",
        "passed": not on,
        "status": "on" if on else "off",
        "message": "Node is out of rotation; run 'ober maintenance off'" if on else "",
        "critical": False,
    }


def _check_network_tools() -> dict[str, Any]:
    """Check for required network tools."""
    tools = ["ip", "ss", "ping"]
    missing = [t for t in tools if not check_command_exists(t)]

    if not missing:
        return {
            "name": "Network Tools",
            "passed": True,
            "status": "available",
            "message": "ip, ss, ping",
            "critical": False,
        }
    return {
        "name": "Network Tools",
        "passed": False,
        "status": "missing",
        "message": f"Missing: {', '.join(missing)}",
        "critical": False,
    }


def _print_results(checks: list[dict[str, Any]], system: SystemInfo) -> None:
    """Print diagnostic results as a table."""
    console.print()
    console.print("[bold]Ober Diagnostics[/bold]")
    console.print(f"Host: {system.hostname} ({system.arch})")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    for check in checks:
        if check["passed"]:
            status_str = f"[green]{check['status']}[/green]"
        elif check.get("critical"):
            status_str = f"[red]{check['status']}[/red]"
        else:
            status_str = f"[yellow]{check['status']}[/yellow]"

        table.add_row(check["name"], status_str, check["message"])

    console.print(table)
    console.print()

    # Summary
    passed = sum(1 for c in checks if c["passed"])
    failed = len(checks) - passed
    critical_failed = sum(1 for c in checks if not c["passed"] and c.get("critical"))

    if critical_failed > 0:
        console.print(
            f"[red]Critical issues found:[/red] {critical_failed} critical check(s) failed"
        )
    elif failed > 0:
        console.print(f"[yellow]Some checks failed:[/yellow] {failed} non-critical issue(s)")
    else:
        console.print("[green]All checks passed![/green]")
