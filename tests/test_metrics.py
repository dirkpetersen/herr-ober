"""Tests for ober metrics (Prometheus text from the status dict)."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from ober.cli import main
from ober.commands.metrics import render


def _status(ha_mode: str = "keepalived") -> dict[str, Any]:
    vip: dict[str, Any] = {"address": "10.1.0.1", "present": True}
    vip.update({"announced": True} if ha_mode == "bgp" else {"vrrp_state": "MASTER"})
    return {
        "ha_mode": ha_mode,
        "services": {"ober-http": {"active": True}, "ober-ha": {"active": False}},
        "haproxy": {"version": '3.3.1 "x"'},
        "ober": {"version": "0.5.0"},
        "keepalived": {"version": "2.3.4"},
        "config_hash": "abc123",
        "maintenance": False,
        "vips": [vip],
        "backends": {"s3_backend": {"up": 1, "total": 2, "down": []}},
        "certificate": {"days_left": 12},
        "check": {"level": "WARN", "problems": ["x"]},
    }


def test_render_keepalived() -> None:
    text = render(_status())
    for line in (
        "ober_check_level 1",
        'ober_service_active{unit="ober-ha"} 0',
        'ober_vrrp_master{vip="10.1.0.1"} 1',
        'ober_vip_present{vip="10.1.0.1"} 1',
        'ober_backend_servers_up{backend="s3_backend"} 1',
        'ober_backend_servers{backend="s3_backend"} 2',
        "ober_cert_remaining_seconds 1036800",
        'ober_config_info{hash="abc123"} 1',
        'ober_version_info{ober="0.5.0",haproxy="3.3.1 \\"x\\"",exabgp="",keepalived="2.3.4"} 1',
    ):
        assert line in text.splitlines(), line
    assert "ober_vip_announced" not in text


def test_render_bgp_and_unknowns() -> None:
    status = _status("bgp")
    status.update(backends=None, certificate=None)
    status["vips"][0]["present"] = None  # ip failed: no sample rather than a wrong 0
    text = render(status)
    assert 'ober_vip_announced{vip="10.1.0.1"} 1' in text
    assert "ober_vip_present{" not in text and "ober_cert_remaining" not in text
    assert "ober_vrrp_master" not in text


def test_output_file(temp_dir: Path) -> None:
    out = temp_dir / "textfile" / "ober.prom"
    with (
        patch("ober.commands.metrics.collect_status", return_value=_status()),
        patch("ober.commands.metrics.load_config_safe", return_value=(None, None)),
    ):
        result = CliRunner().invoke(main, ["metrics", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "ober_check_level 1" in out.read_text()
