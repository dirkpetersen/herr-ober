#!/usr/bin/env python3
"""Ober test command - test BGP connectivity and config validity."""

import json
import socket
import subprocess
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from ober.config import OberConfig
from ober.system import check_command_exists

console = Console()


@click.command()
@click.pass_context
def test(ctx: click.Context) -> None:
    """Test BGP connectivity and configuration validity.

    Validates configuration and tests connectivity to BGP neighbors
    without starting services. Useful for verifying setup before going live.
    """
    parent_ctx = ctx.obj
    json_output = parent_ctx.json_output if parent_ctx else False

    config = OberConfig.load()
    results: dict[str, Any] = {
        "config_valid": True,
        "errors": [],
        "warnings": [],
        "tests": [],
    }

    # Test 1: Check configuration exists
    if not config.config_path.exists():
        results["config_valid"] = False
        results["errors"].append("Configuration file not found. Run 'ober config' first.")
        _output_results(results, json_output)
        ctx.exit(1)

    # Test 2: Validate HAProxy config syntax
    haproxy_test = _test_haproxy_config(config)
    results["tests"].append(haproxy_test)
    if not haproxy_test["passed"]:
        results["config_valid"] = False
        results["errors"].append(haproxy_test["message"])

    # Test 3: Check BGP neighbors configured
    if not config.bgp.neighbors:
        results["warnings"].append("No BGP neighbors configured")
    else:
        # Test 4: Test connectivity to each BGP neighbor
        for neighbor in config.bgp.neighbors:
            neighbor_test = _test_bgp_neighbor(neighbor)
            results["tests"].append(neighbor_test)
            if not neighbor_test["passed"]:
                results["warnings"].append(f"BGP neighbor {neighbor}: {neighbor_test['message']}")

    # Test 5: Check VIPs configured
    if not config.vips:
        results["warnings"].append("No VIPs configured")

    # Test 6: Check backends configured
    if not config.backends:
        results["warnings"].append("No backends configured")
    else:
        # Test backend connectivity
        for backend in config.backends:
            for server in backend.servers:
                backend_test = _test_backend(server, backend.name)
                results["tests"].append(backend_test)
                if not backend_test["passed"]:
                    results["warnings"].append(
                        f"Backend {backend.name}/{server}: {backend_test['message']}"
                    )

    # Test 7: Check certificate if configured
    if config.certs.path:
        cert_test = _test_certificate(config.certs.path)
        results["tests"].append(cert_test)
        if not cert_test["passed"]:
            results["warnings"].append(f"Certificate: {cert_test['message']}")

    _output_results(results, json_output)

    # Exit with appropriate code
    if not results["config_valid"]:
        ctx.exit(1)
    elif results["warnings"]:
        ctx.exit(0)  # Warnings are not fatal


