"""Tests for ober cluster status (node list, SSH collection, cluster-wide checks)."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ober.cli import main
from ober.commands.cluster import (
    cluster_nodes,
    evaluate_cluster,
    gather,
    shared_per_node_values,
)
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


def test_local_overlay_merges_and_saves_back(temp_dir: Path) -> None:
    etc = temp_dir / "etc"
    etc.mkdir()
    (etc / "ober.yaml").write_text(
        "ha_mode: bgp\nbgp:\n  local_as: 65010\n  neighbors: [10.0.0.254]\n"
    )
    (etc / "ober.local.yaml").write_text("local_ip: 10.0.0.7\nbgp:\n  router_id: 10.0.0.7\n")
    with patch.dict("os.environ", {"OBER_INSTALL_PATH": str(temp_dir)}):
        config = OberConfig.load()
    assert (config.local_ip, config.bgp.router_id, config.bgp.local_as) == (
        "10.0.0.7",
        "10.0.0.7",
        65010,
    )
    config.bgp.router_id = "10.0.0.8"
    config.bgp.local_as = 65020
    config.save()
    shared = (etc / "ober.yaml").read_text()
    assert shared_per_node_values(shared) == []  # per-node values stay in the local file
    assert "65020" in shared
    assert "10.0.0.8" in (etc / "ober.local.yaml").read_text()


def _push(temp_dir: Path, text: str, rcs: dict[str, int]) -> tuple[Any, list[str]]:
    (temp_dir / "etc").mkdir(exist_ok=True)
    (temp_dir / "etc" / "ober.yaml").write_text(text)
    config = OberConfig(install_path=temp_dir, local_ip="10.0.0.1", nodes=["10.0.0.1", *rcs])
    sent: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        node = cmd[cmd.index("--") + 1]
        sent.append(node)
        assert kw["input"] == text and cmd[-1].endswith("ober apply --timeout 30 -f -")
        return subprocess.CompletedProcess(cmd, rcs[node], f"applied on {node}\n", "")

    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch("ober.commands.cluster.load_config_safe", return_value=(config, None)),
        patch("ober.commands.cluster.subprocess.run", side_effect=fake_run),
    ):
        return CliRunner().invoke(main, ["push", "-y"]), sent


def test_push_one_node_at_a_time_and_stops_at_failure(temp_dir: Path) -> None:
    result, sent = _push(temp_dir, "ha_mode: bgp\n", {"b": 0, "c": 1, "d": 0})
    assert result.exit_code == 1, result.output
    assert sent == ["b", "c"]  # never the local node, nothing after the failure
    assert "Not pushed: d" in result.output
    result, sent = _push(temp_dir, "ha_mode: bgp\n", {"b": 0, "c": 0})
    assert result.exit_code == 0 and sent == ["b", "c"], result.output


def test_push_refuses_per_node_values(temp_dir: Path) -> None:
    result, sent = _push(temp_dir, "bgp:\n  router_id: 10.0.0.1\n", {"b": 0})
    assert result.exit_code == 1 and sent == []
    assert "bgp.router_id" in result.output and "ober.local.yaml" in result.output


def _upgrade(
    temp_dir: Path, fail: str = "", drained: bool = True, upgrade_out: str = "", mode: str = "bgp"
) -> tuple[Any, list[tuple[str, str]]]:
    config = OberConfig(
        install_path=temp_dir, local_ip="10.0.0.1", nodes=["10.0.0.1", "b"], ha_mode=mode
    )
    ran: list[tuple[str, str]] = []

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        node = "local" if cmd[:2] == ["sh", "-c"] else cmd[cmd.index("--") + 1]
        ran.append((node, cmd[-1]))
        rc = 1 if fail and fail in cmd[-1] else 0
        out = upgrade_out if cmd[-1].endswith("ober upgrade -y") else "ok"
        return subprocess.CompletedProcess(cmd, rc, out, "")

    held = _node(announced=[] if drained else ["v"])
    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch("ober.commands.cluster.load_config_safe", return_value=(config, None)),
        patch("ober.commands.cluster.subprocess.run", side_effect=fake_run),
        patch(
            "ober.commands.cluster.gather", side_effect=lambda _c, ns, _t: dict.fromkeys(ns, held)
        ),
        patch("ober.commands.cluster.time.sleep"),
    ):
        return CliRunner().invoke(main, ["cluster", "upgrade", "-y", "--timeout", "0"]), ran


def test_cluster_upgrade_runs_runbook_this_node_last(temp_dir: Path) -> None:
    result, ran = _upgrade(temp_dir)
    assert result.exit_code == 0, result.output
    steps = [cmd for node, cmd in ran if node == "b"]
    assert steps == [
        "sudo -n ober maintenance on",
        "sudo -n sh -c 'PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade herr-ober'",
        "sudo -n ober upgrade -y",
        "sudo -n ober restart --hard",  # always in BGP mode
        "sudo -n ober doctor",
        "sudo -n ober maintenance off",
    ]
    assert [node for node, _ in ran] == ["b"] * 6 + ["local"] * 6


def test_cluster_upgrade_restarts_keepalived_only_when_asked(temp_dir: Path) -> None:
    result, ran = _upgrade(temp_dir, mode="keepalived")
    assert result.exit_code == 0 and not any("restart" in c for _, c in ran)
    out = "Run 'sudo ober restart --hard' once, in a maintenance window"
    result, ran = _upgrade(temp_dir, mode="keepalived", upgrade_out=out)
    assert sum("restart --hard" in c for _, c in ran) == 2


def test_cluster_upgrade_stops_in_maintenance(temp_dir: Path) -> None:
    result, ran = _upgrade(temp_dir, fail="doctor")
    assert result.exit_code == 1
    assert ran[-1] == ("b", "sudo -n ober doctor")  # no maintenance off, no next node
    assert "b stays in maintenance" in result.output
    result, ran = _upgrade(temp_dir, drained=False)  # still announces: no upgrade
    assert result.exit_code == 1 and "did not leave" in result.output
    assert ran == [("b", "sudo -n ober maintenance on")]
