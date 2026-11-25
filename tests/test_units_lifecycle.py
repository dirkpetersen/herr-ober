"""Tests for the systemd unit templates, reload/restart and upgrade lifecycle."""

import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands import bootstrap as bs
from ober.commands import upgrade as up
from ober.config import OberConfig


def _units(tmp: Path) -> dict[str, str]:
    config = OberConfig(install_path=Path("/opt/ober"))
    with patch.object(bs, "run_command"):
        bs._create_systemd_services(config, Path("/opt/pipx/venvs/herr-ober"), tmp)
    return {f.name: f.read_text() for f in tmp.iterdir()}


def test_http_unit_is_master_worker_with_validated_reload(temp_dir: Path) -> None:
    lines = _units(temp_dir)["ober-http.service"].splitlines()
    assert "Type=notify" in lines
    cfg = "/opt/ober/etc/haproxy/haproxy.cfg"
    assert f"ExecStartPre=/usr/sbin/haproxy -Ws -f {cfg} -c" in lines
    assert any(x.startswith(f"ExecStart=/usr/sbin/haproxy -Ws -f {cfg} ") for x in lines)
    reloads = [x for x in lines if x.startswith("ExecReload=")]
    # The check runs first, so a bad config fails the reload before USR2 is sent
    assert reloads == [
        f"ExecReload=/usr/sbin/haproxy -Ws -f {cfg} -c",
        "ExecReload=/bin/kill -USR2 $MAINPID",
    ]
    assert not any(" -D " in x for x in lines)


@pytest.mark.parametrize("name", ["ober-bgp.service", "ober-ha.service"])
def test_ha_units_bound_to_and_wanted_by_http(temp_dir: Path, name: str) -> None:
    lines = _units(temp_dir)[name].splitlines()
    assert "BindsTo=ober-http.service" in lines
    assert "WantedBy=multi-user.target ober-http.service" in lines


def test_rerender_reports_only_changed_units(temp_dir: Path) -> None:
    config = OberConfig(install_path=Path("/opt/ober"))
    venv = Path("/opt/pipx/venvs/herr-ober")
    (temp_dir / "ober-http.service").write_text("[Service]\nType=forking\n")
    with patch.object(bs, "run_command") as run:
        changed = bs._create_systemd_services(config, venv, temp_dir)
        assert sorted(changed) == ["ober-bgp.service", "ober-ha.service", "ober-http.service"]
        assert bs._create_systemd_services(config, venv, temp_dir) == []
    assert ["systemctl", "daemon-reload"] in [c.args[0] for c in run.call_args_list]


def test_bootstrap_disables_distro_units() -> None:
    with patch.object(bs, "run_command") as run:
        bs._disable_distro_units()
    run.assert_called_once_with(
        ["systemctl", "disable", "--now", "haproxy", "keepalived"], check=False
    )


def test_exabgp_pin_matches_bgp_extra() -> None:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
    assert extras["bgp"] == [bs.EXABGP_SPEC]


# --- start / restart -------------------------------------------------------------------------


