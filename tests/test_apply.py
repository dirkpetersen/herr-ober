"""Tests for ober apply / rollback: snapshot, check, reload, health gate, restore."""

import re
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands import apply as ap
from ober.config import OberConfig


@pytest.fixture
def node(sample_config: OberConfig) -> Iterator[dict[str, Any]]:
    """A rendered node with HAProxy running and healthy; every system call is mocked."""
    sample_config.save()
    state: dict[str, Any] = {"check_rc": 0, "healthy": [True], "cmds": []}

    def run(cmd: list[str], **_kw: Any) -> MagicMock:
        state["cmds"].append(cmd)
        rc = state["check_rc"] if "-c" in cmd or "-t" in cmd else 0
        return MagicMock(returncode=rc, stdout="", stderr="bad line 3" if rc else "")

    def healthy(_config: OberConfig) -> bool:
        seq = state["healthy"]
        return bool(seq.pop(0) if len(seq) > 1 else seq[0])

    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch(
            "ober.commands.apply.OberConfig.load",
            side_effect=lambda path=None: _reload(sample_config, path),
        ),
        patch("ober.commands.apply.run_command", side_effect=run),
        patch("ober.commands.apply.ServiceInfo") as svc,
        patch("ober.commands.apply._healthy", side_effect=healthy),
        patch("ober.commands.apply.save_server_state"),
        patch("ober.commands.apply.time.sleep"),
        patch("ober.commands.config._configure_vip_interface") as vip,
    ):
        svc.from_service_name.side_effect = lambda name: MagicMock(
            is_active=name not in state["inactive"]
        )
        state["inactive"] = {"ober-ha"}
        state["vip"] = vip
        from ober.commands.config import _apply_configuration

        _apply_configuration(sample_config, is_root=False)
        state["config"] = sample_config
        yield state


_LOAD = OberConfig.load


def _reload(config: OberConfig, path: Any = None) -> OberConfig:
    with patch.dict("os.environ", {"OBER_INSTALL_PATH": str(config.install_path)}):
        return _LOAD(path or config.config_path)


def _edit(config: OberConfig, server: str) -> None:
    text = config.config_path.read_text().replace("rgw2:7480", server)
    config.config_path.write_text(text)


def test_no_change(node: dict[str, Any]) -> None:
    result = CliRunner().invoke(main, ["apply"])
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output
    assert ap._snapshots(node["config"]) == []  # nothing applied, nothing stored


def test_apply_reloads_and_keeps_snapshot(node: dict[str, Any]) -> None:
    config = node["config"]
    _edit(config, "rgw3:7480")
    result = CliRunner().invoke(main, ["apply"])
    assert result.exit_code == 0, result.output
    assert "+    server rgw3_7480 rgw3:7480" in result.output
    assert ["systemctl", "reload", "ober-http"] in node["cmds"]
    assert ["pkill", "-HUP", "-f", "ober[.]commands[.]health"] in node["cmds"]  # VIP re-read
    assert "rgw3_7480" in config.haproxy_config_path.read_text()
    base, applied = ap._snapshots(config)  # first apply: the baseline plus the new state
    assert "rgw2_7480" in (base / "haproxy/haproxy.cfg").read_text()
    assert "rgw3:7480" in (applied / "ober.yaml").read_text()


def test_failed_check_restores(node: dict[str, Any]) -> None:
    config = node["config"]
    _edit(config, "rgw3:7480")
    node["check_rc"] = 1
    result = CliRunner().invoke(main, ["apply"])
    assert result.exit_code == 1
    assert "bad line 3" in result.output
    assert "rgw2_7480" in config.haproxy_config_path.read_text()
    assert ["systemctl", "reload", "ober-http"] not in node["cmds"]


def test_failed_health_gate_rolls_back(node: dict[str, Any]) -> None:
    config = node["config"]
    _edit(config, "rgw3:7480")
    node["healthy"] = [True] + [False] * 40 + [True]  # before: OK, after reload: never OK
    result = CliRunner().invoke(main, ["apply", "--timeout", "0"])
    assert result.exit_code == 1, result.output
    assert "did not become OK" in result.output
    assert "Apply failed:" in result.output and "[red]" not in result.output
    assert "rgw3:7480" in config.config_path.read_text()  # the edit stays, to fix and retry
    assert "rgw2_7480" in config.haproxy_config_path.read_text()
    assert ap._snapshots(config) == []


