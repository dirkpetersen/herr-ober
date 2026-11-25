#!/usr/bin/env python3
"""Ober cert command: check a PEM and put it into the running HAProxy without a reload.

``ober cert install`` checks that the key matches the certificate, that it is not expired and
that its SAN covers ``security.s3_hostnames``. It writes the file (mode 600) to ``certs.path``
and swaps it in the running HAProxy over the admin socket (``set ssl cert`` and
``commit ssl cert``), so no connection is dropped and no worker is started. When HAProxy does
not know the file yet (a new file in a certificate directory), it reloads instead.
"""

import os
import shlex
import ssl
import tempfile
import time
from pathlib import Path
from typing import TextIO

import click
from rich.console import Console
from rich.markup import escape

from ober.certs import CERT_FAIL_DAYS
from ober.commands.cluster import run_on_other_nodes
from ober.config import OberConfig, write_atomic
from ober.system import ServiceInfo, SystemInfo, run_command
from ober.watch.runtime import HAProxyRuntime

console = Console()


def _covered(name: str, sans: list[str]) -> bool:
    """True when a SAN DNS name matches ``name`` (a wildcard covers one label)."""
    parent = name.split(".", 1)[1] if "." in name else ""
    return any(s.lower() == name or (s.startswith("*.") and s[2:].lower() == parent) for s in sans)


def check_pem(text: str, hostnames: list[str]) -> tuple[list[str], list[str]]:
    """Errors and warnings for a combined certificate plus key PEM."""
    errors: list[str] = []
    warnings: list[str] = []
    fd, tmp = tempfile.mkstemp(suffix=".pem")  # mode 600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tmp)
        except ssl.SSLError as e:
            return [f"Certificate and key do not load or do not match: {e.reason or e}"], []
        # shortcut: private stdlib decoder, as in ober.certs (no `cryptography` dependency)
        info = ssl._ssl._test_decode_cert(tmp)  # type: ignore[attr-defined]
    finally:
        os.unlink(tmp)
    days = int((ssl.cert_time_to_seconds(info["notAfter"]) - time.time()) // 86400)
    if days < 0:
        errors.append(f"The certificate expired {-days} days ago")
    elif days < CERT_FAIL_DAYS:
        warnings.append(f"The certificate expires in {days} days")
    sans = [value for kind, value in info.get("subjectAltName", ()) if kind == "DNS"]
    for host in hostnames:
        if not _covered(host, sans):
            errors.append(f"SAN does not cover {host} (has: {', '.join(sans) or 'none'})")
        elif not _covered(f"bucket.{host}", sans):
            warnings.append(f"No *.{host} in the SAN: virtual-host bucket names fail TLS checks")
    return errors, warnings


def hot_swap(runtime: HAProxyRuntime, crt_path: str, text: str) -> str | None:
    """Replace a loaded certificate in the running HAProxy. Returns an error text or None."""
    answer = runtime.send(f"set ssl cert {crt_path}", payload=text)
    if "Transaction" not in answer:  # "Transaction created/updated for certificate ..."
        return answer.strip() or "no answer"
    answer = runtime.send(f"commit ssl cert {crt_path}")
    if "Success" not in answer:
        runtime.send(f"abort ssl cert {crt_path}")
        return answer.strip() or "no answer"
    return None


def _target(config: OberConfig, source: str, name: str | None) -> Path:
    crt = Path(config.certs.path)
    if crt.is_dir():
        name = name or (None if source == "-" else Path(source).name)
        if not name or "/" in name:
            raise click.UsageError("certs.path is a directory: give --name FILE.pem")
        return crt / name
    return crt


@click.group()
def cert() -> None:
    """Install TLS certificates."""


@cert.command("install")
@click.argument("pem", type=click.File("r"))
@click.option("--all-nodes", is_flag=True, help="Also on every other node ('nodes:'), over SSH.")
@click.option("--name", help="File name in a certs.path directory (default: the PEM's name).")
@click.pass_context
def install(ctx: click.Context, pem: TextIO, all_nodes: bool, name: str | None) -> None:
    """Check PEM (certificate plus key, '-' = stdin) and swap it in without a reload."""
    system = ctx.obj.system if ctx.obj else SystemInfo()
    if not system.is_root:
        console.print("[red]Error:[/red] This needs root. Use sudo.")
        ctx.exit(1)
    config = OberConfig.load()
    if not config.certs.path:
        console.print("[red]Error:[/red] certs.path is not set in ober.yaml.")
        ctx.exit(1)
    text = pem.read()
    errors, warnings = check_pem(text, config.security.s3_hostnames)
    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(w)}")
    if errors:
        for e in errors:
            console.print(f"[red]Error:[/red] {escape(e)}")
        ctx.exit(1)

    target = _target(config, pem.name, name)
    write_atomic(target, text, mode=0o600)
    if ServiceInfo.from_service_name("ober-http").is_active:
        try:
            error = hot_swap(HAProxyRuntime(config.admin_socket_path), str(target), text)
        except OSError as e:
            error = str(e)
        if error:
            console.print(f"[dim]Runtime swap not possible ({escape(error)}); reloading.[/dim]")
            result = run_command(["systemctl", "reload", "ober-http"], check=False)
            if result.returncode != 0:
                console.print(f"[red]Error:[/red] reload failed: {escape(result.stderr)}")
                ctx.exit(1)
    console.print(f"[green]Installed[/green] {escape(str(target))}")
    if all_nodes:
        remote = f"sudo -n ober cert install --name {shlex.quote(target.name)} -"
        run_on_other_nodes(ctx, remote, stdin=text)