def _service_env(ha_mode: str, check_rc: int = 0, reload_fails: bool = False) -> Any:
    """Patch service.py; return the list that collects every command run."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> MagicMock:
        calls.append(cmd)
        if reload_fails and cmd[:2] == ["systemctl", "reload"]:
            raise subprocess.CalledProcessError(1, cmd)
        rc = check_rc if "-c" in cmd or "-t" in cmd else 0
        return MagicMock(returncode=rc, stdout="", stderr="[ALERT] bad keyword")

    config = MagicMock()
    config.ha_mode = ha_mode
    config.validate.return_value = ([], [])
    svc = MagicMock(is_active=True, is_enabled=True)
    patches = [
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch("ober.commands.service.OberConfig.load", return_value=config),
        patch("ober.commands.service.ServiceInfo.from_service_name", return_value=svc),
        patch("ober.commands.service.run_command", side_effect=fake_run),
        patch("ober.commands.service.time.sleep"),
        patch("ober.commands.service._drain", return_value=False),
    ]
    return calls, patches


def _invoke(args: list[str], **env: Any) -> tuple[Any, list[list[str]]]:
    calls, patches = _service_env(**env)
    for p in patches:
        p.start()
    try:
        result = CliRunner().invoke(main, args)
    finally:
        for p in patches:
            p.stop()
    return result, calls


def test_restart_default_is_validated_reload() -> None:
    result, calls = _invoke(["restart"], ha_mode="bgp")
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "/usr/sbin/haproxy" and "-c" in calls[0]
    assert ["systemctl", "reload", "ober-http"] in calls
    assert not any("restart" in c for c in calls)


def test_restart_invalid_config_changes_nothing() -> None:
    result, calls = _invoke(["restart"], ha_mode="bgp", check_rc=1)
    assert result.exit_code == 1
    assert "bad keyword" in result.output
    assert not any(c[0] == "systemctl" for c in calls)


def test_restart_hard_invalid_config_keeps_running_haproxy() -> None:
    result, calls = _invoke(["restart", "--hard"], ha_mode="bgp", check_rc=1)
    assert result.exit_code == 1
    assert not any(c[0] == "systemctl" for c in calls)


def test_failed_reload_never_falls_back_to_restart() -> None:
    result, calls = _invoke(["restart"], ha_mode="bgp", reload_fails=True)
    assert result.exit_code == 1
    assert not any("restart" in c or "stop" in c for c in calls)


def test_restart_keepalived_validates_then_reloads() -> None:
    result, calls = _invoke(["restart"], ha_mode="keepalived")
    assert result.exit_code == 0, result.output
    assert any(c[0] == "/usr/sbin/keepalived" and "-t" in c for c in calls)
    assert ["systemctl", "reload", "ober-ha"] in calls


def test_restart_hard_restarts_http_and_starts_ha_unit() -> None:
    result, calls = _invoke(["restart", "--hard"], ha_mode="bgp")
    assert result.exit_code == 0, result.output
    assert ["systemctl", "restart", "ober-http"] in calls
    assert ["systemctl", "start", "ober-bgp"] in calls


@pytest.mark.parametrize(("mode", "other"), [("bgp", "ober-ha"), ("keepalived", "ober-bgp")])
def test_start_disables_other_mode_unit_before_http(mode: str, other: str) -> None:
    result, calls = _invoke(["start"], ha_mode=mode)
    assert result.exit_code == 0, result.output
    disable = ["systemctl", "disable", "--now", other]
    assert calls.index(disable) < calls.index(["systemctl", "start", "ober-http"])


# --- upgrade ---------------------------------------------------------------------------------


def _pipx_venv(tmp: Path) -> OberConfig:
    """A config whose venv looks like pipx: bin/python but no bin/pip."""
    config = OberConfig(install_path=tmp)
    config._venv_path_override = tmp / "venv"
    (tmp / "venv" / "bin").mkdir(parents=True)
    (tmp / "venv" / "bin" / "python").touch()
    return config


def test_exabgp_check_works_without_pip_and_stays_in_pinned_major(temp_dir: Path) -> None:
    config = _pipx_venv(temp_dir)
    out = MagicMock(
        returncode=0, stdout="exabgp (5.0.15)\nAvailable versions: 5.0.15, 4.2.25, 4.2.21\n"
    )
    with (
        patch.object(up, "get_exabgp_version", return_value="4.2.21"),
        patch("subprocess.run", return_value=out) as run,
    ):
        result = up._check_exabgp_update(config)
    assert run.call_args.args[0][:3] == [str(temp_dir / "venv/bin/python"), "-m", "pip"]
    assert result == {"current": "4.2.21", "available": "4.2.25", "update_available": True}


def test_exabgp_upgrade_uses_python_m_pip_and_pin(temp_dir: Path) -> None:
    config = _pipx_venv(temp_dir)
    with patch.object(up, "run_command") as run:
        up._upgrade_exabgp(config)
    python = str(temp_dir / "venv/bin/python")
    run.assert_called_once_with([python, "-m", "pip", "install", "--upgrade", bs.EXABGP_SPEC])


def test_upgrade_rerenders_units_and_reenables_ha_unit(temp_dir: Path) -> None:
    config = OberConfig(install_path=temp_dir)
    with (
        patch.object(up, "_create_systemd_services", return_value=["ober-http.service"]) as cs,
        patch.object(up, "ServiceInfo") as svc,
        patch.object(up, "run_command") as run,
    ):
        svc.from_service_name.return_value.is_enabled = True
        up._rerender_units(config)
    cs.assert_called_once_with(config, config.venv_path)
    run.assert_called_once_with(["systemctl", "enable", "ober-bgp"], check=False)


def test_exabgp_check_offers_install_when_missing(temp_dir: Path) -> None:
    config = _pipx_venv(temp_dir)
    out = MagicMock(returncode=0, stdout="Available versions: 5.0.15, 4.2.25\n")
    with (
        patch.object(up, "get_exabgp_version", return_value=None),
        patch("subprocess.run", return_value=out),
    ):
        result = up._check_exabgp_update(config)
    assert result["update_available"] is True