def test_rollback_and_undo(node: dict[str, Any]) -> None:
    config = node["config"]
    _edit(config, "rgw3:7480")
    assert CliRunner().invoke(main, ["apply"]).exit_code == 0
    config.config_path.write_text(config.config_path.read_text().replace("rgw3:7480", "rgw4:7480"))
    assert CliRunner().invoke(main, ["apply"]).exit_code == 0
    result = CliRunner().invoke(main, ["rollback"])
    assert result.exit_code == 0, result.output
    assert "rgw3_7480" in config.haproxy_config_path.read_text()
    assert "rgw3:7480" in config.config_path.read_text()  # ober.yaml goes back too
    undo = result.output.split("ober rollback ")[-1].split()[0]
    assert CliRunner().invoke(main, ["rollback", undo]).exit_code == 0
    assert "rgw4_7480" in config.haproxy_config_path.read_text()


def test_snapshot_skips_certs_and_blocklist(node: dict[str, Any]) -> None:
    config = node["config"]
    (config.certs_path / "s3.pem").write_text("PRIVATE KEY")
    config.blocklist_path.write_text("192.0.2.1\n")
    snap = ap._save(config, ap._tracked(config))
    names = [p.name for p in snap.rglob("*")]
    assert "s3.pem" not in names and "blocklist.lst" not in names
    assert "ober.yaml" in names


def test_snapshot_skips_binary_files(node: dict[str, Any]) -> None:
    # install path = venv: pip writes etc/exabgp/examples/__pycache__/*.pyc
    config = node["config"]
    (config.install_path / "etc" / "x.pyc").write_bytes(b"\x00\xb8\xff")
    assert "x.pyc" not in ap._tracked(config)
    assert "ober.yaml" in ap._tracked(config)


@pytest.mark.usefixtures("sample_config")
def test_needs_root() -> None:
    with patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=False)):
        assert CliRunner().invoke(main, ["apply"]).exit_code == 1


def test_prune_keeps_newest(temp_config: OberConfig) -> None:
    for i in range(ap.KEEP_SNAPSHOTS + 3):
        (ap._history(temp_config) / f"2026010{i:02d}").mkdir(parents=True)
    ap._save(temp_config, {})
    snaps = ap._snapshots(temp_config)
    assert len(snaps) == ap.KEEP_SNAPSHOTS and snaps[0].name == "202601004"


@pytest.mark.usefixtures("node")
def test_unknown_snapshot() -> None:
    assert CliRunner().invoke(main, ["rollback", "nope"]).exit_code == 1


def test_refuses_mode_switch(node: dict[str, Any]) -> None:
    config = node["config"]
    config.config_path.write_text(
        config.config_path.read_text().replace("ha_mode: bgp", "ha_mode: keepalived")
    )
    node["inactive"] = {"ober-ha"}  # ober-bgp still runs
    result = CliRunner().invoke(main, ["apply"])
    assert result.exit_code == 1
    assert "ober-bgp runs" in result.output
    assert "keepalived" not in config.haproxy_config_path.read_text()  # nothing rendered


def test_rollback_resyncs_lo_vip(node: dict[str, Any]) -> None:
    config = node["config"]
    _edit(config, "rgw3:7480")
    assert CliRunner().invoke(main, ["apply"]).exit_code == 0
    node["vip"].reset_mock()
    assert CliRunner().invoke(main, ["rollback"]).exit_code == 0
    node["vip"].assert_called_once()


def test_apply_file_installs_and_restores_on_failure(node: dict[str, Any]) -> None:
    config = node["config"]
    old = config.config_path.read_text()
    new = old.replace("rgw2:7480", "rgw3:7480")
    node["healthy"] = [True, False]  # healthy before, never after the reload
    result = CliRunner().invoke(main, ["apply", "--timeout", "1", "-f", "-"], input=new)
    assert result.exit_code == 1, result.output
    assert config.config_path.read_text() == old  # -f: the old ober.yaml comes back
    node["healthy"] = [True]
    result = CliRunner().invoke(main, ["apply", "-f", "-"], input=new)
    assert result.exit_code == 0, result.output
    assert config.config_path.read_text() == new
    assert "rgw3_7480" in config.haproxy_config_path.read_text()


def test_apply_file_refuses_invalid_or_other_install_path(node: dict[str, Any]) -> None:
    config = node["config"]
    old = config.config_path.read_text()
    moved = re.sub(r"^install_path: .*$", "install_path: /elsewhere", old, flags=re.M)
    assert moved != old
    for bad in ("ha_mode: carrier-pigeon\n", moved):
        result = CliRunner().invoke(main, ["apply", "-f", "-"], input=bad)
        assert result.exit_code == 1, result.output
        assert "not valid on this node" in result.output
        assert config.config_path.read_text() == old
        assert not config.config_path.with_name(".ober.yaml.new").exists()
