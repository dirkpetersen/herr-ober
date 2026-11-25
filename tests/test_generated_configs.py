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
from ober.config import BackendConfig, CertConfig, OberConfig


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


def _vip_iface(config: OberConfig, family: str, show_rc: int = 0, fail: bool = False) -> list:
    """Run _configure_vip_interface with mocks and return the commands it ran."""
    import subprocess
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from ober.system import OSFamily

    calls: list[list[str]] = []

    def run(cmd: list[str], check: bool = True, **_: object) -> MagicMock:
        calls.append(cmd)
        rc = show_rc if cmd[:3] == ["nmcli", "connection", "show"] else int(fail)
        if rc and check:
            raise subprocess.CalledProcessError(rc, cmd, stderr="boom")
        return MagicMock(returncode=rc)

    system = MagicMock(os_family=OSFamily[family])
    with (
        patch("ober.system.SystemInfo", return_value=system),
        patch("ober.system.check_command_exists", return_value=True),
        patch("ober.system.run_command", side_effect=run),
    ):
        _configure_vip_interface(config, netplan_path=Path(config.install_path) / "60-vip.yaml")
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


def test_netplan_written_and_applied(sample_config: OberConfig) -> None:
    calls = _vip_iface(sample_config, "DEBIAN")
    assert calls == [["netplan", "apply"]]
    assert "dummy-devices" in (sample_config.install_path / "60-vip.yaml").read_text()


def test_stats_bind_roundtrip(sample_config: OberConfig) -> None:
    sample_config.stats_bind = ["10.0.5.1"]
    sample_config.save()
    assert OberConfig.load(sample_config.config_path).stats_bind == ["10.0.5.1"]
