#!/usr/bin/env python3
"""Ober cluster init: set up a keepalived cluster from a list of IPs, a backend and a cert.

``ober cluster init 10.0.0.11 10.0.0.12 10.0.0.13 10.0.0.100 --backend s3.example.org``
logs in to each IP over SSH. An IP that a host has on a NIC (prefix shorter than /32) is a
node. The other IPs (no SSH, or held as /32 like a VIP) are floating IPs, one VIP each. A
floating IP that answers ping or ARP and is not on one of the nodes is in use, and init stops.
Init installs and bootstraps ober where needed, writes ober.yaml and ober.local.yaml
(``local_ip``) on each node, renders the configs and starts ober, one node at a time.
"""

import ipaddress
import json
import os
import re
import shlex
import ssl
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import click
import yaml
from rich.console import Console
from rich.markup import escape

from ober import __version__
from ober.commands.cert import check_pem
from ober.commands.cluster import _run_on
from ober.config import LOCAL_CONFIG_NAME, OberConfig

console = Console()

PROBE = (
    "cat /etc/machine-id; echo ---; ip -o -4 addr show; echo ---; command -v ober; echo ---;"
    " cat /etc/ober/install_path 2>/dev/null; true"
)
INSTALL = (
    "curl -fsSL https://raw.githubusercontent.com/dirkpetersen/herr-ober/v{v}/install.sh"
    " | sudo -n bash"
)
_ADDR_RE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", re.M)
_ROUTE_RE = re.compile(r"\bdev\s+(\S+).*?\bsrc\s+(\S+)")


@dataclass
class Host:
    """What one SSH login to an IP found."""

    ip: str
    machine_id: str = ""
    addrs: list[tuple[str, str, int]] = field(default_factory=list)  # (dev, ip, prefix)
    has_ober: bool = False
    install_path: str = ""
    error: str = ""

    @property
    def dev(self) -> str:
        """The NIC that holds this IP."""
        return next((d for d, a, _ in self.addrs if a == self.ip), "")

    @property
    def is_node(self) -> bool:
        """The host has this IP on a NIC as a normal address (a VIP is a /32)."""
        return any(a == self.ip and p < 32 for _, a, p in self.addrs)


def parse_probe(ip: str, rc: int, out: str) -> Host:
    """Read the output of PROBE."""
    parts = out.split("---\n")
    if rc != 0 or len(parts) < 4:
        return Host(ip, error=(out.strip().splitlines() or [f"exit {rc}"])[-1][:200])
    return Host(
        ip,
        machine_id=parts[0].strip(),
        addrs=[(d, a, int(p)) for d, a, p in _ADDR_RE.findall(parts[1])],
        has_ober=bool(parts[2].strip()),
        install_path=parts[3].strip(),
    )


def classify(hosts: list[Host]) -> tuple[list[Host], list[str], list[str]]:
    """Nodes, floating IPs and errors. Two IPs of one machine as nodes is an error."""
    nodes = [h for h in hosts if h.is_node]
    floating = [h.ip for h in hosts if not h.is_node]
    errors = []
    seen: dict[str, str] = {}
    for h in nodes:
        if h.machine_id in seen:
            errors.append(f"{h.ip} and {seen[h.machine_id]} are the same host")
        seen[h.machine_id] = h.ip
    held = {a for h in nodes for _, a, _ in h.addrs}
    for ip in floating:
        on_host = next((h for h in hosts if h.ip == ip and not h.error), None)
        if on_host and ip not in held:  # SSH works and the IP is a /32, but not on a node
            errors.append(f"{ip} is a /32 on a host that is not in the list")
    if len(nodes) < 2:
        errors.append(f"need 2 or more nodes, found {len(nodes)}")
    if not floating:
        errors.append("no floating IP: give one IP that no node has")
    return nodes, floating, errors


def parse_backend(text: str) -> tuple[str, bool]:
    """``host[:port]`` or ``https://host[/...]`` -> (``host:port``, tls). Default: 443, TLS."""
    url = text.strip()
    scheme, _, rest = url.rpartition("://")
    hostport = rest.split("/", 1)[0]
    host, _, port = hostport.rpartition(":") if ":" in hostport else (hostport, "", "")
    if not host:
        raise click.BadParameter(f"no host in {text!r}")
    port = port or ("80" if scheme == "http" else "443")
    if not port.isdigit():
        raise click.BadParameter(f"bad port in {text!r}")
    return f"{host}:{port}", scheme == "https" or (scheme != "http" and port == "443")


def cert_hostnames(text: str) -> list[str]:
    """The SAN DNS names of a PEM without wildcards (``*.x`` covers buckets of ``x``)."""
    fd, tmp = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        info = ssl._ssl._test_decode_cert(tmp)  # type: ignore[attr-defined]
    finally:
        os.unlink(tmp)
    names = [v.lower() for k, v in info.get("subjectAltName", ()) if k == "DNS"]
    return [n for n in dict.fromkeys(names) if not n.startswith("*.")]


