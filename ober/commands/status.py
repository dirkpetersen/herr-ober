#!/usr/bin/env python3
"""Ober status command - show current state."""

import csv
import json
import subprocess
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from ober.certs import check_cert_expiry
from ober.commands.maintenance import is_maintenance
from ober.config import OberConfig
from ober.system import ServiceInfo, get_haproxy_version

console = Console()

# State files written by other components, relative to install_path.
HEALTH_STATE_FILE = Path("var/run/ober-health.json")  # health.py (BGP mode)
VRRP_STATE_FILE = Path("var/run/vrrp-state.json")  # keepalived notify script

LEVELS = {"OK": 0, "WARN": 1, "CRIT": 2}


@click.command()
@click.option(
    "--check",
    is_flag=True,
    help="Print a one-line summary and exit 0 (OK), 1 (WARN) or 2 (CRIT).",
)
@click.pass_context
def status(ctx: click.Context, check: bool) -> None:
    """Show current status of Ober services.

    Displays systemd services, VIPs (present, announced or VRRP state),
    backend health and certificate expiry. Works with a missing or invalid config.
    """
    parent_ctx = ctx.obj
    json_output = parent_ctx.json_output if parent_ctx else False

    config, config_error = load_config_safe()
    result = collect_status(config, config_error)

    if check:
        if json_output:
            click.echo(json.dumps(result["check"], indent=2))
        else:
            click.echo(result["check"]["summary"])
        ctx.exit(LEVELS[result["check"]["level"]])

    if json_output:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _print_status(result)


def load_config_safe() -> tuple[OberConfig, str | None]:
    """Load the config, or return defaults and the error when the file is invalid."""
    try:
        config = OberConfig.load()
    except Exception as exc:  # read-only commands must work with a broken config
        return OberConfig(), str(exc) or type(exc).__name__
    errors, _ = config.validate()  # load() records problems instead of raising
    return config, "\n".join(errors) or None


def collect_status(config: OberConfig, config_error: str | None = None) -> dict[str, Any]:
    """Collect the node state into one dict (also used by ``--json``)."""
    ha_name = "ober-bgp" if config.ha_mode == "bgp" else "ober-ha"
    services: dict[str, Any] = {}
    for name in ("ober-http", ha_name):
        svc = ServiceInfo.from_service_name(name)
        services[name] = {
            "active": svc.is_active,
            "enabled": svc.is_enabled,
            "status": svc.status,
            "pid": svc.pid,
        }

    result: dict[str, Any] = {
        "ha_mode": config.ha_mode,
        "services": services,
        "haproxy": {"version": get_haproxy_version()},
        "config": {
            "path": str(config.config_path),
            "exists": config.config_path.exists(),
            "error": config_error,
        },
    }
    if config.ha_mode == "bgp":
        from ober.system import get_exabgp_version

        neighbors = list(config.bgp.neighbors)
        result["bgp"] = {
            "version": get_exabgp_version(),
            "sessions": bgp_sessions(neighbors) if neighbors else {},
        }
    else:
        from ober.system import get_keepalived_version

        result["keepalived"] = {"version": get_keepalived_version()}

    result["maintenance"] = is_maintenance(config.maintenance_path)
    result["vips"] = _vip_status(config)
    result["backends"] = backend_summary(config)
    if config.certs.path:
        result["certificate"] = {"path": config.certs.path, **check_cert_expiry(config.certs.path)}
    result["check"] = evaluate(result)
    return result


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_announced_routes(config: OberConfig) -> list[str] | None:
    """Return the routes health.py announces, or None when unknown."""
    data = _read_json(config.install_path / HEALTH_STATE_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("announced"), list):
        return None
    return [str(r) for r in data["announced"]]


def read_vrrp_state(config: OberConfig) -> dict[str, str] | None:
    """Return the VRRP state per VIP address, or None when unknown.

    The notify script keys the file by instance name (``VI_<n>``, n = VIP index + 1).
    A key equal to the VIP address is accepted too.
    """
    data = _read_json(config.install_path / VRRP_STATE_FILE)
    if not isinstance(data, dict):
        return None
    states: dict[str, str] = {}
    for idx, vip in enumerate(config.vips):
        addr = vip.address.split("/")[0]
        state = data.get(f"VI_{idx + 1}") or data.get(addr) or data.get(vip.address)
        if state:
            states[addr] = str(state).upper()
    return states


def local_ipv4_addresses() -> set[str] | None:
    """Return the IPv4 addresses on this host (``ip -o -4 addr show``), None if unknown."""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    addrs = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if "inet" in parts[:-1]:
            addrs.add(parts[parts.index("inet") + 1].split("/")[0])
    return addrs


