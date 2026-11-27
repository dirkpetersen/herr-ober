#!/usr/bin/env python3
"""Ober apply and rollback: change the running node safely.

``ober apply`` renders the configs from ober.yaml, checks them, reloads HAProxy (and
keepalived) and waits until ``/health`` is OK. If a step fails, it puts the old files back
and reloads again. Each apply first saves the generated files to ``<install>/var/history``,
which ``ober rollback`` restores.

When servers added to or removed from a backend are the only change in haproxy.cfg, apply
uses the HAProxy runtime API (``add server`` / ``del server``) instead of a reload. Any
failure there falls back to the normal reload.

Not in a snapshot: certificates (keys must not be copied around), the blocklist (it is
managed by ``ober blocklist sync``) and the ``lo-vip`` network files outside the install path.
"""

import difflib
import ipaddress
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import TextIO

import click
from rich.console import Console
from rich.markup import escape

from ober.commands.backend import _servers, save_server_state
from ober.config import LOCAL_CONFIG_NAME, OberConfig, exit_if_invalid, write_atomic
from ober.system import ServiceInfo, SystemInfo, run_command
from ober.watch.runtime import HAProxyRuntime

console = Console()

KEEP_SNAPSHOTS = 20


def _history(config: OberConfig) -> Path:
    return config.install_path / "var" / "history"


def _tracked(config: OberConfig) -> dict[str, str]:
    """Relative path -> text of every file under <install>/etc that a snapshot covers."""
    etc = config.install_path / "etc"
    skip = {config.blocklist_path}
    files = {}
    for path in sorted(etc.rglob("*")):
        if not path.is_file() or path in skip or config.certs_path in path.parents:
            continue
        if path.name.startswith("."):  # tmp files of write_atomic
            continue
        try:
            files[str(path.relative_to(etc))] = path.read_text()
        except UnicodeDecodeError:  # not ours: pip puts ExaBGP's example .pyc here in a venv
            continue
    return files


def _save(config: OberConfig, files: dict[str, str]) -> Path:
    """Store one applied state and drop the oldest beyond KEEP_SNAPSHOTS."""
    history = _history(config)
    snap = history / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while snap.exists():  # two applies in one second
        snap = history / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        n += 1
    snap.mkdir(parents=True)
    for rel, text in files.items():
        write_atomic(snap / rel, text)
    for old in _snapshots(config)[:-KEEP_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)
    return snap


def _snapshots(config: OberConfig) -> list[Path]:
    history = _history(config)
    return sorted(p for p in history.iterdir() if p.is_dir()) if history.is_dir() else []


def _load(snap: Path) -> dict[str, str]:
    return {str(p.relative_to(snap)): p.read_text() for p in snap.rglob("*") if p.is_file()}


def _write(config: OberConfig, files: dict[str, str]) -> None:
    """Make the tracked files equal to ``files``: write them, remove the others."""
    etc = config.install_path / "etc"
    for rel in _tracked(config).keys() - files.keys():
        (etc / rel).unlink()
    for rel, text in files.items():
        write_atomic(etc / rel, text)


def _diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for rel in sorted(before.keys() | after.keys()):
        old, new = before.get(rel, ""), after.get(rel, "")
        if old != new:
            lines += difflib.unified_diff(
                old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm=""
            )
    return lines


def _healthy(config: OberConfig) -> bool:
    url = f"http://127.0.0.1:{config.stats_port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (fixed local URL)
            return bool(resp.status == 200)
    except OSError:
        return False


def _wait_healthy(config: OberConfig, timeout: int) -> bool:
    """True after 3 good checks in a row (new workers answer, old ones are gone)."""
    deadline, good = time.time() + timeout, 0
    time.sleep(1)
    while time.time() < deadline:
        good = good + 1 if _healthy(config) else 0
        if good >= 3:
            return True
        time.sleep(1)
    return False


# (backend, server, "addr options" to add it, or None to delete it)
ServerPlan = list[tuple[str, str, str | None]]


