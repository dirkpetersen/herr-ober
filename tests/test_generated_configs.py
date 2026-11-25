"""Tests for generated configuration files."""

import re
import sys

import pytest

from ober.commands.config import (
    _configure_vip_interface,
    _generate_exabgp_config,
    _generate_haproxy_config,
    _netplan_vip_yaml,
)
from ober.config import BackendConfig, CertConfig, OberConfig, VIPConfig


def _refs_and_sections(text: str) -> tuple[set[str], set[str]]:
    refs = set(re.findall(r"^\s*default_backend\s+(\S+)", text, re.MULTILINE))
    sections = set(re.findall(r"^backend\s+(\S+)", text, re.MULTILINE))
    return refs, sections


def test_default_backend_exists(sample_config: OberConfig) -> None:
    _generate_haproxy_config(sample_config)
    text = sample_config.haproxy_config_path.read_text()
    refs, sections = _refs_and_sections(text)
    assert refs == {"s3_backend"}
    assert refs <= sections


def test_several_backend_groups_rejected(sample_config: OberConfig) -> None:
    sample_config.backends = [
        BackendConfig(name="s3_backend_1", servers=["rgw1:7480"]),
        BackendConfig(name="s3_backend_2", servers=["rgw2:7480"]),
    ]
    with pytest.raises(ValueError, match="one backend group"):
        _generate_haproxy_config(sample_config)


@pytest.mark.parametrize("field", ["vips", "backends", "certs"])
def test_no_s3_frontend_is_an_error(sample_config: OberConfig, field: str) -> None:
    if field == "certs":
        sample_config.certs = CertConfig()
    else:
        setattr(sample_config, field, [])
    with pytest.raises(ValueError, match="No S3 frontend"):
        _generate_haproxy_config(sample_config)
    assert not sample_config.haproxy_config_path.exists()


def test_multi_vip_bind_uses_commas(sample_config: OberConfig) -> None:
    _generate_haproxy_config(sample_config, https_port=8443)
    text = sample_config.haproxy_config_path.read_text()
    assert "bind 10.0.100.1:8443,10.0.100.2:8443 ssl crt" in text


def test_health_fails_without_backend_servers(sample_config: OberConfig) -> None:
    _generate_haproxy_config(sample_config)
    assert "monitor fail if { nbsrv(s3_backend) lt 1 }" in (
        sample_config.haproxy_config_path.read_text()
    )


def test_stats_bind_defaults_to_localhost(sample_config: OberConfig) -> None:
    _generate_haproxy_config(sample_config)
    text = sample_config.haproxy_config_path.read_text()
    assert "bind 127.0.0.1:8404\n" in text
    assert "*:8404" not in text


def test_stats_bind_extra_addresses(sample_config: OberConfig) -> None:
    sample_config.stats_bind = ["10.0.5.1", "127.0.0.1"]
    _generate_haproxy_config(sample_config)
    assert "bind 127.0.0.1:8404,10.0.5.1:8404\n" in sample_config.haproxy_config_path.read_text()


def test_stats_bind_rejects_garbage(sample_config: OberConfig) -> None:
    sample_config.stats_bind = ["10.0.5.1:80\nglobal"]
    with pytest.raises(ValueError, match="stats_bind"):
        _generate_haproxy_config(sample_config)


def test_exabgp_has_no_bfd_block(sample_config: OberConfig) -> None:
    sample_config.bgp.bfd_enabled = True
    _generate_exabgp_config(sample_config)
    assert "bfd" not in sample_config.bgp_config_path.read_text()


def test_netplan_uses_dummy_devices(sample_config: OberConfig) -> None:
    import yaml

    data = yaml.safe_load(_netplan_vip_yaml(sample_config))
    assert data == {
        "network": {
            "version": 2,
            "dummy-devices": {"lo-vip": {"addresses": ["10.0.100.1/32", "10.0.100.2/32"]}},
        }
    }


def test_exabgp_health_uses_running_interpreter(sample_config: OberConfig) -> None:
    sample_config.bgp.neighbors = ["10.0.0.1"]
    _generate_exabgp_config(sample_config)
    text = sample_config.bgp_config_path.read_text()
    assert f"run {sys.executable} -m ober.commands.health;" in text
    assert str(sample_config.venv_path / "bin" / "python") not in text


def _vip_iface(
    config: OberConfig,
    family: str,
    show_rc: int = 0,
    fail: bool = False,
    tools: tuple[str, ...] = ("netplan", "nmcli", "networkctl"),
    active_rc: int = 0,
) -> list:
    """Run _configure_vip_interface with mocks and return the commands it ran."""
    import subprocess
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from ober.system import OSFamily

    calls: list[list[str]] = []

    def run(cmd: list[str], check: bool = True, **_: object) -> MagicMock:
        calls.append(cmd)
        if cmd[:3] == ["nmcli", "connection", "show"]:
            rc = show_rc
        elif cmd[:2] == ["systemctl", "is-active"]:
            rc = active_rc
        else:
            rc = int(fail)
        if rc and check:
            raise subprocess.CalledProcessError(rc, cmd, stderr="boom")
        return MagicMock(returncode=rc)

    system = MagicMock(os_family=OSFamily[family])
    with (
        patch("ober.system.SystemInfo", return_value=system),
        patch("ober.system.check_command_exists", side_effect=lambda t: t in tools),
        patch("ober.system.run_command", side_effect=run),
        patch("ober.commands.uninstall.run_command", side_effect=run),
        patch("ober.commands.uninstall.check_command_exists", side_effect=lambda t: t in tools),
    ):
        _configure_vip_interface(
            config,
            netplan_path=Path(config.install_path) / "60-vip.yaml",
            networkd_dir=Path(config.install_path) / "network",
        )
    return calls