def bgp_sessions(neighbors: list[str]) -> dict[str, bool] | None:
    """Return per neighbor whether a TCP session on port 179 is established, None if unknown."""
    try:
        out = subprocess.run(
            ["ss", "-Htn", "state", "established", "( sport = :179 or dport = :179 )"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    hosts = set()
    for line in out.stdout.splitlines():
        for field in line.split()[2:]:  # Recv-Q Send-Q Local:Port Peer:Port
            hosts.add(field.rsplit(":", 1)[0].strip("[]").removeprefix("::ffff:"))
    return {n: n in hosts for n in neighbors}


def _expected_owner(config: OberConfig, vip: str) -> str:
    from ober.commands.config import get_vip_owner, keepalived_peers

    # Same node list as _generate_keepalived_config, so the answer matches the config.
    local_ip = config.node_ip()
    return get_vip_owner(vip, [local_ip, *keepalived_peers(config, local_ip)], local_ip)[0]


def _vip_status(config: OberConfig) -> list[dict[str, Any]]:
    present = local_ipv4_addresses()
    routes = read_announced_routes(config) if config.ha_mode == "bgp" else None
    vrrp = read_vrrp_state(config) if config.ha_mode == "keepalived" else None
    vips = []
    for vip in config.vips:
        addr = vip.address.split("/")[0]
        entry: dict[str, Any] = {
            "address": addr,
            "exposure": vip.exposure,
            "present": None if present is None else addr in present,
        }
        if config.ha_mode == "bgp":
            entry["announced"] = None if routes is None else f"{addr}/32" in routes
        else:
            entry["vrrp_state"] = None if vrrp is None else vrrp.get(addr, "UNKNOWN")
            entry["expected_owner"] = _expected_owner(config, addr)
        vips.append(entry)
    return vips


def backend_summary(config: OberConfig) -> dict[str, Any] | None:
    """Return server health per backend from the HAProxy runtime socket, None if unknown."""
    from ober.watch.runtime import HAProxyRuntime

    try:
        text = HAProxyRuntime(config.admin_socket_path, timeout=2).send("show stat")
    except OSError:
        return None
    backends: dict[str, Any] = {}
    for row in csv.DictReader(text.lstrip("# ").splitlines()):
        if row.get("svname") in (None, "FRONTEND", "BACKEND"):
            continue
        state = row.get("status") or ""
        entry = backends.setdefault(row["pxname"], {"up": 0, "total": 0, "down": []})
        entry["total"] += 1
        if state.startswith("UP") or state == "no check":
            entry["up"] += 1
        else:
            since = row.get("lastchg") or ""
            entry["down"].append(
                {
                    "server": row["svname"],
                    "status": state,
                    "since_s": int(since) if since.isdigit() else None,
                }
            )
    return backends


def evaluate(result: dict[str, Any]) -> dict[str, Any]:
    """Turn the collected state into OK/WARN/CRIT and a one-line summary."""
    problems: list[tuple[str, str]] = []
    cfg = result["config"]
    if cfg["error"]:
        problems.append(("CRIT", f"config invalid: {cfg['error'].splitlines()[0][:120]}"))
    elif not cfg["exists"]:
        problems.append(("CRIT", f"config not found: {cfg['path']}"))
    for name, svc in result["services"].items():
        if not svc["active"]:
            problems.append(("CRIT", f"{name} {svc['status']}"))
    if result.get("maintenance"):
        problems.append(("WARN", "maintenance mode on (node out of rotation)"))

    backends = result["backends"]
    if backends is None:
        problems.append(("WARN", "backend state unknown (runtime socket not readable)"))
    else:
        for name, b in sorted(backends.items()):
            if b["up"] == 0:
                problems.append(("CRIT", f"{name}: 0/{b['total']} servers UP"))
            elif b["up"] < b["total"]:
                down = ", ".join(d["server"] for d in b["down"])
                problems.append(("WARN", f"{name}: {b['up']}/{b['total']} UP ({down} down)"))

    cert = result.get("certificate")
    if cert and cert["level"] != "ok":
        problems.append((cert["level"].upper(), f"certificate: {cert['message']}"))

    sessions = result.get("bgp", {}).get("sessions", {})
    if sessions is None:
        problems.append(("WARN", "BGP session state unknown (ss failed)"))
    for neighbor, up in (sessions or {}).items():
        if not up:
            problems.append(("WARN", f"BGP neighbor {neighbor}: no established session"))

    for vip in result["vips"]:
        addr = vip["address"]
        if "announced" in vip:
            if vip["announced"] is None:
                problems.append(("WARN", f"{addr}: announce state unknown"))
            elif not vip["announced"]:
                problems.append(("WARN", f"{addr}: not announced"))
            if vip["present"] is False:
                problems.append(("WARN", f"{addr}: not on a local interface"))
        else:
            state = vip["vrrp_state"]
            if state in (None, "UNKNOWN"):
                problems.append(("WARN", f"{addr}: VRRP state unknown"))
            elif state == "FAULT":
                problems.append(("CRIT", f"{addr}: VRRP FAULT"))
            elif vip["present"] is not None and vip["present"] != (state == "MASTER"):
                held = "present" if vip["present"] else "absent"
                problems.append(("WARN", f"{addr}: VRRP {state} but VIP {held}"))

    level = max((p[0] for p in problems), key=LEVELS.__getitem__, default="OK")
    if problems:
        summary = f"OBER {level} - " + "; ".join(p[1] for p in problems)
    else:
        summary = f"OBER OK - {len(result['vips'])} VIP(s), services and backends up"
    return {"level": level, "summary": summary, "problems": [p[1] for p in problems]}


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return "[dim]unknown[/dim]"
    return "[green]yes[/green]" if value else "[red]no[/red]"


def _ago(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _print_status(result: dict[str, Any]) -> None:
    """Print status in a human-readable format."""
    console.print()
    console.print("[bold]Ober Status[/bold]")
    console.print()

    table = Table(title="Services", show_header=True, header_style="bold")
    for col in ("Service", "Status", "PID", "Enabled"):
        table.add_column(col)
    for name, svc in result["services"].items():
        color = "green" if svc["active"] else "red"
        table.add_row(
            name,
            f"[{color}]{svc['status']}[/{color}]",
            str(svc["pid"]) if svc["pid"] else "-",
            "[green]yes[/green]" if svc["enabled"] else "[dim]no[/dim]",
        )
    console.print(table)
    console.print()

    console.print(f"[bold]HAProxy:[/bold] {result['haproxy']['version'] or 'not installed'}")
    if "bgp" in result:
        console.print(f"[bold]ExaBGP:[/bold] {result['bgp']['version'] or 'not installed'}")
        sessions = result["bgp"]["sessions"]
        if sessions is None:
            console.print("[bold]BGP sessions:[/bold] [dim]unknown (ss failed)[/dim]")
        for neighbor, up in (sessions or {}).items():
            state = "[green]established[/green]" if up else "[red]not established[/red]"
            console.print(f"[bold]BGP {neighbor}:[/bold] {state}")
    else:
        ka_ver = result["keepalived"]["version"] or "not installed"
        console.print(f"[bold]Keepalived:[/bold] {ka_ver}")

    if result.get("maintenance"):
        console.print("[bold yellow]Maintenance mode: on[/bold yellow] (ober maintenance off)")

    cfg = result["config"]
    if cfg["error"]:
        console.print(f"[red]Config invalid:[/red] {cfg['path']}: {cfg['error']}")
    elif cfg["exists"]:
        console.print(f"[bold]Config:[/bold] {cfg['path']}")
    else:
        console.print("[yellow]Configuration not found. Run 'ober config' to configure.[/yellow]")
    console.print()

    if result["vips"]:
        bgp = "bgp" in result
        vt = Table(title="VIPs", show_header=True, header_style="bold")
        cols = ["VIP", "Exposure", "Present"] + (["Announced"] if bgp else ["VRRP", "Owner"])
        for col in cols:
            vt.add_column(col)
        for vip in result["vips"]:
            row = [vip["address"], vip["exposure"], _fmt_bool(vip["present"])]
            if bgp:
                row.append(_fmt_bool(vip["announced"]))
            else:
                state = vip["vrrp_state"] or "unknown"
                color = {"MASTER": "green", "BACKUP": "yellow", "FAULT": "red"}.get(state, "dim")
                row += [f"[{color}]{state}[/{color}]", vip["expected_owner"]]
            vt.add_row(*row)
        console.print(vt)
        console.print()

    backends = result["backends"]
    if backends is None:
        console.print("[bold]Backends:[/bold] [dim]unknown (runtime socket needs root)[/dim]")
    for name, b in sorted((backends or {}).items()):
        color = "green" if b["up"] == b["total"] else "red" if b["up"] == 0 else "yellow"
        down = ", ".join(
            f"{d['server']} {d['status']}" + (f" {_ago(d['since_s'])}" if d["since_s"] else "")
            for d in b["down"]
        )
        line = f"[bold]{name}:[/bold] [{color}]{b['up']}/{b['total']} UP[/{color}]"
        console.print(line + (f" ({down})" if down else ""))

    cert = result.get("certificate")
    if cert:
        color = {"ok": "green", "warn": "yellow"}.get(cert["level"], "red")
        console.print(f"[bold]Certificate:[/bold] [{color}]{cert['message']}[/{color}]")

    console.print()
    check = result["check"]
    color = {"OK": "green", "WARN": "yellow"}.get(check["level"], "red")
    console.print(f"[{color}]{check['summary']}[/{color}]")
    console.print("[dim]Use 'ober logs' to view service logs[/dim]")
