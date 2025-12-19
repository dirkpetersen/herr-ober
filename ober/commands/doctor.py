#!/usr/bin/env python3
"""Ober doctor command - diagnostic checks."""

from typing import Any

import click
from rich.console import Console
from rich.table import Table

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

    checks: list[dict[str, Any]] = []
    all_passed = True

    # System checks
    system = SystemInfo()

    # Check 1: Operating System
    os_check = _check_os(system)
    checks.append(os_check)
    if not os_check["passed"]:
        all_passed = False

    # Check 2: Python version
    python_check = _check_python(system)
    checks.append(python_check)
    if not python_check["passed"]:
        all_passed = False

    # Check 3: Root access
    root_check = _check_root(system)
    checks.append(root_check)
    if not root_check["passed"]:
        all_passed = False

    # Check 4: HAProxy installed
    haproxy_check = _check_haproxy()
    checks.append(haproxy_check)

    # Check 5: ExaBGP installed
    exabgp_check = _check_exabgp()
    checks.append(exabgp_check)

    # Check 6: Configuration exists
    config_check = _check_config()
    checks.append(config_check)

    # Check 7: Services status
    http_service_check = _check_service("ober-http")
    checks.append(http_service_check)

    bgp_service_check = _check_service("ober-bgp")
    checks.append(bgp_service_check)

    # Check 8: Network tools
    network_check = _check_network_tools()
    checks.append(network_check)

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
            "message": f"Unknown OS: {system.os_name}. Supported: Ubuntu, Debian, RHEL 10+",
            "critical": True,
        }

    if not system.is_supported:
        return {
            "name": "Operating System",
            "passed": False,
            "status": "unsupported",
            "message": f"{system.os_name} {system.os_version} is not supported. RHEL requires version 10+",
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
        "message": "Not running as root. Use sudo for bootstrap/config operations",
        "critical": True,
    }


def _check_haproxy() -> dict[str, Any]:
    """Check HAProxy installation."""
    version = get_haproxy_version()
    if version:
        # Check for AWS-LC build (version 3.3+)
        try:
            major, minor = map(int, version.split(".")[:2])
            if major >= 3 and minor >= 3:
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


def _check_config() -> dict[str, Any]:
    """Check if configuration exists."""
    config = OberConfig.load()
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
    from pathlib import Path

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
