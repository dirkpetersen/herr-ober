#!/usr/bin/env python3
"""Ober cluster command: one view of all ingress nodes.

``ober cluster status`` collects ``ober --json status`` from each node over SSH (this node is
read in-process) and adds the checks that one node cannot make alone: is every VIP held or
announced by the right number of nodes, and do the nodes run the same config and versions.
"""

import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import click
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ober.commands.status import LEVELS, collect_status, load_config_safe
from ober.config import PER_NODE_KEYS, OberConfig

console = Console()

REMOTE_STATUS = "sudo -n ober --json status"


def cluster_nodes(config: OberConfig) -> list[str]:
    """``nodes`` from ober.yaml; keepalived mode falls back to this node plus its peers."""
    if config.nodes:
        return list(dict.fromkeys(config.nodes))
    if config.ha_mode == "keepalived" and config.keepalived.peers:
        return list(dict.fromkeys([config.node_ip(), *config.keepalived.peers]))
    return []


def _is_local(node: str, config: OberConfig) -> bool:
    return node in {config.node_ip(), "localhost", "127.0.0.1", socket.gethostname()} or (
        node == socket.getfqdn()
    )


def _ssh(node: str, timeout: int) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", "--", node]


def _remote_status(node: str, timeout: int) -> dict[str, Any]:
    """Run ``ober --json status`` on ``node`` over SSH. Raises RuntimeError with the reason."""
    cmd = [*_ssh(node, timeout), REMOTE_STATUS]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e) or type(e).__name__) from e
    if proc.returncode != 0:
        lines = (proc.stderr.strip() or f"exit {proc.returncode}").splitlines()
        raise RuntimeError(lines[-1][:200])
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError(f"no JSON from 'ober --json status': {e}") from e
    if not isinstance(data, dict) or "check" not in data:
        raise RuntimeError("unexpected answer from 'ober --json status'")
    return data


def gather(config: OberConfig, nodes: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    """Node -> its status dict, or ``{"error": text}`` when it could not be read."""

    def one(node: str) -> dict[str, Any]:
        if _is_local(node, config):
            return collect_status(*load_config_safe())
        try:
            return _remote_status(node, timeout)
        except RuntimeError as e:
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=min(16, len(nodes) or 1)) as pool:
        return dict(zip(nodes, pool.map(one, nodes), strict=True))