def build_config(
    nodes: list[str],
    floating: list[str],
    backends: list[tuple[str, bool]],
    cert_path: str,
    hostnames: list[str],
    allow: list[str],
) -> dict[str, Any]:
    """The shared ober.yaml (no per-node values) as a dict."""
    internet = [ip for ip in floating if ipaddress.ip_address(ip).is_global]
    data: dict[str, Any] = {
        "ha_mode": "keepalived",
        "nodes": nodes,
        "keepalived": {"peers": nodes},
        "vips": [
            {"address": ip, "exposure": "internet" if ip in internet else "internal"}
            for ip in floating
        ],
        "backends": [
            {
                "name": "s3",
                "servers": [s for s, _ in backends],
                "tls": backends[0][1],
                "health_check_expect": "rstatus ^[234]",  # S3 answers / with 403 without auth
            }
        ],
        "certs": {"path": cert_path},
        "security": {"s3_hostnames": hostnames},
    }
    if internet:
        data["security"]["allowlist"] = allow
    return data


def _in_use(probe_node: str, config: OberConfig, ip: str) -> bool:
    """True when ``ip`` answers ping or has a MAC in the probe node's ARP table."""
    q = shlex.quote(ip)
    _, out = _run_on(
        probe_node, config, f"ping -c 2 -W 1 {q} >/dev/null 2>&1 && echo ANSWER; ip neigh show {q}"
    )
    return "ANSWER" in out or "lladdr" in out


def _backend_routes(node: Host, config: OberConfig, servers: list[str]) -> list[str]:
    """One line per backend server: the NIC and source address this node uses for it."""
    lines = []
    nics = {d for d, _, p in node.addrs if d != "lo" and p < 32}  # lo-vip has only /32s
    for server in servers:
        host = shlex.quote(server.rsplit(":", 1)[0])
        cmd = f"ip route get $(getent ahostsv4 {host} | awk 'NR==1{{print $1}}')"
        rc, out = _run_on(node.ip, config, cmd)
        m = _ROUTE_RE.search(out)
        if rc != 0 or not m:
            lines.append(f"[red]{node.ip}: cannot resolve or route {escape(server)}[/red]")
            continue
        dev, src = m.groups()
        note = ""
        if dev == node.dev and len(nics) > 1:
            note = " [yellow](same NIC as the frontend, though the host has more NICs)[/yellow]"
        lines.append(f"{node.ip}: {escape(server)} -> {dev}, source {src}{note}")
    return lines


def _existing_cert(nodes: list[Host], config: OberConfig) -> tuple[str, str] | None:
    """(path, PEM) of a certificate that a node already uses, or None."""
    for node in nodes:
        _, out = _run_on(node.ip, config, "sudo -n ober --json status")  # exit 1/2 = not OK
        try:  # stderr follows the JSON in ``out``
            data = json.JSONDecoder().raw_decode(out[out.find("{") :])[0]
            path = data.get("certificate", {}).get("path", "")
        except (AttributeError, ValueError):
            path = ""
        if not path:
            continue
        rc, pem = _run_on(node.ip, config, f"sudo -n cat {shlex.quote(path)}")
        if rc == 0 and "PRIVATE KEY" in pem:
            return path, pem
    return None


