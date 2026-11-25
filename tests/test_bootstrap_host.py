"""Tests for the opt-in host firewall (8.1) and the conntrack sysctls (8.2) of bootstrap."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from ober.commands import bootstrap as bs


def _runner(
    fail: str = "", sshd: str = "port 22\nport 2222\n", active: int = 0
) -> tuple[list[list[str]], Callable[..., MagicMock]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], check: bool = True) -> MagicMock:  # noqa: ARG001
        calls.append(cmd)
        if cmd[:2] == ["sshd", "-T"]:
            return MagicMock(returncode=0, stdout=sshd, stderr="")
        if cmd[:2] == ["systemctl", "is-active"]:
            return MagicMock(returncode=active, stdout="", stderr="")
        code = 1 if fail and fail in " ".join(cmd) else 0
        return MagicMock(returncode=code, stdout="", stderr="boom")

    return calls, run


def _apply(tools: set[str], **kw: object) -> list[list[str]]:
    calls, run = _runner(**kw)  # type: ignore[arg-type]
    with (
        patch.object(bs, "run_command", run),
        patch.object(bs, "check_command_exists", lambda name: name in tools),
    ):
        bs._open_firewall_ports()
    return [c for c in calls if c[0] != "sshd"]


def test_ufw_allows_ssh_first_and_enables_last() -> None:
    calls = _apply({"ufw"})
    assert calls[:2] == [["ufw", "allow", "22/tcp"], ["ufw", "allow", "2222/tcp"]]
    assert ["ufw", "allow", "proto", "vrrp", "from", "any", "to", "any"] in calls
    assert calls[-1] == ["ufw", "--force", "enable"]
    assert not any("80/tcp" in " ".join(c) for c in calls)
    assert not any(c[0] == "iptables" for c in calls)


def test_ufw_failed_rule_never_enables() -> None:
    calls, run = _runner(fail="2222/tcp")
    with (
        patch.object(bs, "run_command", run),
        patch.object(bs, "check_command_exists", lambda name: name == "ufw"),
        pytest.raises(click.ClickException),
    ):
        bs._open_firewall_ports()
    assert ["ufw", "--force", "enable"] not in calls


def test_firewalld_not_running_uses_offline_cmd() -> None:
    calls = _apply({"firewall-cmd"}, active=3)
    assert calls[1] == ["firewall-offline-cmd", "--add-port=22/tcp"]
    assert ["firewall-offline-cmd", "--add-protocol=vrrp"] in calls
    assert ["firewall-offline-cmd", "--add-service=https"] in calls
    assert calls[-1] == ["systemctl", "enable", "--now", "firewalld"]


def test_firewalld_running_is_permanent_and_reloads() -> None:
    calls = _apply({"firewall-cmd"}, active=0, sshd="")
    assert calls[1] == ["firewall-cmd", "--permanent", "--add-port=22/tcp"]
    assert calls[-1] == ["firewall-cmd", "--reload"]


def test_no_firewall_tool_changes_nothing() -> None:
    assert _apply(set()) == []


@pytest.mark.parametrize("flag", [False, True])
def test_bootstrap_firewall_is_opt_in(temp_dir: Path, flag: bool) -> None:
    obj = MagicMock(system=MagicMock(is_supported=True, is_root=True))
    args = [str(temp_dir / "ober"), "-y"] + (["--firewall"] if flag else [])
    with (
        patch.object(bs, "_apply_kernel_tuning"),
        patch.object(bs, "_install_packages"),
        patch.object(bs, "_setup_venv"),
        patch.object(bs, "_install_exabgp"),
        patch.object(bs, "_create_systemd_services"),
        patch.object(bs, "_create_config_templates"),
        patch.object(bs, "_configure_watchdog"),
        patch.object(bs, "_open_firewall_ports") as firewall,
        patch.object(bs, "_get_current_venv_path", return_value=None),
    ):
        result = CliRunner().invoke(bs.bootstrap, args, obj=obj)
    assert result.exit_code == 0, result.output
    assert firewall.called is flag


def test_conntrack_sysctls() -> None:
    assert "net.netfilter.nf_conntrack_max = 1048576" in bs.KERNEL_TUNING
    assert "nf_conntrack_tcp_timeout_time_wait" in bs.KERNEL_TUNING


def test_rerun_never_overwrites_an_invalid_config(temp_dir: Path) -> None:
    """A YAML error must not turn into a file full of defaults on bootstrap re-run."""
    from ober.config import OberConfig

    config = OberConfig(install_path=temp_dir)
    config.ensure_directories()
    config.config_path.write_text("ha_mode: keepalived\n  broken: [\n")
    system = MagicMock()
    system.get_local_ip.return_value = "10.1.1.1"
    bs._create_config_templates(config, system, temp_dir / "venv")
    assert config.config_path.read_text() == "ha_mode: keepalived\n  broken: [\n"
