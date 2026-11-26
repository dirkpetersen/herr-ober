"""Tests for BGP health hysteresis, maintenance mode and the VRRP notify hook."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from ober.config import OberConfig


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
        flag = temp_dir / "var" / "run" / "maintenance"
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
        assert f'file "{temp_dir}/var/run/maintenance"' in content
        assert "track_file {\n        ober_maintenance\n    }" in content
        assert f"vrrp-notify --state-file {temp_dir}/var/run/vrrp-state.json" in content
        assert "chk_gateway" not in content

    def test_fast_failover_slow_return(self, temp_dir: Path) -> None:
        # lab: preempt_delay 30 also made backups keep a failing master for 30 s
        content = self._generate(temp_dir)
        assert "preempt_delay" not in content
        assert "    vrrp_startup_delay 30\n" in content  # a started owner waits 30 s
        assert "    rise 15 " in content and "    init_fail " in content  # health flaps
        assert "    fall 2 " in content

    def test_gateway_check(self, temp_dir: Path) -> None:
        content = self._generate(temp_dir, "10.0.0.254")
        assert "/usr/bin/ping -c 1 -W 1 10.0.0.254" in content
        assert "        chk_gateway\n" in content

    def test_gateway_check_rejects_non_ip(self, temp_dir: Path) -> None:
        with pytest.raises(ValueError):
            self._generate(temp_dir, "10.0.0.1; rm -rf /")


class TestStopDrain:
    """`ober stop` and `restart --hard`: maintenance, rotation wait, drain."""

    def _config(self, temp_dir: Path, mode: str) -> OberConfig:
        config = OberConfig(install_path=temp_dir)
        config.ha_mode = mode
        config.drain_timeout = 3
        return config

    def test_drain_bgp_waits_for_withdraw_and_connections(self, temp_dir: Path) -> None:
        from ober.commands import service

        config = self._config(temp_dir, "bgp")
        config.health_state_path.parent.mkdir(parents=True)
        config.health_state_path.write_text('{"announced": [], "updated": 1}')
        conns = iter([4, 1, 0])
        runtime = MagicMock()
        runtime.send.side_effect = lambda _c: f"Name: HAProxy\nCurrConns: {next(conns)}\n"
        with (
            patch.object(service, "HAProxyRuntime", return_value=runtime),
            patch.object(service.time, "sleep") as sleep,
        ):
            assert service._drain(config, ha_active=True) is True
        assert is_maintenance(config.maintenance_path)
        assert sleep.call_count == 2

    def test_drain_keeps_maintenance_set_by_operator(self, temp_dir: Path) -> None:
        from ober.commands import service

        config = self._config(temp_dir, "keepalived")
        set_maintenance(config.maintenance_path, True)
        config.vrrp_state_path.parent.mkdir(parents=True, exist_ok=True)
        config.vrrp_state_path.write_text('{"VI_1": "MASTER"}')
        runtime = MagicMock()
        runtime.send.side_effect = OSError("no socket")
        with (
            patch.object(service, "HAProxyRuntime", return_value=runtime),
            patch.object(service.time, "sleep") as sleep,
        ):
            assert service._drain(config, ha_active=True) is False
        # VIP never moved: waited the full rotation timeout, then went on
        assert sleep.call_count == service.ROTATION_TIMEOUT

    def test_empty_vrrp_state_is_not_out_of_rotation(self, temp_dir: Path) -> None:
        from ober.commands import service

        config = self._config(temp_dir, "keepalived")
        assert service._out_of_rotation(config) is False  # no state file yet
        config.vrrp_state_path.parent.mkdir(parents=True)
        config.vrrp_state_path.write_text('{"VI_1": "BACKUP"}')
        assert service._out_of_rotation(config) is True

    def test_interrupted_drain_turns_maintenance_off(self, temp_dir: Path) -> None:
        from ober.commands import service

        config = self._config(temp_dir, "bgp")
        with (
            patch.object(service, "_wait_out_and_drain", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            service._drain(config, ha_active=True)
        assert not is_maintenance(config.maintenance_path)

    def test_empty_flag_file_is_off(self, temp_dir: Path) -> None:
        flag = temp_dir / "maintenance"
        flag.write_text("")
        assert not is_maintenance(flag)

    def test_stop_clears_only_its_own_maintenance(
        self, cli_runner: CliRunner, temp_dir: Path
    ) -> None:
        config = self._config(temp_dir, "bgp")
        svc = MagicMock(is_active=True)

        def drain(cfg: OberConfig, _ha: bool) -> bool:
            set_maintenance(cfg.maintenance_path, True)
            return True

        with (
            patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
            patch("ober.commands.service.OberConfig.load", return_value=config),
            patch("ober.commands.service.ServiceInfo.from_service_name", return_value=svc),
            patch("ober.commands.service.run_command") as run,
            patch("ober.commands.service._drain", side_effect=drain),
        ):
            result = cli_runner.invoke(main, ["stop"])
        assert result.exit_code == 0, result.output
        assert not is_maintenance(config.maintenance_path)
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds.index(["systemctl", "stop", "ober-http"]) < cmds.index(
            ["systemctl", "stop", "ober-bgp"]
        )

    def test_restart_hard_drains_first(self, cli_runner: CliRunner, temp_dir: Path) -> None:
        config = self._config(temp_dir, "bgp")
        svc = MagicMock(is_active=True, is_enabled=True)
        order: list[str] = []
        with (
            patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
            patch("ober.commands.service.OberConfig.load", return_value=config),
            patch("ober.commands.service.ServiceInfo.from_service_name", return_value=svc),
            patch(
                "ober.commands.service.run_command",
                side_effect=lambda c, **_: order.append(" ".join(c)) or MagicMock(returncode=0),
            ),
            patch("ober.commands.service._drain", side_effect=lambda *_: order.append("drain")),
        ):
            result = cli_runner.invoke(main, ["restart", "--hard"])
        assert result.exit_code == 0, result.output
        assert order.index("drain") < order.index("systemctl restart ober-http")


def test_sighup_reload_announces_new_and_withdraws_removed_vips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ober.commands import health as h

    with patch.object(h, "_config_vips", return_value=["10.0.0.2", "10.0.0.3"]):
        vips, routes = h._reload_vips(["10.0.0.1", "10.0.0.2"], up=True)
    assert vips == ["10.0.0.2", "10.0.0.3"] and routes == ["10.0.0.2/32", "10.0.0.3/32"]
    assert capsys.readouterr().out.splitlines() == [
        "withdraw route 10.0.0.1/32 next-hop self",
        "announce route 10.0.0.3/32 next-hop self",
    ]
    with patch.object(h, "_config_vips", return_value=["10.0.0.3"]):
        h._reload_vips(["10.0.0.2"], up=False)  # unhealthy: withdraw only, no announce
    assert capsys.readouterr().out == "withdraw route 10.0.0.2/32 next-hop self\n"
    with patch.object(h, "_config_vips", side_effect=ValueError("bad yaml")):
        assert h._reload_vips(["10.0.0.2"], up=True) == (["10.0.0.2"], ["10.0.0.2/32"])
    assert capsys.readouterr().out == ""  # a broken file changes no route
