#!/usr/bin/env python3
"""Ober watch and blocklist commands."""

import logging
import sys
import time
from typing import Any
from urllib.parse import quote

import click
import requests
from rich.console import Console
from rich.table import Table

from ober.config import OberConfig, load_secrets
from ober.watch.blocklist import BlockList, normalize_target
from ober.watch.server import split_listen

console = Console()
error_console = Console(stderr=True)

API_TIMEOUT_S = 10


def _json_output(ctx: click.Context) -> bool:
    parent = ctx.obj
    return bool(parent.json_output) if parent else False


def _local_api_url(config: OberConfig) -> str:
    host, port = split_listen(config.watch.api_listen)
    if host in ("0.0.0.0", "::"):  # noqa: S104
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _headers() -> dict[str, str]:
    token = load_secrets().get("OBER_WATCH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


def _fail(ctx: click.Context, message: str) -> None:
    if _json_output(ctx):
        click.echo(_dumps({"error": message}))
    else:
        error_console.print(f"[red]Error:[/red] {message}")
    sys.exit(1)


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value)


@click.group()
def watch() -> None:
    """Security log analysis (ober watch)."""


@watch.command("run")
@click.option("--store-raw/--no-store-raw", default=True, help="Keep raw request logs on disk.")
@click.pass_context
def watch_run(ctx: click.Context, store_raw: bool) -> None:
    """Run the analysis daemon in the foreground."""
    from ober.watch.daemon import WatchDaemon

    logging.basicConfig(
        level=logging.DEBUG if ctx.obj and ctx.obj.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = OberConfig.load()
    try:
        WatchDaemon(config, store_raw=store_raw).run()
    except OSError as exc:
        _fail(ctx, f"cannot start ober watch: {exc}")


@watch.command("status")
@click.pass_context
def watch_status(ctx: click.Context) -> None:
    """Show counters of the running ober watch daemon."""
    config = OberConfig.load()
    base = config.watch.url or _local_api_url(config)
    try:
        resp = requests.get(f"{base.rstrip('/')}/v1/health", timeout=API_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        _fail(ctx, f"ober watch not reachable at {base}: {exc}")
        return
    if _json_output(ctx):
        click.echo(_dumps(data))
        return
    table = Table(title=f"ober watch ({base})")
    table.add_column("Counter")
    table.add_column("Value", justify="right")
    for key, value in data.items():
        table.add_row(str(key), str(value))
    console.print(table)


@click.group()
def blocklist() -> None:
    """Manage the shared IP blocklist."""


def _api_base(config: OberConfig) -> str | None:
    """Return the API base URL to use, or None to operate on the local file."""
    if config.watch.url:
        return config.watch.url.rstrip("/")
    local = _local_api_url(config)
    try:
        requests.get(f"{local}/v1/health", timeout=1).raise_for_status()
    except requests.RequestException:
        return None
    return local


def _local_list(config: OberConfig) -> BlockList:
    return BlockList(
        config.watch_data_dir / "blocklist.json",
        config.watch.block_ttl_s,
        config.watch.block_ttl_max_s,
    )


def _fmt_expiry(expires_at: float) -> str:
    if not expires_at:
        return "never"
    remaining = int(expires_at - time.time())
    return f"{remaining}s" if remaining > 0 else "expired"


@blocklist.command("sync")
@click.pass_context
def blocklist_sync(ctx: click.Context) -> None:
    """Pull the blocklist from ober watch and apply it to the local HAProxy."""
    from ober.watch.sync import sync_blocklist

    config = OberConfig.load()
    if not config.watch.url:
        _fail(ctx, "watch.url is not configured")
    token = load_secrets().get("OBER_WATCH_TOKEN", "")
    if not token:
        _fail(ctx, "OBER_WATCH_TOKEN missing in ~/.ober/login")
    result = sync_blocklist(config, token)
    if _json_output(ctx):
        click.echo(_dumps(result.__dict__))
    else:
        for warning in result.warnings:
            error_console.print(f"[yellow]Warning:[/yellow] {warning}")
        for error in result.errors:
            error_console.print(f"[red]Error:[/red] {error}")
        if result.reachable and ctx.obj and not ctx.obj.quiet:
            console.print(f"Blocklist synced: +{len(result.added)} -{len(result.removed)}")
    # A timer must not fail because the analysis host is down; real errors do exit 1.
    if result.errors:
        sys.exit(1)


@blocklist.command("list")
@click.pass_context
def blocklist_list(ctx: click.Context) -> None:
    """List active blocks."""
    config = OberConfig.load()
    base = _api_base(config)
    if base:
        try:
            resp = requests.get(f"{base}/v1/blocklist", headers=_headers(), timeout=API_TIMEOUT_S)
            resp.raise_for_status()
            entries = resp.json().get("entries", [])
        except (requests.RequestException, ValueError) as exc:
            _fail(ctx, f"cannot read blocklist from {base}: {exc}")
            return
    else:
        entries = [e.to_dict() for e in _local_list(config).active()]
    if _json_output(ctx):
        click.echo(_dumps(entries))
        return
    table = Table(title="Blocklist")
    for column in ("Target", "Source", "Expires", "Offenses", "Reason"):
        table.add_column(column)
    for e in entries:
        table.add_row(
            str(e.get("target")),
            str(e.get("source")),
            _fmt_expiry(float(e.get("expires_at", 0))),
            str(e.get("offenses", 1)),
            str(e.get("reason", ""))[:60],
        )
    console.print(table)


@blocklist.command("add")
@click.argument("target")
@click.option("--reason", default="manual", help="Why the target is blocked.")
@click.option("--ttl", type=click.IntRange(min=0), default=None, help="Seconds; omit for never.")
@click.pass_context
def blocklist_add(ctx: click.Context, target: str, reason: str, ttl: int | None) -> None:
    """Block an IP or CIDR."""
    try:
        target = normalize_target(target)
    except ValueError as exc:
        _fail(ctx, f"invalid target: {exc}")
        return
    config = OberConfig.load()
    base = _api_base(config)
    if base:
        body = {"target": target, "reason": reason, "ttl_s": ttl}
        try:
            resp = requests.post(
                f"{base}/v1/blocklist", json=body, headers=_headers(), timeout=API_TIMEOUT_S
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            _fail(ctx, f"cannot add {target} via {base}: {exc}")
            return
    else:
        _local_list(config).add_manual(target, reason, ttl)
    if _json_output(ctx):
        click.echo(_dumps({"added": target}))
    elif not ctx.obj.quiet:
        console.print(f"[green]Blocked {target}[/green]")


@blocklist.command("remove")
@click.argument("target")
@click.pass_context
def blocklist_remove(ctx: click.Context, target: str) -> None:
    """Unblock an IP or CIDR."""
    try:
        target = normalize_target(target)
    except ValueError as exc:
        _fail(ctx, f"invalid target: {exc}")
        return
    config = OberConfig.load()
    base = _api_base(config)
    if base:
        try:
            resp = requests.delete(
                f"{base}/v1/blocklist/{quote(target, safe='')}",
                headers=_headers(),
                timeout=API_TIMEOUT_S,
            )
            if resp.status_code == 404:
                _fail(ctx, f"{target} is not in the blocklist")
                return
            resp.raise_for_status()
        except requests.RequestException as exc:
            _fail(ctx, f"cannot remove {target} via {base}: {exc}")
            return
    elif not _local_list(config).remove(target):
        _fail(ctx, f"{target} is not in the blocklist")
        return
    if _json_output(ctx):
        click.echo(_dumps({"removed": target}))
    elif not ctx.obj.quiet:
        console.print(f"[green]Unblocked {target}[/green]")