def _test_haproxy_config(config: OberConfig) -> dict[str, Any]:
    """Test HAProxy configuration syntax."""
    if not config.haproxy_config_path.exists():
        return {
            "name": "HAProxy Config",
            "passed": False,
            "message": "Configuration file not found",
        }

    if not check_command_exists("haproxy"):
        return {
            "name": "HAProxy Config",
            "passed": False,
            "message": "HAProxy not installed",
        }

    try:
        result = subprocess.run(
            ["haproxy", "-c", "-f", str(config.haproxy_config_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return {
                "name": "HAProxy Config",
                "passed": True,
                "message": "Configuration valid",
            }
        else:
            # Extract error message
            error = result.stderr.strip() or result.stdout.strip()
            return {
                "name": "HAProxy Config",
                "passed": False,
                "message": f"Invalid configuration: {error[:100]}",
            }
    except subprocess.TimeoutExpired:
        return {
            "name": "HAProxy Config",
            "passed": False,
            "message": "Validation timed out",
        }


def _test_bgp_neighbor(neighbor: str) -> dict[str, Any]:
    """Test connectivity to a BGP neighbor."""
    # Test TCP port 179 (BGP)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((neighbor, 179))
        sock.close()

        if result == 0:
            return {
                "name": f"BGP Neighbor {neighbor}",
                "passed": True,
                "message": "Port 179 reachable",
            }
        else:
            return {
                "name": f"BGP Neighbor {neighbor}",
                "passed": False,
                "message": "Port 179 not reachable. Check firewall rules.",
            }
    except TimeoutError:
        return {
            "name": f"BGP Neighbor {neighbor}",
            "passed": False,
            "message": "Connection timed out. Check network connectivity.",
        }
    except socket.gaierror:
        return {
            "name": f"BGP Neighbor {neighbor}",
            "passed": False,
            "message": "Cannot resolve hostname",
        }
    except Exception as e:
        return {
            "name": f"BGP Neighbor {neighbor}",
            "passed": False,
            "message": str(e),
        }


def _test_backend(server: str, backend_name: str) -> dict[str, Any]:
    """Test connectivity to a backend server."""
    # Parse host:port
    if ":" in server:
        host, port_str = server.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return {
                "name": f"Backend {backend_name}/{server}",
                "passed": False,
                "message": f"Invalid port: {port_str}",
            }
    else:
        host = server
        port = 80  # Default HTTP port

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()

        if result == 0:
            return {
                "name": f"Backend {backend_name}/{server}",
                "passed": True,
                "message": "Reachable",
            }
        else:
            return {
                "name": f"Backend {backend_name}/{server}",
                "passed": False,
                "message": "Not reachable",
            }
    except TimeoutError:
        return {
            "name": f"Backend {backend_name}/{server}",
            "passed": False,
            "message": "Connection timed out",
        }
    except socket.gaierror:
        return {
            "name": f"Backend {backend_name}/{server}",
            "passed": False,
            "message": "Cannot resolve hostname",
        }
    except Exception as e:
        return {
            "name": f"Backend {backend_name}/{server}",
            "passed": False,
            "message": str(e),
        }


def _test_certificate(cert_path: str) -> dict[str, Any]:
    """Test if certificate file exists and is valid."""
    from pathlib import Path

    path = Path(cert_path)
    if not path.exists():
        return {
            "name": "Certificate",
            "passed": False,
            "message": f"File not found: {cert_path}",
        }

    # Check if file contains both cert and key (PEM format for HAProxy)
    try:
        content = path.read_text()
        has_cert = "-----BEGIN CERTIFICATE-----" in content
        has_key = "-----BEGIN" in content and "PRIVATE KEY-----" in content

        if has_cert and has_key:
            return {
                "name": "Certificate",
                "passed": True,
                "message": "Valid PEM file with certificate and key",
            }
        elif has_cert:
            return {
                "name": "Certificate",
                "passed": False,
                "message": "Certificate found but no private key. HAProxy requires both in PEM file.",
            }
        else:
            return {
                "name": "Certificate",
                "passed": False,
                "message": "Invalid PEM format",
            }
    except Exception as e:
        return {
            "name": "Certificate",
            "passed": False,
            "message": f"Cannot read file: {e}",
        }


def _output_results(results: dict[str, Any], json_output: bool) -> None:
    """Output test results."""
    if json_output:
        click.echo(json.dumps(results, indent=2))
        return

    console.print()
    console.print("[bold]Ober Configuration Test[/bold]")
    console.print()

    # Tests table
    if results["tests"]:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Test")
        table.add_column("Status")
        table.add_column("Details")

        for test in results["tests"]:
            status = "[green]PASS[/green]" if test["passed"] else "[red]FAIL[/red]"
            table.add_row(test["name"], status, test["message"])

        console.print(table)
        console.print()

    # Errors
    if results["errors"]:
        console.print("[bold red]Errors:[/bold red]")
        for error in results["errors"]:
            console.print(f"  - {error}")
        console.print()

    # Warnings
    if results["warnings"]:
        console.print("[bold yellow]Warnings:[/bold yellow]")
        for warning in results["warnings"]:
            console.print(f"  - {warning}")
        console.print()

    # Summary
    passed = sum(1 for t in results["tests"] if t["passed"])
    failed = len(results["tests"]) - passed

    if results["config_valid"] and failed == 0:
        console.print("[bold green]All tests passed![/bold green]")
    elif results["config_valid"]:
        console.print(f"[bold yellow]{passed} passed, {failed} failed[/bold yellow]")
    else:
        console.print("[bold red]Configuration invalid[/bold red]")
