"""BFD in BGP mode: FRR bfdd config, ober-bfd unit, status, doctor and metrics."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ober import config as ober_config
from ober.commands import config as cfg_cmd
from ober.commands import doctor, status
from ober.commands.bootstrap import _create_systemd_services
from ober.commands.metrics import render
from ober.config import OberConfig


@pytest.fixture
def frr() -> Path:
    """Make FRR look installed (the conftest fixture points BFDD_PATHS at a temp file)."""
    bfdd = ober_config.BFDD_PATHS[0]
    bfdd.write_text("")
    return bfdd


def test_inactive_without_frr_or_flag(sample_config: OberConfig, frr: Path) -> None:
    assert sample_config.bfd_active()
    sample_config.ha_mode = "keepalived"
    assert not sample_config.bfd_active()
    sample_config.ha_mode = "bgp"
    sample_config.bgp.bfd_enabled = False
    assert not sample_config.bfd_active()
    sample_config.bgp.bfd_enabled = True
    frr.unlink()  # old ober.yaml with bfd_enabled: true, frr not installed: nothing changes
    assert not sample_config.bfd_active()
    assert cfg_cmd._generate_bfdd_config(sample_config) is False
    assert not sample_config.bfdd_config_path.exists()


@pytest.mark.usefixtures("frr")
def test_render_one_peer_per_neighbor(sample_config: OberConfig) -> None:
    sample_config.bgp.bfd_interval_ms = 150
    sample_config.bgp.bfd_multiplier = 4
    assert cfg_cmd._generate_bfdd_config(sample_config) is True
    text = sample_config.bfdd_config_path.read_text()
    assert text.count(" peer ") == 2
    assert " peer 10.0.0.1 local-address 10.0.1.1\n  receive-interval 150\n" in text
    assert "  transmit-interval 150\n  detect-multiplier 4\n exit\n" in text
    assert "router bgp" not in text  # FRR never runs BGP or touches routes
    assert cfg_cmd._generate_bfdd_config(sample_config) is False  # unchanged: no restart


@pytest.mark.usefixtures("frr")
def test_render_falls_back_to_node_ip(sample_config: OberConfig) -> None:
    sample_config.bgp.local_address = ""
    sample_config.local_ip = "10.0.1.7"
    cfg_cmd._generate_bfdd_config(sample_config)
    assert "local-address 10.0.1.7\n" in sample_config.bfdd_config_path.read_text()


def test_timer_validation(sample_config: OberConfig) -> None:
    sample_config.bgp.bfd_interval_ms = 5
    sample_config.bgp.bfd_multiplier = 1
    errors, _ = sample_config.validate()
    assert any("bgp.bfd_interval_ms" in e for e in errors)
    assert any("bgp.bfd_multiplier" in e for e in errors)


def _svc(active: bool) -> MagicMock:
    return MagicMock(is_active=active)


@pytest.mark.usefixtures("frr")
def test_sync_unit(sample_config: OberConfig, tmp_path: Path) -> None:
    with (
        patch("ober.system.run_command") as run,
        patch("ober.system.ServiceInfo.from_service_name", return_value=_svc(True)),
    ):
        cfg_cmd.sync_bfd_unit(sample_config, changed=True)
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds == [["systemctl", "enable", "ober-bfd"], ["systemctl", "restart", "ober-bfd"]]

    # ober-bgp stopped: enable only, never start (BindsTo would pull ober-bgp up too)
    with (
        patch("ober.system.run_command") as run,
        patch("ober.system.ServiceInfo.from_service_name", return_value=_svc(False)),
    ):
        cfg_cmd.sync_bfd_unit(sample_config)
    assert [c.args[0] for c in run.call_args_list] == [["systemctl", "enable", "ober-bfd"]]

    sample_config.ha_mode = "keepalived"
    unit = tmp_path / "ober-bfd.service"
    with (
        patch("ober.system.run_command") as run,
        patch.object(cfg_cmd, "BFD_UNIT_PATH", unit),
    ):
        cfg_cmd.sync_bfd_unit(sample_config)  # no unit: nothing to do
        unit.write_text("")
        cfg_cmd.sync_bfd_unit(sample_config)
    assert [c.args[0] for c in run.call_args_list] == [
        ["systemctl", "disable", "--now", "ober-bfd"]
    ]


def test_unit_only_with_frr(sample_config: OberConfig, tmp_path: Path) -> None:
    with patch("ober.commands.bootstrap.run_command"):
        assert "ober-bfd.service" not in _create_systemd_services(sample_config, tmp_path, tmp_path)
        ober_config.BFDD_PATHS[0].write_text("")
        assert "ober-bfd.service" in _create_systemd_services(sample_config, tmp_path, tmp_path)
    unit = (tmp_path / "ober-bfd.service").read_text()
    assert "BindsTo=ober-bgp.service" in unit
    assert "WantedBy=ober-bgp.service" in unit
    assert f"ExecStart={ober_config.BFDD_PATHS[0]} -f {sample_config.bfdd_config_path}" in unit


def test_bfd_peers_parses_vtysh() -> None:
    peers = [{"peer": "10.0.0.1", "status": "up"}, {"peer": "10.0.0.2", "status": "down"}]
    out = MagicMock(returncode=0, stdout=json.dumps(peers))
    with patch.object(status.subprocess, "run", return_value=out):
        got = status.bfd_peers(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
    assert got == {"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": False}
    with patch.object(status.subprocess, "run", side_effect=OSError):
        assert status.bfd_peers(["10.0.0.1"]) is None
    bad = MagicMock(returncode=0, stdout="% bfdd is not running")
    with patch.object(status.subprocess, "run", return_value=bad):
        assert status.bfd_peers(["10.0.0.1"]) is None


def _result(bfd: Any) -> dict[str, Any]:
    return {
        "config": {"error": None, "exists": True, "path": "x"},
        "services": {},
        "backends": {},
        "bgp": {"sessions": {}, "bfd": bfd},
        "vips": [],
    }


def test_evaluate_warns_on_bfd_down() -> None:
    assert status.evaluate(_result({"10.0.0.1": True}))["level"] == "OK"
    check = status.evaluate(_result({"10.0.0.1": True, "10.0.0.2": False}))
    assert check["level"] == "WARN" and "BFD peer 10.0.0.2: down" in check["problems"]
    assert status.evaluate(_result(None))["problems"] == ["BFD state unknown (vtysh failed)"]


def test_metric() -> None:
    s: dict[str, Any] = {
        "ha_mode": "bgp",
        "services": {},
        "haproxy": {"version": None},
        "maintenance": False,
        "vips": [],
        "bgp": {"bfd": {"10.0.0.1": True, "10.0.0.2": False}},
        "check": {"level": "WARN"},
    }
    lines = render(s).splitlines()
    assert 'ober_bfd_peer_up{peer="10.0.0.1"} 1' in lines
    assert 'ober_bfd_peer_up{peer="10.0.0.2"} 0' in lines
    del s["bgp"]["bfd"]
    assert "ober_bfd_peer_up" not in render(s)


def test_doctor(sample_config: OberConfig, frr: Path) -> None:
    with (
        patch.object(doctor.ServiceInfo, "from_service_name", return_value=_svc(True)),
        patch.object(doctor, "bfd_peers", return_value={"10.0.0.1": True, "10.0.0.2": False}),
    ):
        check = doctor._check_bfd(sample_config)
    assert not check["passed"] and check["message"] == "down: 10.0.0.2"
    frr.unlink()
    check = doctor._check_bfd(sample_config)
    assert "FRR (bfdd) not installed" in check["message"]
