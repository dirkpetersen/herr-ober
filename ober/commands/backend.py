#!/usr/bin/env python3
"""Ober backend command: list RGW servers and drain, maint or ready one on this node.

Changes go to the local HAProxy over the admin socket and take effect at once. The state is
saved to the server-state-file, so it survives a reload or restart of HAProxy.
"""

import json
import shlex
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from ober.commands.cluster import run_on_other_nodes
from ober.config import OberConfig, server_name, write_atomic
from ober.system import SystemInfo
from ober.watch.runtime import HAProxyRuntime

console = Console()


def save_server_state(config: OberConfig) -> bool:
    """Write ``show servers state`` to the server-state-file. False when HAProxy is not up."""
    try:
        state = HAProxyRuntime(config.admin_socket_path).send("show servers state")
    except OSError:
        return False
    if not state.startswith("1\n"):  # format version line; anything else is an error text
        return False
    config.server_state_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(config.server_state_path, state)
    return True


def _servers(runtime: HAProxyRuntime, backend: str) -> list[dict[str, str]]:
    """Server rows of ``show stat`` (CSV) for one backend."""
    lines = runtime.send("show stat -1 4 -1").splitlines()
    if not lines or not lines[0].startswith("# "):
        raise click.ClickException(f"Unexpected HAProxy answer: {lines[:1]}")
    header = lines[0][2:].split(",")
    rows = [dict(zip(header, line.split(","), strict=False)) for line in lines[1:] if line]
    return [r for r in rows if r.get("pxname") == backend]


def _setup(ctx: click.Context) -> tuple[OberConfig, HAProxyRuntime, str]:
    system = ctx.obj.system if ctx.obj else SystemInfo()
    if not system.is_root:
        console.print("[red]Error:[/red] The HAProxy admin socket needs root. Use sudo.")
        ctx.exit(1)
    config = OberConfig.load()
    if not config.backends:
        console.print("[red]Error:[/red] No backends configured.")
        ctx.exit(1)
    return config, HAProxyRuntime(config.admin_socket_path), config.backends[0].name


@click.group()
def backend() -> None:
    """Show and change the RGW backend servers on this node."""


@backend.command("list")
@click.option("--json", "json_flag", is_flag=True, help="Output in JSON format.")
@click.pass_context
def list_servers(ctx: click.Context, json_flag: bool) -> None:
    """Show each server: state, check result and sessions."""
    _, runtime, be = _setup(ctx)
    try:
        rows = _servers(runtime, be)
    except OSError as e:
        console.print(f"[red]Error:[/red] Cannot reach HAProxy: {e}")
        ctx.exit(1)
    keys = ("svname", "status", "check_status", "scur", "qcur", "weight")
    out = [{k: r.get(k, "") for k in keys} for r in rows]
    if json_flag or (ctx.obj and ctx.obj.json_output):
        click.echo(json.dumps(out, indent=2))
        return
    table = Table(title=f"Backend {be}")
    for title in ("Server", "State", "Check", "Sessions", "Queued", "Weight"):
        table.add_column(title)
    for r in out:
        color = "green" if r["status"] == "UP" else "yellow" if r["status"] == "DRAIN" else "red"
        table.add_row(*[f"[{color}]{v}[/{color}]" if k == "status" else v for k, v in r.items()])
    console.print(table)


def _set_state(ctx: click.Context, server: str, state: str) -> None:
    config, runtime, be = _setup(ctx)
    names = {server_name(s): s for s in config.backends[0].servers}
    name = server if server in names else server_name(server)
    if name not in names:
        console.print(f"[red]Error:[/red] Unknown server {server}. Known: {', '.join(names)}")
        ctx.exit(1)
    try:
        answer = runtime.send(f"set server {be}/{name} state {state}").strip()
    except OSError as e:
        console.print(f"[red]Error:[/red] Cannot reach HAProxy: {e}")
        ctx.exit(1)
    if answer:
        console.print(f"[red]Error:[/red] HAProxy refused the change: {answer}")
        ctx.exit(1)
    if not save_server_state(config):
        console.print("[yellow]Warning:[/yellow] Could not save the server state file.")
    console.print(f"[green]{be}/{name} is now {state}[/green] on this node.")


_HELP: dict[str, Any] = {
    "drain": "Send no new requests to SERVER; running ones finish (for RGW maintenance).",
    "maint": "Take SERVER out at once: stop health checks and new connections.",
    "ready": "Put SERVER back in service (after drain or maint).",
}


def _make(state: str) -> click.Command:
    @click.argument("server")
    @click.option("--all-nodes", is_flag=True, help="Also on every other node ('nodes:').")
    @click.pass_context
    def cmd(ctx: click.Context, server: str, all_nodes: bool) -> None:
        _set_state(ctx, server, state)
        if all_nodes:
            run_on_other_nodes(ctx, f"sudo -n ober backend {state} -- {shlex.quote(server)}")

    cmd.__doc__ = _HELP[state] + " SERVER is its name or host:port."
    return backend.command(state)(cmd)


for _state in _HELP:
    _make(_state)