def run_init(
    ips: list[str], backend_specs: list[str], cert_text: str | None, allow: list[str], yes: bool
) -> bool:
    """Set up the cluster. Returns False on any error (already printed)."""
    config = OberConfig.load()
    for ip in ips:
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            console.print(f"[red]Error:[/red] {escape(ip)} is not an IPv4 address")
            return False
    try:
        backends = [parse_backend(b) for b in backend_specs]
    except click.BadParameter as exc:
        console.print(f"[red]Error:[/red] {escape(exc.message)}")
        return False
    if not backends:
        console.print("[red]Error:[/red] give a backend")
        return False
    if len({tls for _, tls in backends}) > 1:
        console.print("[red]Error:[/red] mix of TLS and plain backends")
        return False

    with ThreadPoolExecutor(max_workers=min(16, len(ips))) as pool:
        hosts = list(pool.map(lambda ip: parse_probe(ip, *_run_on(ip, config, PROBE)), ips))
    nodes, floating, errors = classify(hosts)
    held = {a for h in nodes for _, a, _ in h.addrs}
    for ip in floating:
        if ip not in held and nodes and _in_use(nodes[0].ip, config, ip):
            errors.append(f"{ip} is not on a node but answers ping or ARP: it is in use")
    paths = {h.install_path for h in nodes if h.install_path}
    if len(paths) > 1:
        errors.append(f"nodes use different install paths: {', '.join(sorted(paths))}")
    for h in hosts:
        if h.error:
            console.print(f"[dim]{escape(h.ip)}: no SSH ({escape(h.error)})[/dim]")
    if errors:
        for e in errors:
            console.print(f"[red]Error:[/red] {escape(e)}")
        return False

    install = paths.pop() if paths else "/opt/herr-ober"
    cert_path = f"{install}/etc/certs/server.pem"
    if cert_text is None:
        found = _existing_cert(nodes, config)
        if not found:
            console.print("[red]Error:[/red] no node has a certificate yet: give --cert PEM")
            return False
        cert_path, cert_text = found
        console.print(f"Using the certificate the nodes have: {escape(cert_path)}")
    hostnames = cert_hostnames(cert_text)
    cert_errors, cert_warnings = check_pem(cert_text, hostnames)
    for w in cert_warnings:
        console.print(f"[yellow]Warning:[/yellow] {escape(w)}")
    if cert_errors:
        for e in cert_errors:
            console.print(f"[red]Error:[/red] {escape(e)}")
        return False
    data = build_config([h.ip for h in nodes], floating, backends, cert_path, hostnames, allow)
    if "allowlist" in data["security"] and not allow:
        public = [v["address"] for v in data["vips"] if v["exposure"] == "internet"]
        console.print(
            f"[red]Error:[/red] {', '.join(public)} is public. Give --allow CIDR for the client"
            " networks (internet VIPs deny all other sources), or --allow 0.0.0.0/0."
        )
        return False

    console.print(f"[bold]Nodes:[/bold] {', '.join(h.ip for h in nodes)}")
    console.print(f"[bold]Floating IPs (VIPs):[/bold] {', '.join(floating)}")
    console.print(f"[bold]Backend:[/bold] {', '.join(s for s, _ in backends)}")
    console.print(f"[bold]S3 hostnames (from the cert):[/bold] {', '.join(hostnames) or 'any'}")
    for node in nodes:
        for line in _backend_routes(node, config, [s for s, _ in backends]):
            console.print(line)
    if not yes:
        click.confirm("Set up ober on these nodes, one at a time?", abort=True)

    text = yaml.safe_dump(data, sort_keys=False)
    for node in nodes:
        console.print(f"[bold]== {node.ip}[/bold]")
        etc = shlex.quote(f"{install}/etc")
        steps: list[tuple[str, str | None]] = []
        if not node.has_ober:
            steps.append((INSTALL.format(v=__version__), None))
        if not node.install_path:
            steps.append(("sudo -n ober bootstrap -y", None))
        cert_q = shlex.quote(cert_path)
        cert_dir, new = shlex.quote(os.path.dirname(cert_path)), shlex.quote(cert_path + ".new")
        steps += [
            (  # the cert first: haproxy -c fails without it
                "sudo -n sh -c "
                + shlex.quote(
                    f"umask 077; mkdir -p {cert_dir} && cat > {new} && mv {new} {cert_q}"
                ),
                cert_text,
            ),
            (f"sudo -n tee {etc}/{LOCAL_CONFIG_NAME} >/dev/null", f"local_ip: {node.ip}\n"),
            (
                # same mode running: gated reload; else the mode switch (stop, render, start)
                "if systemctl is-active -q ober-http && systemctl is-active -q ober-ha; then"
                " sudo -n ober apply --no-diff -f -; else sudo -n ober stop </dev/null >/dev/null"
                f" 2>&1; sudo -n tee {etc}/ober.yaml >/dev/null"
                " && sudo -n ober config --non-interactive; fi",
                text,
            ),
            ("sudo -n ober start", None),
        ]
        for cmd, stdin in steps:
            rc, out = _run_on(node.ip, config, cmd, stdin=stdin)
            if rc != 0:
                console.print(out.rstrip(), markup=False)
                console.print(f"[red]Init stopped:[/red] {escape(node.ip)}: {escape(cmd[:60])}")
                return False
    console.print("[green]Cluster set up.[/green] Check: ober cluster status")
    return True


@click.command("init")
@click.argument("ips", nargs=-1, required=True)
@click.option(
    "--backend",
    "backends",
    multiple=True,
    required=True,
    help="S3 endpoint host[:port] or https://host (repeat for more). Port 443 uses TLS.",
)
@click.option("--cert", type=click.File("r"), help="PEM (cert plus key). Default: the nodes' one.")
@click.option("--allow", multiple=True, help="Client CIDR allowed on public VIPs (repeat).")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def init(
    ctx: click.Context,
    ips: tuple[str, ...],
    backends: tuple[str, ...],
    cert: Any,
    allow: tuple[str, ...],
    yes: bool,
) -> None:
    """Set up a keepalived cluster from node IPs plus floating IPs (found over SSH)."""
    text = cert.read() if cert else None
    if not run_init(list(dict.fromkeys(ips)), list(backends), text, list(allow), yes):
        ctx.exit(1)
