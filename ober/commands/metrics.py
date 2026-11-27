#!/usr/bin/env python3
"""Ober metrics command: node state in the Prometheus text format.

HAProxy already serves request and backend metrics on ``:<stats_port>/metrics``. This adds what
HAProxy does not know: VIP state, maintenance, certificate expiry, config hash and versions.
Write it for the node_exporter textfile collector with ``--output`` from a timer or cron.
The alert rules that use it are in ``deploy/prometheus/alerts.yml``.
"""

from pathlib import Path
from typing import Any

import click

from ober.commands.status import LEVELS, collect_status, load_config_safe
from ober.config import write_atomic


def _labels(**labels: Any) -> str:
    def esc(v: Any) -> str:
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in labels.items()) + "}" if labels else ""


def _num(value: float) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def render(s: dict[str, Any]) -> str:
    """Prometheus text for one ``collect_status`` result."""
    out: list[str] = []

    def metric(name: str, help_text: str, samples: list[tuple[dict[str, Any], float]]) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} gauge")
        out.extend(f"{name}{_labels(**lab)} {_num(val)}" for lab, val in samples)

    metric(
        "ober_check_level",
        "ober status --check: 0 OK, 1 WARN, 2 CRIT.",
        [({}, LEVELS[s["check"]["level"]])],
    )
    metric(
        "ober_service_active",
        "1 when the systemd unit is active.",
        [({"unit": name}, bool(svc["active"])) for name, svc in s["services"].items()],
    )
    metric("ober_maintenance", "1 when maintenance mode is on.", [({}, bool(s["maintenance"]))])
    vips = s["vips"]
    metric(
        "ober_vip_present",
        "1 when the VIP is on a local interface.",
        [({"vip": v["address"]}, bool(v["present"])) for v in vips if v["present"] is not None],
    )
    if s["ha_mode"] == "bgp":
        metric(
            "ober_vip_announced",
            "1 when this node announces the VIP over BGP.",
            [
                ({"vip": v["address"]}, bool(v["announced"]))
                for v in vips
                if v.get("announced") is not None
            ],
        )
        bfd = (s.get("bgp") or {}).get("bfd")
        if bfd is not None:
            metric(
                "ober_bfd_peer_up",
                "1 when the BFD session (FRR bfdd) to the BGP neighbor is up.",
                [({"peer": peer}, up) for peer, up in bfd.items()],
            )
    else:
        metric(
            "ober_vrrp_master",
            "1 when this node is VRRP MASTER for the VIP.",
            [({"vip": v["address"]}, v.get("vrrp_state") == "MASTER") for v in vips],
        )
    backends = s.get("backends") or {}
    metric(
        "ober_backend_servers_up",
        "RGW servers UP in the backend.",
        [({"backend": name}, b["up"]) for name, b in backends.items()],
    )
    metric(
        "ober_backend_servers",
        "RGW servers in the backend.",
        [({"backend": name}, b["total"]) for name, b in backends.items()],
    )
    days = (s.get("certificate") or {}).get("days_left")
    if days is not None:
        metric(
            "ober_cert_remaining_seconds",
            "Time until the first certificate expires (whole days).",
            [({}, days * 86400)],
        )
    metric(
        "ober_config_info",
        "Hash of haproxy.cfg; differs between nodes on drift.",
        [({"hash": s.get("config_hash") or ""}, 1)],
    )
    versions = {
        "ober": (s.get("ober") or {}).get("version"),
        "haproxy": s["haproxy"]["version"],
        "exabgp": (s.get("bgp") or {}).get("version"),
        "keepalived": (s.get("keepalived") or {}).get("version"),
    }
    metric(
        "ober_version_info", "Installed versions.", [({k: v or "" for k, v in versions.items()}, 1)]
    )
    return "\n".join(out) + "\n"


@click.command()
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write to this file atomically (node_exporter textfile dir, name ending in .prom).",
)
def metrics(output: Path | None) -> None:
    """Print node metrics in the Prometheus text format (run as root for backend state)."""
    text = render(collect_status(*load_config_safe()))
    if output:
        write_atomic(output, text)
    else:
        click.echo(text, nl=False)