def test_nmcli_sets_full_address_list(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "RHEL", show_rc=0)
    assert not any(c[:3] == ["nmcli", "connection", "add"] for c in calls)
    assert [
        "nmcli", "connection", "modify", "lo-vip", "ipv4.method", "manual",
        "ipv4.addresses", "10.0.100.1/32,10.0.100.2/32",
    ] in calls  # fmt: skip


def test_nmcli_creates_missing_connection(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "RHEL", show_rc=10)
    assert any(c[:3] == ["nmcli", "connection", "add"] for c in calls)


def test_vip_interface_failure_is_reported(sample_config: OberConfig) -> None:
    with pytest.raises(RuntimeError, match="netplan apply: boom"):
        _vip_iface(sample_config, "DEBIAN", fail=True)


def test_networkd_without_nmcli_running(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "RHEL", tools=("networkctl",), active_rc=0)
    assert ["systemctl", "enable", "--now", "systemd-networkd"] in calls
    assert calls[-1] == ["networkctl", "reload"]
    assert not any(c[0] == "nmcli" for c in calls)
    net = sample_config.install_path / "network"
    assert (net / "60-lo-vip.netdev").read_text() == "[NetDev]\nName=lo-vip\nKind=dummy\n"
    network = (net / "60-lo-vip.network").read_text()
    assert "[Match]\nName=lo-vip\n" in network
    assert "Address=10.0.100.1/32\nAddress=10.0.100.2/32\n" in network
    # Run again: same files, no error (idempotent)
    _vip_iface(sample_config, "RHEL", tools=("networkctl",))
    assert (net / "60-lo-vip.network").read_text() == network


def test_networkd_not_running_is_started_without_reload(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "RHEL", tools=("networkctl",), active_rc=3)
    assert calls[-1] == ["systemctl", "enable", "--now", "systemd-networkd"]


def test_networkd_failure_is_reported(sample_config: OberConfig) -> None:
    with pytest.raises(RuntimeError, match="systemctl enable --now systemd-networkd: boom"):
        _vip_iface(sample_config, "RHEL", tools=("networkctl",), fail=True)


def test_no_vip_tool_is_reported(sample_config: OberConfig) -> None:
    with pytest.raises(RuntimeError, match="nmcli or networkctl not found"):
        _vip_iface(sample_config, "RHEL", tools=())


def test_netplan_written_and_applied(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "DEBIAN")
    assert calls == [["netplan", "apply"]]
    assert "dummy-devices" in (sample_config.install_path / "60-vip.yaml").read_text()


def test_stats_bind_roundtrip(sample_config: OberConfig) -> None:
    sample_config.stats_bind = ["10.0.5.1"]
    sample_config.save()
    assert OberConfig.load(sample_config.config_path).stats_bind == ["10.0.5.1"]


def test_config_command_exits_1_on_render_error(
    sample_config: OberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import patch

    from click.testing import CliRunner

    from ober.cli import main

    sample_config.save()
    monkeypatch.setenv("OBER_INSTALL_PATH", str(sample_config.install_path))
    with (
        patch("ober.cli.SystemInfo") as system,
        patch(
            "ober.commands.config._apply_configuration",
            side_effect=ValueError("No S3 frontend can be generated: missing backends"),
        ),
    ):
        system.return_value.is_root = True
        result = CliRunner().invoke(main, ["config", "--non-interactive"])
    assert result.exit_code == 1
    assert "No S3 frontend" in result.output
    assert "Configuration applied" not in result.output


def test_bare_vip_is_rendered_as_slash_32(sample_config: OberConfig) -> None:
    import yaml

    from ober.commands.config import _networkd_vip_files

    sample_config.vips = [VIPConfig(address="10.0.100.1")]
    data = yaml.safe_load(_netplan_vip_yaml(sample_config))
    assert data["network"]["dummy-devices"]["lo-vip"]["addresses"] == ["10.0.100.1/32"]
    assert "Address=10.0.100.1/32\n" in _networkd_vip_files(sample_config)["60-lo-vip.network"]
    calls = _vip_iface(sample_config, "RHEL")
    assert "10.0.100.1/32" in next(c for c in calls if c[2:3] == ["modify"])


def test_keepalived_mode_removes_lo_vip(sample_config: OberConfig) -> None:
    """A lo-vip left from BGP mode would keep every VIP on every node."""
    sample_config.ha_mode = "keepalived"
    netplan = sample_config.install_path / "60-vip.yaml"
    net = sample_config.install_path / "network"
    net.mkdir()
    netplan.write_text("x")
    (net / "60-lo-vip.netdev").write_text("x")
    calls = _vip_iface(sample_config, "DEBIAN")
    assert not netplan.exists() and not list(net.iterdir())
    assert ["netplan", "apply"] in calls and ["networkctl", "delete", "lo-vip"] in calls
    # Nothing left: no command runs (idempotent)
    assert _vip_iface(sample_config, "DEBIAN") == []


def test_no_vips_removes_nmcli_lo_vip(sample_config: OberConfig) -> None:
    sample_config.vips = []
    calls = _vip_iface(sample_config, "RHEL")
    assert calls == [["nmcli", "connection", "delete", "lo-vip"]]


def test_stats_bind_wildcard_rejected_with_internet_vip(sample_config: OberConfig) -> None:
    sample_config.stats_bind = ["0.0.0.0"]
    _generate_haproxy_config(sample_config)  # internal VIPs only: allowed
    assert sample_config.validate()[0] == []
    sample_config.vips[0].exposure = "internet"
    with pytest.raises(ValueError, match="stats_bind must not be 0.0.0.0"):
        _generate_haproxy_config(sample_config)
    assert any(
        e.startswith("stats_bind[0]: must not be 0.0.0.0") for e in sample_config.validate()[0]
    )