def _backend_servers(
    cfg: str,
) -> tuple[list[str], dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Split haproxy.cfg into the non-server lines, the server tokens and the default-server
    options of each backend."""
    rest: list[str] = []
    servers: dict[str, dict[str, list[str]]] = {}
    defaults: dict[str, list[str]] = {}
    backend = None
    for line in cfg.splitlines():
        if line and not line[0].isspace():
            backend = line.split()[1] if line.startswith("backend ") else None
        words = line.split()
        if backend and len(words) >= 3 and words[0] == "server":
            servers.setdefault(backend, {})[words[1]] = words[2:]
            continue
        if backend and words[:1] == ["default-server"]:
            defaults[backend] = words[1:]
        rest.append(line)
    return rest, servers, defaults


def server_plan(old: str, new: str) -> ServerPlan | None:
    """Runtime API steps when only servers were added or removed, else None (reload).

    Dynamic servers do not inherit ``default-server``, so its options are repeated. A server
    with a host name is not added at runtime: HAProxy cannot resolve it without resolvers.
    """
    old_rest, old_srv, _ = _backend_servers(old)
    new_rest, new_srv, defaults = _backend_servers(new)
    if old_rest != new_rest:
        return None
    adds: ServerPlan = []
    dels: ServerPlan = []
    for backend in sorted(old_srv.keys() | new_srv.keys()):
        before, after = old_srv.get(backend, {}), new_srv.get(backend, {})
        if any(after[name] != before[name] for name in after.keys() & before.keys()):
            return None
        for name in sorted(after.keys() - before.keys()):
            addr, *opts = after[name]
            try:
                ipaddress.ip_address(addr.rsplit(":", 1)[0].strip("[]"))
            except ValueError:
                return None
            adds.append((backend, name, " ".join([addr, *defaults.get(backend, []), *opts])))
        dels += [(backend, name, None) for name in sorted(before.keys() - after.keys())]
    return (adds + dels) or None  # adds first: the backend never runs short of servers


def _expect(runtime: HAProxyRuntime, command: str, answer: str) -> None:
    got = runtime.send(command).strip()
    if got != answer:
        raise RuntimeError(f"'{command}': {got or 'no answer'}")


def _run_plan(config: OberConfig, plan: ServerPlan, timeout: int) -> str | None:
    """Add and delete servers over the admin socket. Returns an error text, or None."""
    runtime = HAProxyRuntime(config.admin_socket_path)
    try:
        for backend, name, args in plan:
            srv = f"{backend}/{name}"
            if args is not None:
                _expect(runtime, f"add server {srv} {args}", "New server registered.")
                if "check" in args.split():
                    _expect(runtime, f"enable health {srv}", "")
                _expect(runtime, f"enable server {srv}", "")
                continue
            _expect(runtime, f"set server {srv} state maint", "")
            deadline = time.time() + timeout
            while time.time() < deadline:  # drain: open sessions finish first
                rows = [r for r in _servers(runtime, backend) if r.get("svname") == name]
                if not rows or int(rows[0].get("scur") or 0) == 0:
                    break
                time.sleep(1)
            _expect(runtime, f"del server {srv}", "Server deleted.")
    except (OSError, RuntimeError, click.ClickException) as e:
        return str(e)
    return None


def _reload(config: OberConfig, ka: bool, ha_changed: bool) -> str | None:
    reloads = [["systemctl", "reload", "ober-http"]]
    if ka:
        reloads.append(["systemctl", "reload", "ober-ha"])
    for cmd in reloads:
        result = run_command(cmd, check=False)
        if result.returncode != 0:
            return f"{' '.join(cmd)} failed:\n{result.stdout}{result.stderr}"
    if config.ha_mode == "bgp" and ServiceInfo.from_service_name("ober-bgp").is_active:
        # The VIP list is not in the ExaBGP config: the health process re-reads it on SIGHUP
        run_command(["pkill", "-HUP", "-f", "ober[.]commands[.]health"], check=False)
        if ha_changed:
            console.print("[yellow]The ExaBGP config changed: run 'ober restart --hard'.[/yellow]")
    return None


def _activate(
    config: OberConfig,
    timeout: int,
    ha_changed: bool,
    restore: bool = False,
    plan: ServerPlan | None = None,
) -> str | None:
    """Check, reload (or run ``plan`` over the runtime API) and gate on health.

    Returns an error text, or None when it worked. ``restore``: the old files come back
    after a failure, so /health failing now is expected and the gate still applies.
    """
    checks = [["/usr/sbin/haproxy", "-c", "-f", str(config.haproxy_config_path)]]
    ka = config.ha_mode == "keepalived" and ServiceInfo.from_service_name("ober-ha").is_active
    if ka:
        checks.append(["/usr/sbin/keepalived", "-t", "-f", str(config.keepalived_config_path)])
    for cmd in checks:
        result = run_command(cmd, check=False)
        if result.returncode != 0:
            return f"{cmd[0]} check failed:\n{result.stdout}{result.stderr}"

    if not ServiceInfo.from_service_name("ober-http").is_active:
        console.print("[dim]HAProxy is not running: files written, nothing reloaded.[/dim]")
        return None
    was_healthy = _healthy(config)
    # Saved before the runtime steps: after a failed step, the reload must not load a
    # half-added server in maintenance from the state file
    save_server_state(config)
    runtime_error = _run_plan(config, plan, timeout) if plan else None
    if plan and runtime_error is None:
        done = ", ".join(f"{'+' if a else '-'}{b}/{n}" for b, n, a in plan)
        console.print(f"Runtime API, no reload: {done}", markup=False)
        save_server_state(config)
    else:
        if runtime_error:
            console.print(f"Runtime API failed ({runtime_error}), reloading.", markup=False)
        error = _reload(config, ka, ha_changed)
        if error:
            return error
        console.print("Reloaded HAProxy.")

    if not was_healthy and not restore:  # a gate that never passes would undo every change
        console.print("[yellow]Warning:[/yellow] /health was failing before; no health gate.")
        return None
    if not _wait_healthy(config, timeout):
        return f"/health did not become OK within {timeout}s after the reload"
    return None


def _root_config(ctx: click.Context) -> OberConfig:
    system = ctx.obj.system if ctx.obj else SystemInfo()
    if not system.is_root:
        console.print("[red]Error:[/red] This needs root. Use sudo.")
        ctx.exit(1)
    return OberConfig.load()


@click.command()
@click.option("--timeout", default=30, show_default=True, help="Seconds to wait for /health.")
@click.option("--diff/--no-diff", default=True, show_default=True, help="Show the changes.")
@click.option(
    "-f",
    "--file",
    "new_file",
    type=click.File("r"),
    help="Install this ober.yaml first ('-' = stdin). A failure restores the old one too.",
)
@click.pass_context
def apply(ctx: click.Context, timeout: int, diff: bool, new_file: TextIO | None) -> None:
    """Apply ober.yaml: render, check, reload, health gate, roll back on failure."""
    from ober.commands.config import _apply_configuration

    config = _root_config(ctx)
    before = _tracked(config)
    if new_file is not None:
        config = _install(ctx, config, new_file.read())
    exit_if_invalid(config, ctx)
    other = "ober-ha" if config.ha_mode == "bgp" else "ober-bgp"
    if ServiceInfo.from_service_name(other).is_active:
        # A reload cannot switch modes: the old unit would keep the VIPs that we remove
        console.print(f"[red]Error:[/red] ha_mode is {config.ha_mode}, but {other} runs.")
        console.print("Switch with: ober stop; ober config --non-interactive; ober start")
        _write(config, before)
        ctx.exit(1)
    try:
        _apply_configuration(config)
    except Exception as e:
        _write(config, before)
        console.print(f"[red]Error:[/red] Rendering failed, old files restored: {e}")
        ctx.exit(1)
    after = _tracked(config)
    if before == after:
        console.print("[green]No changes.[/green]")
        return
    if diff:
        for line in _diff(before, after):
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else ""
            console.print(f"[{style}]{line}[/{style}]" if style else line, markup=bool(style))

    error = _activate(
        config, timeout, _bgp_changed(config, before, after), plan=_plan(config, before, after)
    )
    if error:
        console.print(f"[red]Apply failed:[/red] {escape(error)}")
        # Without -f, ober.yaml keeps the edit, so it can be fixed and applied again;
        # with -f, the old ober.yaml comes back
        _write(config, before)
        if _activate(config, timeout, False, restore=True):
            console.print("[red]The old files did not get healthy either. Check the node.[/red]")
        else:
            console.print("[yellow]Rolled back to the previous files.[/yellow]")
        ctx.exit(1)
    if not _snapshots(config):
        # shortcut: the first baseline has the already edited ober.yaml; the generated files
        # are the ones that ran before.
        _save(config, before)
    snap = _save(config, after)
    console.print(f"[green]Applied[/green] as snapshot {snap.name}. Undo with: ober rollback")


def _install(ctx: click.Context, config: OberConfig, text: str) -> OberConfig:
    """Replace ober.yaml with ``text`` when it is valid here (with this node's ober.local.yaml)."""
    new = config.config_path.with_name(".ober.yaml.new")  # same dir: the overlay applies
    write_atomic(new, text)
    candidate = OberConfig.load(new)
    errors, _ = candidate.validate()
    if candidate.install_path != config.install_path:
        errors.append(f"install_path {candidate.install_path} is not {config.install_path}")
    if errors:
        new.unlink()
        console.print("[red]Error:[/red] The new ober.yaml is not valid on this node:")
        for err in errors:
            console.print(f"  {err}", markup=False)
        ctx.exit(1)
    os.replace(new, config.config_path)
    return OberConfig.load()


def _sync_vips() -> None:
    """Make lo-vip match the restored ober.yaml (it is outside the snapshot)."""
    from ober.commands.config import _configure_vip_interface

    _configure_vip_interface(OberConfig.load())


def _plan(config: OberConfig, before: dict[str, str], after: dict[str, str]) -> ServerPlan | None:
    """A runtime server plan when haproxy.cfg is the only generated file that changed."""
    etc = config.install_path / "etc"
    cfg = str(config.haproxy_config_path.relative_to(etc))
    sources = {config.config_path.name, LOCAL_CONFIG_NAME}  # ober.yaml itself may differ
    changed = {rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)}
    if cfg not in before or cfg not in after or not changed <= sources | {cfg}:
        return None
    return server_plan(before[cfg], after[cfg])


def _bgp_changed(config: OberConfig, before: dict[str, str], after: dict[str, str]) -> bool:
    rel = str(config.bgp_config_path.relative_to(config.install_path / "etc"))
    return before.get(rel) != after.get(rel)


@click.command()
@click.argument("snapshot", required=False)
@click.option("--list", "list_only", is_flag=True, help="List the snapshots, newest last.")
@click.option("--timeout", default=30, show_default=True, help="Seconds to wait for /health.")
@click.pass_context
def rollback(ctx: click.Context, snapshot: str | None, list_only: bool, timeout: int) -> None:
    """Go back to SNAPSHOT (default: the one before the newest) and reload.

    Each apply and rollback stores the applied state, so a second rollback undoes the first.
    """
    config = _root_config(ctx)
    snaps = _snapshots(config)
    if list_only:
        for snap in snaps:
            click.echo(snap.name)
        return
    if snapshot:
        target = next((s for s in snaps if s.name == snapshot), None)
    else:
        target = snaps[-2] if len(snaps) >= 2 else None
    if target is None:
        console.print(f"[red]Error:[/red] No snapshot {snapshot or 'to go back to'}.")
        ctx.exit(1)
        return
    current, files = _tracked(config), _load(target)
    _write(config, files)
    _sync_vips()
    error = _activate(config, timeout, _bgp_changed(config, current, files))
    if error:
        console.print(f"[red]Rollback failed:[/red] {escape(error)}")
        _write(config, current)
        _sync_vips()
        _activate(config, timeout, False, restore=True)
        ctx.exit(1)
    _save(config, files)
    console.print(
        f"[green]Restored {target.name}.[/green] Undo with: ober rollback {snaps[-1].name}"
    )
