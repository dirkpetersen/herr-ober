"""Tests for ober backend (stable names, tuning, drain/maint/ready over the admin socket)."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ober.cli import main
from ober.commands.backend import save_server_state
from ober.config import BackendConfig, OberConfig, server_name

STAT = (
    "# pxname,svname,qcur,scur,status,weight,check_status\n"
    "s3,FRONTEND,0,1,OPEN,,\n"
    "s3_backend,rgw1_7480,0,4,UP,1,L7OK\n"
    "s3_backend,rgw2_7480,0,0,DRAIN,1,L7OK\n"
)


def _config(temp_dir: Path, **kw: Any) -> OberConfig:
    config = OberConfig(install_path=temp_dir)
    config.backends = [BackendConfig(name="s3_backend", servers=["rgw1:7480", "rgw2:7480"], **kw)]
    return config


def _run(temp_dir: Path, args: list[str], runtime: MagicMock, root: bool = True) -> Any:
    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=root)),
        patch("ober.commands.backend.OberConfig.load", return_value=_config(temp_dir)),
        patch("ober.commands.backend.HAProxyRuntime", return_value=runtime),
    ):
        return CliRunner().invoke(main, ["backend", *args])


def test_server_name_is_stable() -> None:
    assert server_name("rgw1:7480") == "rgw1_7480"
    assert server_name("10.0.0.5:7480") == "10.0.0.5_7480"
    assert server_name("[fd00::5]:7480") == "fd00_5_7480"


def test_duplicate_server_names_are_invalid(temp_dir: Path) -> None:
    config = _config(temp_dir)
    config.backends[0].servers = ["rgw1:7480", "rgw1:7480"]
    errors, _ = config.validate()
    assert any("duplicates" in e for e in errors)


def test_tuning_fields_validated(temp_dir: Path) -> None:
    errors, _ = _config(temp_dir, fall=0, maxconn=-1).validate()
    assert any("fall" in e for e in errors) and any("maxconn" in e for e in errors)


def test_render_uses_names_and_tuning(temp_dir: Path) -> None:
    from ober.commands.config import _generate_haproxy_config
    from ober.config import CertConfig, VIPConfig

    config = _config(temp_dir, maxconn=500, slowstart_s=0)
    config.vips = [VIPConfig(address="10.0.0.100")]
    config.certs = CertConfig(path="/etc/ssl/s3.pem")
    _generate_haproxy_config(config)
    text = config.haproxy_config_path.read_text()
    assert "default-server check inter 1000ms fall 3 rise 2 maxconn 500\n" in text
    assert "    server rgw2_7480 rgw2:7480\n" in text
    assert f"server-state-file {config.server_state_path}" in text


def test_drain_by_address(temp_dir: Path) -> None:
    runtime = MagicMock()
    runtime.send.side_effect = lambda cmd: "1\n# hdr\n" if cmd == "show servers state" else ""
    result = _run(temp_dir, ["drain", "rgw2:7480"], runtime)
    assert result.exit_code == 0, result.output
    runtime.send.assert_any_call("set server s3_backend/rgw2_7480 state drain")
    assert (temp_dir / "var" / "run" / "haproxy-server-state").read_text() == "1\n# hdr\n"


def test_unknown_server_and_refusal(temp_dir: Path) -> None:
    runtime = MagicMock()
    assert _run(temp_dir, ["maint", "rgw9:7480"], runtime).exit_code == 1
    runtime.send.assert_not_called()
    runtime.send.return_value = "No such server."
    assert _run(temp_dir, ["ready", "rgw1_7480"], runtime).exit_code == 1


def test_needs_root(temp_dir: Path) -> None:
    assert _run(temp_dir, ["list"], MagicMock(), root=False).exit_code == 1


def test_list_json(temp_dir: Path) -> None:
    runtime = MagicMock()
    runtime.send.return_value = STAT
    result = _run(temp_dir, ["list", "--json"], runtime)
    assert result.exit_code == 0, result.output
    assert '"svname": "rgw2_7480"' in result.output and '"DRAIN"' in result.output
    assert "FRONTEND" not in result.output


def test_save_state_skips_when_haproxy_down(temp_dir: Path) -> None:
    config = _config(temp_dir)
    with patch("ober.commands.backend.HAProxyRuntime") as rt:
        rt.return_value.send.side_effect = OSError("no socket")
        assert save_server_state(config) is False
    assert not config.server_state_path.exists()
