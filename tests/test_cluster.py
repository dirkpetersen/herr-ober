"""Tests for ober cluster status (node list, SSH collection, cluster-wide checks)."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ober.cli import main
from ober.commands.cluster import cluster_nodes, evaluate_cluster, gather
from ober.commands.status import config_hash
from ober.config import KeepalivedConfig, OberConfig


def _node(vrrp: dict[str, str] | None = None, announced: list[str] | None = None, **kw: Any) -> Any:
    vips = []
    for addr, state in (vrrp or {}).items():
        vips.append({"address": addr, "present": state == "MASTER", "vrrp_state": state})
    for addr in announced or []:
        vips.append({"address": addr, "present": True, "announced": True})
    status = {
        "services": {"ober-http": {"active": True}, "ober-ha": {"active": True}},
        "vips": vips,
        "backends": {"s3_backend": {"up": 2, "total": 2, "down": []}},
        "haproxy": {"version": "3.3.1"},
        "ober": {"version": "0.4.0"},
        "config_hash": "abc",
        "check": {"level": "OK", "problems": []},
    }
    status.update(kw)
    return status


def test_nodes_from_config_or_keepalived_peers(temp_dir: Path) -> None:
    config = OberConfig(install_path=temp_dir, local_ip="10.0.0.1", ha_mode="keepalived")
    config.keepalived = KeepalivedConfig(peers=["10.0.0.1", "10.0.0.2"])
    assert cluster_nodes(config) == ["10.0.0.1", "10.0.0.2"]
    config.nodes = ["a", "b", "a"]
    assert cluster_nodes(config) == ["a", "b"]
    assert cluster_nodes(OberConfig(install_path=temp_dir)) == []  # BGP needs nodes:


def test_healthy_keepalived_cluster() -> None:
    nodes = {
        "a": _node({"10.1.0.1": "MASTER", "10.1.0.2": "BACKUP"}),
        "b": _node({"10.1.0.1": "BACKUP", "10.1.0.2": "MASTER"}),
    }
    check = evaluate_cluster("keepalived", nodes)
    assert check["level"] == "OK", check


def test_vip_on_no_node_or_two_nodes_is_crit() -> None:
    # The gap from the lab: every node is BACKUP, and each one alone says OK.
    none = {"a": _node({"v": "BACKUP"}), "b": _node({"v": "BACKUP"})}
    assert evaluate_cluster("keepalived", none)["problems"] == [
        "v: not held (VRRP MASTER) on any node"
    ]
    split = {"a": _node({"v": "MASTER"}), "b": _node({"v": "MASTER"})}
    check = evaluate_cluster("keepalived", split)
    assert check["level"] == "CRIT"
    assert "MASTER on 2 nodes" in check["summary"]
    bgp = {"a": _node(announced=[]), "b": _node(announced=["v"])}
    assert evaluate_cluster("bgp", bgp)["level"] == "OK"


def test_unreachable_node_and_drift() -> None:
    nodes = {
        "a": _node({"v": "MASTER"}),
        "b": _node({"v": "BACKUP"}, config_hash="zzz", ober={"version": "0.3.9"}),
        "c": {"error": "Connection refused"},
    }
    check = evaluate_cluster("keepalived", nodes)
    assert check["level"] == "CRIT"
    assert "c: unreachable (Connection refused)" in check["problems"]
    assert "config differs between nodes" in check["problems"]
    assert "ober version differs between nodes" in check["problems"]
    assert "HAProxy differs between nodes" not in check["problems"]


def test_gather_reads_remote_and_reports_ssh_errors(temp_dir: Path) -> None:
    config = OberConfig(install_path=temp_dir, local_ip="10.0.0.1")
    good = subprocess.CompletedProcess([], 0, json.dumps(_node({"v": "MASTER"})), "")
    bad = subprocess.CompletedProcess([], 255, "", "ssh: connect to host c: No route to host\n")

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["ssh", "-o", "BatchMode=yes"]
        return good if "b" in cmd else bad

    with (
        patch("ober.commands.cluster.subprocess.run", side_effect=fake_run),
        patch("ober.commands.cluster.collect_status", return_value=_node({"v": "BACKUP"})),
        patch("ober.commands.cluster.load_config_safe", return_value=(config, None)),
    ):
        out = gather(config, ["10.0.0.1", "b", "c"], timeout=1)
    assert out["10.0.0.1"]["vips"][0]["vrrp_state"] == "BACKUP"  # local, no SSH
    assert out["b"]["vips"][0]["vrrp_state"] == "MASTER"
    assert out["c"] == {"error": "ssh: connect to host c: No route to host"}


def test_cli_exit_code_and_json(temp_dir: Path) -> None:
    config = OberConfig(install_path=temp_dir, ha_mode="keepalived")
    results = {"a": _node({"v": "BACKUP"}), "b": _node({"v": "BACKUP"})}
    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch("ober.commands.cluster.load_config_safe", return_value=(config, None)),
        patch("ober.commands.cluster.gather", return_value=results),
    ):
        text = CliRunner().invoke(main, ["cluster", "status", "--nodes", "a,b"])
        js = CliRunner().invoke(main, ["--json", "cluster", "status", "--nodes", "a,b"])
        none = CliRunner().invoke(main, ["cluster", "status"])
    assert text.exit_code == 2, text.output
    assert "not held" in text.output
    assert json.loads(js.output)["check"]["level"] == "CRIT"
    assert none.exit_code == 1 and "No nodes" in none.output


def test_config_hash_ignores_per_node_stats_bind(temp_dir: Path) -> None:
    a, b, c = temp_dir / "a", temp_dir / "b", temp_dir / "c"
    a.write_text("frontend stats\n    bind 127.0.0.1:8404,10.0.0.1:8404\n    maxconn 10\n")
    b.write_text("frontend stats\n    bind 127.0.0.1:8404,10.0.0.2:8404\n    maxconn 10\n")
    c.write_text("frontend stats\n    bind 127.0.0.1:8404\n    maxconn 20\n")
    assert config_hash(a) == config_hash(b) != config_hash(c)
    assert config_hash(temp_dir / "missing") is None
