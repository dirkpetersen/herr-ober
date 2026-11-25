"""Tests for BGP health hysteresis, maintenance mode and the VRRP notify hook."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands.health import Hysteresis
from ober.commands.maintenance import (
    is_maintenance,
    read_json,
    record_vrrp_state,
    set_maintenance,
)


def _feed(h: Hysteresis, probes: str, maint: bool = False, now: float = 100.0) -> list[bool]:
    """Feed probes ("1" good, "0" bad) and return the up state after each one."""
    out = []
    for p in probes:
        h.update(p == "1", maint, now)
        out.append(h.up)
    return out


class TestHysteresis:
    def test_rise_and_fall(self) -> None:
        h = Hysteresis(rise=3, fall=2, holddown_until=0)
        assert _feed(h, "11100110") == [False, False, True, True, False, False, False, False]

    def test_single_failure_does_not_withdraw(self) -> None:
        h = Hysteresis(rise=1, fall=2, holddown_until=0)
        assert _feed(h, "1010") == [True, True, True, True]

    def test_holddown_blocks_first_announce(self) -> None:
        h = Hysteresis(rise=1, fall=1, holddown_until=50)
        assert h.update(True, False, 10) is None
        assert h.update(True, False, 60) is not None
        assert h.up

    def test_maintenance_withdraws_at_once_and_blocks_announce(self) -> None:
        h = Hysteresis(rise=1, fall=5, holddown_until=0)
        _feed(h, "1")
        assert h.update(True, True, 1) == "maintenance mode is on"
        assert _feed(h, "111", maint=True) == [False, False, False]
        assert _feed(h, "1") == [True]


class TestHealthOnce:
    def test_once_exit_codes(self, cli_runner: CliRunner) -> None:
        with patch("ober.commands.health._check_health", return_value=True) as chk:
            result = cli_runner.invoke(main, ["health", "--once", "--port", "9999"])
            assert result.exit_code == 0
            assert chk.call_args[0][0] == "http://127.0.0.1:9999/health"
        with patch("ober.commands.health._check_health", return_value=False):
            result = cli_runner.invoke(main, ["health", "--once", "--port", "9999"])
            assert result.exit_code == 1
            assert result.output == ""


class TestHealthLoop:
    def test_loop_writes_state_and_only_exabgp_on_stdout(self, temp_dir: Path) -> None:
        from ober.commands import health as hmod
        from ober.config import OberConfig, VIPConfig

        config = OberConfig(install_path=temp_dir)
        config.vips = [VIPConfig(address="10.0.100.1/32")]
        config.bgp.health_rise = 1
        config.bgp.health_holddown = 0
        states: list[dict[str, object]] = []

        def sleep(_s: float) -> None:
            states.append(read_json(config.health_state_path))
            if len(states) >= 2:
                hmod._running = False

        runner = CliRunner()
        with (
            patch.object(hmod.OberConfig, "load", return_value=config),
            patch.object(hmod, "_check_health", return_value=True),
            patch.object(hmod.time, "sleep", side_effect=sleep),
            patch.object(hmod.signal, "signal"),
        ):
            hmod._running = True
            result = runner.invoke(hmod.health, [])
        hmod._running = True
        assert result.exit_code == 0
        assert states[0]["announced"] == ["10.0.100.1/32"]
        assert read_json(config.health_state_path)["announced"] == []
        lines = [line for line in result.stdout.splitlines() if line]
        assert lines == [
            "announce route 10.0.100.1/32 next-hop self",
            "withdraw route 10.0.100.1/32 next-hop self",
        ]


class TestMaintenanceFlag:
    def test_flag_values(self, temp_dir: Path) -> None:
        flag = temp_dir / "etc" / "maintenance"
        assert not is_maintenance(flag)
        set_maintenance(flag, True)
        assert flag.read_text() == "1\n"
        assert is_maintenance(flag)
        set_maintenance(flag, False)
        assert flag.read_text() == "0\n"
        assert not is_maintenance(flag)

    def test_cli_on_off_status(self, cli_runner: CliRunner, temp_dir: Path) -> None:
        from ober.config import OberConfig

        config = OberConfig(install_path=temp_dir)
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.maintenance.OberConfig.load", return_value=config),
        ):
            mock_system.return_value.is_root = True
            assert cli_runner.invoke(main, ["maintenance", "on"]).exit_code == 0
            assert is_maintenance(config.maintenance_path)
            result = cli_runner.invoke(main, ["maintenance", "status"])
            assert "ON" in result.output
            assert cli_runner.invoke(main, ["maintenance", "off"]).exit_code == 0
            assert not is_maintenance(config.maintenance_path)

    def test_cli_requires_root(self, cli_runner: CliRunner) -> None:
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_system.return_value.is_root = False
            assert cli_runner.invoke(main, ["maintenance", "on"]).exit_code == 1


class TestVrrpNotify:
    def test_record_states(self, temp_dir: Path) -> None:
        state = temp_dir / "var" / "run" / "vrrp-state.json"
        record_vrrp_state(state, "INSTANCE", "VI_1", "MASTER")
        record_vrrp_state(state, "INSTANCE", "VI_2", "BACKUP")
        record_vrrp_state(state, "GROUP", "G1", "FAULT")
        assert json.loads(state.read_text()) == {"VI_1": "MASTER", "VI_2": "BACKUP"}
        record_vrrp_state(state, "INSTANCE", "VI_1", "STOP")
        assert json.loads(state.read_text()) == {"VI_2": "BACKUP"}

    def test_hook_cli(self, cli_runner: CliRunner, temp_dir: Path) -> None:
        state = temp_dir / "vrrp-state.json"
        args = ["maintenance", "vrrp-notify", "--state-file", str(state)]
        result = cli_runner.invoke(main, [*args, "INSTANCE", "VI_1", "FAULT", "90"])
        assert result.exit_code == 0
        assert json.loads(state.read_text()) == {"VI_1": "FAULT"}


class TestKeepalivedChecks:
    def _generate(self, temp_dir: Path, gateway: str = "") -> str:
        from ober.commands.config import _generate_keepalived_config
        from ober.config import KeepalivedConfig, OberConfig, VIPConfig

        config = OberConfig(install_path=temp_dir)
        config.keepalived = KeepalivedConfig(
            peers=["10.0.0.2"], interface="eth0", gateway_check=gateway
        )
        config.vips = [VIPConfig(address="192.168.1.100/32")]
        with patch("ober.system.SystemInfo.get_local_ip", return_value="10.0.0.1"):
            _generate_keepalived_config(config)
        assert config.maintenance_path.read_text() == "0\n"
        return config.keepalived_config_path.read_text()

    def test_python_check_track_file_and_notify(self, temp_dir: Path) -> None:
        content = self._generate(temp_dir)
        assert "curl" not in content
        assert f"{sys.executable} -m ober.commands.health --once --timeout 1 --port 8404" in content
        assert f'file "{temp_dir}/etc/maintenance"' in content
        assert "track_file {\n        ober_maintenance\n    }" in content
        assert f"vrrp-notify --state-file {temp_dir}/var/run/vrrp-state.json" in content
        assert "chk_gateway" not in content

    def test_gateway_check(self, temp_dir: Path) -> None:
        content = self._generate(temp_dir, "10.0.0.254")
        assert "/usr/bin/ping -c 1 -W 1 10.0.0.254" in content
        assert "        chk_gateway\n" in content

    def test_gateway_check_rejects_non_ip(self, temp_dir: Path) -> None:
        with pytest.raises(ValueError):
            self._generate(temp_dir, "10.0.0.1; rm -rf /")
