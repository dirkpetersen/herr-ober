"""Tests for the config wizard (prefilled prompts) and the non-interactive config paths."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands import config as cmd
from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    KeepalivedConfig,
    OberConfig,
    SecurityConfig,
    VIPConfig,
)


def _enter(question: Any) -> Any:
    """Answer one question like a user who presses Enter, and check the default is accepted."""
    answer = question.default
    if question.__class__.__name__ == "List":
        values = [getattr(c, "value", c) for c in question.choices]
        assert answer in values, f"{question.name}: default {answer!r} is not a choice"
    if question.__class__.__name__ == "Text":
        question.validate(answer)  # raises ValidationError when Enter would be rejected
    return answer


def _fake_text(message: str = "", default: str = "", validate: Any = None, **_: Any) -> str:
    assert validate is None or validate(None, default), f"{message}: default rejected"
    return default


def _press_enter_everywhere() -> Any:
    return patch.multiple(
        cmd.inquirer,
        prompt=lambda qs: {q.name: _enter(q) for q in qs},
        text=_fake_text,
        confirm=lambda _message, default=False, **_: default,
    )


def _rich_config(install_path: Path, ha_mode: str) -> OberConfig:
    config = OberConfig(install_path=install_path, ha_mode=ha_mode, local_ip="10.0.0.11")
    config.bgp = BGPConfig(neighbors=["10.0.0.1", "10.0.0.2"], router_id="", hold_time=9)
    config.keepalived = KeepalivedConfig(peers=["10.0.0.12", "10.0.0.13"], interface="eth1")
    config.vips = [
        VIPConfig(address="10.0.100.1"),  # no /32: the spelling must survive
        VIPConfig(address="10.0.100.2/32", exposure="internet"),
    ]
    config.security = SecurityConfig(
        s3_hostnames=["s3.example.org"],
        allowlist=["192.0.2.0/24"],
        trusted_networks=["198.51.100.0/24"],
        log_target="10.0.0.50:5514",
        autoban_ban_s=7200,
    )
    config.backends = [
        BackendConfig(
            name="rgw_prod",
            servers=["10.1.0.1:7480", "10.1.0.2:7480"],
            health_check_path="/swift/healthcheck",
            health_check_interval=2000,
        ),
        BackendConfig(name="rgw_dr", servers=["10.2.0.1:7480"]),
    ]
    config.certs = CertConfig(path="/etc/ssl/s3.pem", acme_email="ops@example.org")
    config.stats_port = 9404
    config.log_retention_days = 30
    return config


@pytest.mark.parametrize("ha_mode", ["bgp", "keepalived"])
@pytest.mark.parametrize("keep_backends", [True, False])
def test_enter_everywhere_round_trips_yaml(
    tmp_path: Path, ha_mode: str, keep_backends: bool
) -> None:
    """4.3: pressing Enter at every prompt leaves ober.yaml byte for byte unchanged."""
    _rich_config(tmp_path, ha_mode).save()
    before = (tmp_path / "etc" / "ober.yaml").read_text()

    config = OberConfig.load(tmp_path / "etc" / "ober.yaml")
    system = MagicMock()
    system.get_local_ip.return_value = "10.0.0.99"
    real_confirm = cmd.inquirer.confirm
    with _press_enter_everywhere():
        if not keep_backends:  # walk through every backend prompt instead of keeping them
            cmd.inquirer.confirm = lambda message, default=False, **_: (
                False if message.startswith("Keep the current backends") else default
            )
        cmd._run_wizard(config, system)
    assert cmd.inquirer.confirm is real_confirm
    config.save()

    assert (tmp_path / "etc" / "ober.yaml").read_text() == before


def test_wizard_hides_route53() -> None:
    """1.4: the Let's Encrypt via Route53 option is not offered."""
    seen: list[Any] = []

    def prompt(questions: list[Any]) -> dict[str, Any]:
        seen.extend(questions)
        return {q.name: q.default for q in questions}

    with patch.object(cmd.inquirer, "prompt", prompt):
        cmd._configure_certs(CertConfig(route53_enabled=True), Path("/x"))
    values = [getattr(c, "value", c) for c in seen[0].choices]
    assert "route53" not in values


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "etc" / "ober.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_dry_run_without_tty_previews_yaml(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.2: CliRunner has no TTY; --dry-run must not touch inquirer."""
    _rich_config(tmp_path, "bgp").save()
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    with (
        patch.object(cmd.inquirer, "prompt", side_effect=AssertionError("prompted")),
        patch("ober.commands.config._apply_configuration") as apply,
    ):
        result = cli_runner.invoke(main, ["config", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "rgw_prod" in result.output
    apply.assert_not_called()


def test_no_tty_without_flag_fails(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rich_config(tmp_path, "bgp").save()
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    with patch("ober.cli.SystemInfo") as system:
        system.return_value.is_root = True
        result = cli_runner.invoke(main, ["config"])
    assert result.exit_code == 1
    assert "--non-interactive" in result.output


def test_non_interactive_applies_existing_yaml(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rich_config(tmp_path, "keepalived").save()
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    with (
        patch("ober.cli.SystemInfo") as system,
        patch("ober.commands.config._apply_configuration") as apply,
    ):
        system.return_value.is_root = True
        result = cli_runner.invoke(main, ["config", "--non-interactive"])
    assert result.exit_code == 0, result.output
    applied = apply.call_args.args[0]
    assert applied.backends[0].name == "rgw_prod"
    assert applied.local_ip == "10.0.0.11"


def test_non_interactive_needs_yaml(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    with patch("ober.cli.SystemInfo") as system:
        system.return_value.is_root = True
        result = cli_runner.invoke(main, ["config", "--non-interactive"])
    assert result.exit_code == 1
    assert "not found" in " ".join(result.output.split())  # rich wraps long paths


def test_invalid_yaml_blocks_config_and_start_but_not_stop_status_logs(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.6: config/start/test refuse an invalid file; stop/status/logs still run."""
    _write(tmp_path, "ha_mode: bgpp\nbackends: [{name: r, servers: '10.1.0.1:7480'}]\n")
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    with (
        patch("ober.cli.SystemInfo") as system,
        patch("ober.commands.config._apply_configuration") as apply,
        patch("ober.commands.service.run_command") as run,
    ):
        system.return_value.is_root = True
        for args in (["config", "--non-interactive"], ["start"], ["test"]):
            result = cli_runner.invoke(main, args)
            assert result.exit_code == 1, args
            assert "ha_mode" in result.output, args
            assert "backends[0].servers" in result.output, args
        apply.assert_not_called()
        run.assert_not_called()


@pytest.mark.parametrize("args", [["stop"], ["status"], ["logs", "-n", "1"]])
def test_syntax_error_does_not_break_incident_commands(
    cli_runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _write(tmp_path, "ha_mode: bgp\n  bad: [\n")
    monkeypatch.setenv("OBER_INSTALL_PATH", str(tmp_path))
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("ober.cli.SystemInfo") as system,
        patch("ober.commands.service.run_command", return_value=fake),
        patch("ober.commands.service.time.sleep"),
        patch("ober.commands.logs.subprocess.run", return_value=fake, create=True),
        patch("ober.system.subprocess.run", return_value=fake),
    ):
        system.return_value.is_root = True
        result = cli_runner.invoke(main, args)
    assert not isinstance(result.exception, Exception) or isinstance(
        result.exception, SystemExit
    ), result.output


def test_local_ip_override_reaches_generators(tmp_path: Path) -> None:
    """3.8: local_ip sets unicast_src_ip and the empty BGP router-id/local-address."""
    config = _rich_config(tmp_path, "keepalived")
    config.ensure_directories()
    with patch("ober.system.SystemInfo.get_local_ip", return_value="192.0.2.99"):
        cmd._generate_keepalived_config(config)
        config.bgp.local_address = "10.0.0.77"
        cmd._generate_exabgp_config(config)
    assert "unicast_src_ip 10.0.0.11" in config.keepalived_config_path.read_text()
    exabgp = config.bgp_config_path.read_text()
    assert "router-id 10.0.0.11;" in exabgp
    assert "local-address 10.0.0.77;" in exabgp
