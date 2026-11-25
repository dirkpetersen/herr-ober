#!/usr/bin/env python3
"""Ober maintenance command: take a node out of rotation while HAProxy keeps serving.

The flag file (``OberConfig.maintenance_path``) holds "1" (on) or "0" (off). The BGP health
process withdraws all routes while it is on. Keepalived reads the same file as a
``track_file`` with a negative weight, so the node drops its VRRP priority and the VIPs move.

This module also holds the keepalived ``notify`` hook that writes the VRRP state file.
"""

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.console import Console

from ober.config import OberConfig
from ober.system import SystemInfo

console = Console()

VRRP_STATES = ("MASTER", "BACKUP", "FAULT")


def is_maintenance(path: Path) -> bool:
    """Return True when the flag file holds something other than "0" or nothing.

    An empty file (possible after a crash) counts as off, as keepalived reads it as 0.
    """
    try:
        return path.read_text().strip() not in ("", "0")
    except FileNotFoundError:
        return False


def set_maintenance(path: Path, on: bool) -> None:
    """Write the flag file ("1" or "0"; keepalived needs a number, so "off" keeps the file)."""
    write_atomic(path, "1\n" if on else "0\n")


def write_atomic(path: Path, text: str) -> None:
    """Write a file with tmp-plus-rename, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # a crash must not leave an empty file after the rename
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object; a missing or broken file gives an empty dict."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_vrrp_state(state_file: Path, kind: str, name: str, state: str) -> None:
    """Store the VRRP state of one instance (keepalived notify hook).

    STOP and DELETED remove the instance. Groups are ignored. A lock file serializes
    concurrent hooks, because keepalived can run one per instance at the same time.
    """
    if kind != "INSTANCE":
        return
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(state_file)
        if state in VRRP_STATES:
            data[name] = state
        else:
            data.pop(name, None)
        write_atomic(state_file, json.dumps(data, sort_keys=True) + "\n")


@click.group()
def maintenance() -> None:
    """Take this node out of rotation (BGP withdraw or VRRP priority drop).

    New connections go to other nodes. HAProxy keeps running, but once the router (BGP)
    or the peer (keepalived) has moved the traffic, open connections to this node break.
    """


def _require_root(ctx: click.Context, action: str) -> None:
    system = ctx.obj.system if ctx.obj else SystemInfo()
    if not system.is_root:
        console.print(f"[red]Error:[/red] Maintenance {action} requires root access.")
        ctx.exit(1)


@maintenance.command("on")
@click.pass_context
def maintenance_on(ctx: click.Context) -> None:
    """Enter maintenance mode."""
    _require_root(ctx, "on")
    config = OberConfig.load()
    set_maintenance(config.maintenance_path, True)
    console.print("[yellow]Maintenance ON[/yellow]: this node leaves rotation.")
    console.print("New connections go to other nodes. Run 'ober maintenance off' to return.")


@maintenance.command("off")
@click.pass_context
def maintenance_off(ctx: click.Context) -> None:
    """Leave maintenance mode."""
    _require_root(ctx, "off")
    config = OberConfig.load()
    set_maintenance(config.maintenance_path, False)
    console.print("[green]Maintenance OFF[/green]: this node returns to rotation when healthy.")


@maintenance.command("status")
def maintenance_status() -> None:
    """Show the maintenance flag and what this node announces or owns."""
    config = OberConfig.load()
    on = is_maintenance(config.maintenance_path)
    console.print(f"Maintenance: {'[yellow]ON[/yellow]' if on else '[green]off[/green]'}")
    if config.ha_mode == "bgp":
        announced = read_json(config.health_state_path).get("announced", [])
        console.print(f"Announced routes: {', '.join(announced) or 'none'}")
    else:
        states = read_json(config.vrrp_state_path)
        text = ", ".join(f"{k}={v}" for k, v in sorted(states.items()))
        console.print(f"VRRP: {text or 'unknown'}")


@maintenance.command("vrrp-notify", hidden=True)
@click.option("--state-file", type=click.Path(path_type=Path), required=True)
@click.argument("kind")
@click.argument("name")
@click.argument("state")
@click.argument("priority", required=False)
def vrrp_notify(
    state_file: Path,
    kind: str,
    name: str,
    state: str,
    priority: str | None,  # noqa: ARG001 (keepalived passes it, not needed)
) -> None:
    """Keepalived notify hook: KIND NAME STATE PRIORITY are added by keepalived."""
    os.umask(0o022)  # keepalived runs scripts with umask 0177, which gives dirs without +x
    record_vrrp_state(state_file, kind, name, state)


# keepalived runs: <venv>/bin/python -m ober.commands.maintenance vrrp-notify ...
if __name__ == "__main__":
    maintenance()