def evaluate_cluster(ha_mode: str, nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Cluster-wide problems: unreachable or unhealthy nodes, VIP placement, drift."""
    problems: list[tuple[str, str]] = []
    ok = {n: s for n, s in nodes.items() if "error" not in s}
    for node, s in nodes.items():
        if "error" in s:
            problems.append(("CRIT", f"{node}: unreachable ({s['error']})"))
        elif s["check"]["level"] != "OK":
            problems.append((s["check"]["level"], f"{node}: {'; '.join(s['check']['problems'])}"))

    holders: dict[str, list[str]] = {}
    for node, s in ok.items():
        for vip in s.get("vips", []):
            held = vip.get("announced") if ha_mode == "bgp" else vip.get("vrrp_state") == "MASTER"
            holders.setdefault(vip["address"], [])
            if held:
                holders[vip["address"]].append(node)
    for addr, who in sorted(holders.items()):
        if not who:
            what = "announced by" if ha_mode == "bgp" else "held (VRRP MASTER) on"
            problems.append(("CRIT", f"{addr}: not {what} any node"))
        elif ha_mode == "keepalived" and len(who) > 1:
            problems.append(("CRIT", f"{addr}: MASTER on {len(who)} nodes ({', '.join(who)})"))

    for label, key in (("config", ("config_hash",)), ("HAProxy", ("haproxy", "version"))):
        values = {_get(s, key) for s in ok.values()}
        if len(values) > 1:
            problems.append(("WARN", f"{label} differs between nodes"))
    if len({_get(s, ("ober", "version")) for s in ok.values()}) > 1:
        problems.append(("WARN", "ober version differs between nodes"))

    level = max((p[0] for p in problems), key=LEVELS.__getitem__, default="OK")
    summary = (
        f"OBER CLUSTER {level} - " + "; ".join(p[1] for p in problems)
        if problems
        else f"OBER CLUSTER OK - {len(nodes)} node(s), {len(holders)} VIP(s) placed"
    )
    return {"level": level, "summary": summary, "problems": [p[1] for p in problems]}


def _get(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        data = data.get(k) or {}
    return data or None


def _row(ha_mode: str, s: dict[str, Any]) -> list[str]:
    if "error" in s:
        return ["[red]unreachable[/red]", "", "", "", "", "", "", "", ""]
    svc = s["services"]
    ha_unit = "ober-bgp" if ha_mode == "bgp" else "ober-ha"

    def onoff(name: str) -> str:
        active = svc.get(name, {}).get("active")
        return "[green]up[/green]" if active else "[red]down[/red]"

    if ha_mode == "bgp":
        held = [v["address"] for v in s["vips"] if v.get("announced")]
    else:
        held = [v["address"] for v in s["vips"] if v.get("vrrp_state") == "MASTER"]
    backends = s.get("backends")
    be = (
        "?" if backends is None else ", ".join(f"{b['up']}/{b['total']}" for b in backends.values())
    )
    cert = s.get("certificate") or {}
    level = s["check"]["level"]
    color = {"OK": "green", "WARN": "yellow", "CRIT": "red"}[level]
    return [
        f"[{color}]{level}[/{color}]",
        onoff("ober-http"),
        onoff(ha_unit),
        "on" if s.get("maintenance") else "",
        ", ".join(held) or "-",
        be,
        "" if cert.get("days_left") is None else str(cert["days_left"]),
        s.get("config_hash") or "?",
        f"{_get(s, ('ober', 'version')) or '?'} / {_get(s, ('haproxy', 'version')) or '?'}",
    ]


@click.group()
def cluster() -> None:
    """Views over all ingress nodes."""


@cluster.command("status")
@click.option("--nodes", help="Comma-separated nodes (default: 'nodes' in ober.yaml).")
@click.option("--timeout", default=10, show_default=True, help="SSH connect timeout in seconds.")
@click.option("--json", "json_flag", is_flag=True, help="Output in JSON format.")
@click.pass_context
def cluster_status(ctx: click.Context, nodes: str | None, timeout: int, json_flag: bool) -> None:
    """Show every node and check VIP placement and drift. Exit 0 OK, 1 WARN, 2 CRIT.

    Each node is read with 'ssh NODE sudo -n ober --json status' (BatchMode, so keys or an
    agent must work without a prompt). This node is read directly.
    """
    config, _ = load_config_safe()
    names = [n.strip() for n in nodes.split(",") if n.strip()] if nodes else cluster_nodes(config)
    if not names:
        console.print("[red]Error:[/red] No nodes. Set 'nodes:' in ober.yaml or use --nodes.")
        ctx.exit(1)
    results = gather(config, names, timeout)
    check = evaluate_cluster(config.ha_mode, results)

    if json_flag or (ctx.obj and ctx.obj.json_output):
        click.echo(json.dumps({"nodes": results, "check": check}, indent=2, default=str))
        ctx.exit(LEVELS[check["level"]])
    held = "Announces" if config.ha_mode == "bgp" else "MASTER for"
    table = Table(title=f"Cluster ({config.ha_mode})")
    for title in ("Node", "Check", "HTTP", "HA", "Maint", held, "Backends UP", "Cert days"):
        table.add_column(title)
    table.add_column("Config")
    table.add_column("ober / HAProxy")
    for node, s in results.items():
        table.add_row(node, *_row(config.ha_mode, s))
    console.print(table)
    console.print(check["summary"], markup=False)
    ctx.exit(LEVELS[check["level"]])


def shared_per_node_values(text: str) -> list[str]:
    """Per-node keys that ober.yaml sets (copied to every node, they would clash)."""
    found, data = [], yaml.safe_load(text) or {}
    for keys in PER_NODE_KEYS:
        value: Any = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value:
            found.append(".".join(keys))
    return found


@click.command()
@click.option("--nodes", help="Comma-separated nodes (default: 'nodes' in ober.yaml).")
@click.option(
    "--timeout", default=30, show_default=True, help="Seconds each node waits for /health."
)
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def push(ctx: click.Context, nodes: str | None, timeout: int, yes: bool) -> None:
    """Apply this node's ober.yaml on the other nodes, one node at a time.

    Each node runs 'ssh NODE sudo -n ober apply -f -' (check, reload, health gate, and restore
    on failure). The push stops at the first node that fails. Apply on this node first. Values
    that differ per node (local_ip, bgp.router_id, bgp.local_address) go in ober.local.yaml on
    each node. Certificates are not copied.
    """
    config, error = load_config_safe()
    if error or not config.config_path.exists():
        what = escape(f"{config.config_path}: {error}")
        console.print(f"[red]Error:[/red] No valid {what}")
        ctx.exit(1)
    names = [n.strip() for n in nodes.split(",") if n.strip()] if nodes else cluster_nodes(config)
    targets = [n for n in names if not _is_local(n, config)]
    if not targets:
        console.print("[red]Error:[/red] No other nodes. Set 'nodes:' in ober.yaml or use --nodes.")
        ctx.exit(1)
    text = config.config_path.read_text()
    shared = shared_per_node_values(text)
    if shared:
        console.print(
            f"[red]Error:[/red] ober.yaml sets {', '.join(shared)}, which must differ per node."
            " Move them to ober.local.yaml (same directory) on each node."
        )
        ctx.exit(1)
    if not yes:
        click.confirm(f"Apply ober.yaml on {', '.join(targets)}, one at a time?", abort=True)
    for done, node in enumerate(targets):
        console.print(f"[bold]== {node}[/bold]")
        cmd = [*_ssh(node, 10), f"sudo -n ober apply --timeout {int(timeout)} -f -"]
        try:
            proc = subprocess.run(
                cmd, input=text, capture_output=True, text=True, timeout=timeout + 120
            )
            out, rc = proc.stdout + proc.stderr, proc.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            out, rc = str(e), 1
        click.echo(out.rstrip())
        if rc != 0:
            rest = targets[done + 1 :]
            console.print(f"[red]Push stopped:[/red] {node} failed (exit {rc}).")
            if rest:
                console.print(f"Not pushed: {', '.join(rest)}", markup=False)
            ctx.exit(1)
    console.print(f"[green]Applied on {len(targets)} node(s).[/green] Check: ober cluster status")
