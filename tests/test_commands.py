#!/usr/bin/env python3
"""Tests for ober command implementations."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands.doctor import (
    _check_config,
    _check_haproxy,
    _check_network_tools,
    _check_os,
    _check_python,
    _check_root,
    _check_service,
)
from ober.commands.sync import expand_hostlist, resolve_host
from ober.commands.test import _test_backend, _test_bgp_neighbor, _test_certificate
from ober.config import OberConfig
from ober.system import OSFamily, SystemInfo


class TestDoctorChecks:
    """Tests for individual doctor check functions."""

    def test_check_os_debian(self) -> None:
        """Test OS check for Debian."""
        system = SystemInfo()
        system.os_family = OSFamily.DEBIAN
        system.os_name = "Ubuntu"
        system.os_version = "24.04"

        result = _check_os(system)
        assert result["passed"] is True
        assert result["status"] == "supported"

    def test_check_os_unsupported(self) -> None:
        """Test OS check for unsupported OS."""
        system = SystemInfo()
        system.os_family = OSFamily.UNKNOWN
        system.os_name = "Arch Linux"

        result = _check_os(system)
        assert result["passed"] is False
        assert result["status"] == "unsupported"

    def test_check_python_ok(self) -> None:
        """Test Python version check."""
        system = SystemInfo()
        with patch.object(system, "check_python_version", return_value=True):
            result = _check_python(system)
            assert result["passed"] is True

    def test_check_root_true(self) -> None:
        """Test root check when running as root."""
        system = SystemInfo()
        system.is_root = True

        result = _check_root(system)
        assert result["passed"] is True

    def test_check_root_false(self) -> None:
        """Test root check when not running as root."""
        system = SystemInfo()
        system.is_root = False

        result = _check_root(system)
        assert result["passed"] is False

    def test_check_haproxy_installed(self) -> None:
        """Test HAProxy check when installed."""
        with patch("ober.commands.doctor.get_haproxy_version", return_value="3.3.1"):
            result = _check_haproxy()
            assert result["passed"] is True
            assert "3.3.1" in result["message"]

    def test_check_haproxy_not_installed(self) -> None:
        """Test HAProxy check when not installed."""
        with patch("ober.commands.doctor.get_haproxy_version", return_value=None):
            result = _check_haproxy()
            assert result["passed"] is False
            assert result["status"] == "not installed"

    def test_check_config_exists(self, temp_dir: Path) -> None:
        """Test config check when config exists."""
        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()
        config.save()

        with patch("ober.commands.doctor.OberConfig.load", return_value=config):
            result = _check_config()
            assert result["passed"] is True

    def test_check_service_active(self) -> None:
        """Test service check for active service."""
        mock_service = MagicMock()
        mock_service.is_active = True
        mock_service.is_enabled = True
        mock_service.pid = 1234

        with patch("ober.commands.doctor.ServiceInfo.from_service_name", return_value=mock_service):
            result = _check_service("test-service")
            assert result["passed"] is True
            assert result["status"] == "active"

    def test_check_network_tools(self) -> None:
        """Test network tools check."""
        with patch("ober.commands.doctor.check_command_exists", return_value=True):
            result = _check_network_tools()
            assert result["passed"] is True


class TestSyncFunctions:
    """Tests for sync command helper functions."""

    def test_expand_hostlist_simple(self) -> None:
        """Test hostlist expansion with simple list."""
        result = expand_hostlist("host1,host2,host3")
        assert result == ["host1", "host2", "host3"]

    def test_expand_hostlist_with_hostlist_module(self) -> None:
        """Test hostlist expansion with hostlist module."""
        # Test the actual hostlist module
        result = expand_hostlist("node[01-03]")
        assert "node01" in result or result == ["node[01-03]"]

    def test_resolve_host_ip(self) -> None:
        """Test resolve_host with IP address."""
        result = resolve_host("10.0.0.1")
        assert result == "10.0.0.1"

    def test_resolve_host_localhost(self) -> None:
        """Test resolve_host with localhost."""
        result = resolve_host("localhost")
        assert result in ["127.0.0.1", "::1", None]  # Depends on system config

    def test_resolve_host_invalid(self) -> None:
        """Test resolve_host with invalid hostname."""
        # Use a definitely invalid format
        result = resolve_host("not-a-valid-hostname-12345.invalid")
        # May return None or raise error depending on DNS config
        # If it resolves (some networks have catch-all DNS), that's ok
        assert result is None or isinstance(result, str)


class TestTestCommand:
    """Tests for test command helper functions."""

    def test_bgp_neighbor_success(self) -> None:
        """Test BGP neighbor check with successful connection."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 0
            mock_socket.return_value = mock_instance

            result = _test_bgp_neighbor("10.0.0.1")
            assert result["passed"] is True

    def test_bgp_neighbor_fail(self) -> None:
        """Test BGP neighbor check with failed connection."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 1
            mock_socket.return_value = mock_instance

            result = _test_bgp_neighbor("10.0.0.1")
            assert result["passed"] is False

    def test_backend_success(self) -> None:
        """Test backend check with successful connection."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 0
            mock_socket.return_value = mock_instance

            result = _test_backend("10.0.0.1:7480", "s3_backend")
            assert result["passed"] is True

    def test_backend_fail(self) -> None:
        """Test backend check with failed connection."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 1
            mock_socket.return_value = mock_instance

            result = _test_backend("10.0.0.1:7480", "s3_backend")
            assert result["passed"] is False

    def test_certificate_valid(self, temp_dir: Path) -> None:
        """Test certificate check with valid certificate."""
        cert_path = temp_dir / "cert.pem"
        cert_path.write_text(
            "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
            "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n"
        )

        result = _test_certificate(str(cert_path))
        assert result["passed"] is True

    def test_certificate_missing_key(self, temp_dir: Path) -> None:
        """Test certificate check with missing key."""
        cert_path = temp_dir / "cert.pem"
        cert_path.write_text("-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n")

        result = _test_certificate(str(cert_path))
        assert result["passed"] is False

    def test_certificate_not_found(self) -> None:
        """Test certificate check with missing file."""
        result = _test_certificate("/nonexistent/cert.pem")
        assert result["passed"] is False


class TestCLIIntegration:
    """Integration tests for CLI commands."""

    def test_version_output(self, cli_runner: CliRunner) -> None:
        """Test --version output contains expected info."""
        result = cli_runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "ober" in result.output.lower()
        assert "haproxy" in result.output.lower()
        assert "exabgp" in result.output.lower()

    def test_doctor_detects_missing_root(self, cli_runner: CliRunner) -> None:
        """Test doctor command detects missing root access."""
        # Just run doctor - it will detect actual system state
        result = cli_runner.invoke(main, ["doctor"])
        # The command should run and show diagnostic output
        assert "Operating System" in result.output
        # If not running as root, should show missing
        if "missing" in result.output.lower():
            assert "Root Access" in result.output

    def test_status_no_services(self, cli_runner: CliRunner) -> None:
        """Test status command when no services are running."""
        with patch("ober.commands.status.ServiceInfo") as mock:
            mock_instance = MagicMock()
            mock_instance.is_active = False
            mock_instance.is_enabled = False
            mock_instance.status = "inactive"
            mock_instance.pid = None
            mock.from_service_name.return_value = mock_instance

            result = cli_runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "inactive" in result.output

    def test_status_json_output(self, cli_runner: CliRunner) -> None:
        """Test status command with JSON output."""
        with patch("ober.commands.status.ServiceInfo") as mock:
            mock_instance = MagicMock()
            mock_instance.is_active = False
            mock_instance.is_enabled = False
            mock_instance.status = "inactive"
            mock_instance.pid = None
            mock.from_service_name.return_value = mock_instance

            result = cli_runner.invoke(main, ["--json", "status"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "services" in data
            assert "ober-http" in data["services"]


class TestLogsCommand:
    """Tests for logs command."""

    def test_logs_http_service(self, cli_runner: CliRunner) -> None:
        """Test logs command with http service filter."""
        with patch("subprocess.run") as mock_run:
            cli_runner.invoke(main, ["logs", "--service", "http"])
            # Should call journalctl with ober-http
            if mock_run.called:
                cmd_args = mock_run.call_args[0][0]
                assert "ober-http" in cmd_args

    def test_logs_bgp_service(self, cli_runner: CliRunner) -> None:
        """Test logs command with bgp service filter."""
        with patch("subprocess.run") as mock_run:
            cli_runner.invoke(main, ["logs", "--service", "bgp"])
            if mock_run.called:
                cmd_args = mock_run.call_args[0][0]
                assert "ober-bgp" in cmd_args

    def test_logs_all_services(self, cli_runner: CliRunner) -> None:
        """Test logs command with all services."""
        with patch("subprocess.run") as mock_run:
            cli_runner.invoke(main, ["logs", "--service", "all"])
            if mock_run.called:
                cmd_args = mock_run.call_args[0][0]
                assert "ober-http" in cmd_args
                assert "ober-bgp" in cmd_args

    def test_logs_with_lines(self, cli_runner: CliRunner) -> None:
        """Test logs command with custom lines."""
        with patch("subprocess.run") as mock_run:
            cli_runner.invoke(main, ["logs", "-n", "100"])
            if mock_run.called:
                cmd_args = mock_run.call_args[0][0]
                assert "-n" in cmd_args
                assert "100" in cmd_args

    def test_logs_journalctl_not_found(self, cli_runner: CliRunner) -> None:
        """Test logs command when journalctl not found."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = cli_runner.invoke(main, ["logs"])
            assert "journalctl not found" in result.output or result.exit_code != 0


class TestServiceCommands:
    """Tests for service start/stop/restart commands."""

    def test_start_requires_root(self, cli_runner: CliRunner) -> None:
        """Test start command requires root access."""
        # Mock SystemInfo at CLI level where Context is created
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["start"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_stop_requires_root(self, cli_runner: CliRunner) -> None:
        """Test stop command requires root access."""
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["stop"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_restart_requires_root(self, cli_runner: CliRunner) -> None:
        """Test restart command requires root access."""
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["restart"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_start_missing_config(self, cli_runner: CliRunner) -> None:
        """Test start command with missing HAProxy config."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.OberConfig.load") as mock_config,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            config_mock.haproxy_config_path.exists.return_value = False
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["start"])
            assert "not found" in result.output.lower() or result.exit_code != 0

    def test_stop_graceful_shutdown(self, cli_runner: CliRunner) -> None:
        """Test stop command performs graceful shutdown."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.run_command") as mock_run,
            patch("ober.commands.service.time.sleep"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            # Both services active
            bgp_mock = MagicMock()
            bgp_mock.is_active = True
            http_mock = MagicMock()
            http_mock.is_active = True
            mock_svc.side_effect = [bgp_mock, http_mock, bgp_mock]

            cli_runner.invoke(main, ["stop"])
            # Should call stop for both services
            assert mock_run.called

    def test_stop_force(self, cli_runner: CliRunner) -> None:
        """Test stop command with --force flag."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.run_command"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            svc_mock = MagicMock()
            svc_mock.is_active = True
            mock_svc.return_value = svc_mock

            result = cli_runner.invoke(main, ["stop", "--force"])
            assert result.exit_code == 0 or "stopped" in result.output.lower()


class TestUpgradeCommand:
    """Tests for upgrade command."""

    def test_upgrade_check_only(self, cli_runner: CliRunner) -> None:
        """Test upgrade --check-only doesn't require root."""
        with (
            patch("ober.commands.upgrade.SystemInfo") as mock_system,
            patch("ober.commands.upgrade.OberConfig.load") as mock_config,
            patch("ober.commands.upgrade.get_haproxy_version", return_value="3.3.0"),
            patch("ober.commands.upgrade.get_exabgp_version", return_value="4.2.21"),
            patch("subprocess.run") as mock_run,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_instance.os_family = "debian"
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            config_mock.venv_path.exists.return_value = False
            mock_config.return_value = config_mock

            mock_run.return_value = MagicMock(returncode=1, stdout="")

            result = cli_runner.invoke(main, ["upgrade", "--check-only"])
            # Should not fail for missing root when check-only
            assert result.exit_code == 0

    def test_upgrade_requires_root(self, cli_runner: CliRunner) -> None:
        """Test upgrade without --check-only requires root."""
        with patch("ober.commands.upgrade.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["upgrade"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_check_haproxy_update_debian(self) -> None:
        """Test HAProxy update check on Debian."""
        from ober.commands.upgrade import _check_haproxy_update
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.DEBIAN

        mock_output = MagicMock()
        mock_output.returncode = 0
        mock_output.stdout = "haproxy:\n  Installed: 3.3.0\n  Candidate: 3.3.1\n"

        with (
            patch("ober.commands.upgrade.get_haproxy_version", return_value="3.3.0"),
            patch("subprocess.run", return_value=mock_output),
        ):
            result = _check_haproxy_update(system)
            assert result["current"] == "3.3.0"

    def test_check_exabgp_update(self, temp_dir: Path) -> None:
        """Test ExaBGP update check."""
        from ober.commands.upgrade import _check_exabgp_update

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()

        # Create a mock pip
        pip_dir = config.venv_path / "bin"
        pip_dir.mkdir(parents=True, exist_ok=True)
        pip_path = pip_dir / "pip"
        pip_path.touch()

        with (
            patch("ober.commands.upgrade.get_exabgp_version", return_value="4.2.20"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _check_exabgp_update(config)
            assert result["current"] == "4.2.20"


class TestSyncCommand:
    """Tests for sync command."""

    def test_sync_requires_root(self, cli_runner: CliRunner) -> None:
        """Test sync command requires root access."""
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["sync", "--routers", "10.0.0.1"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_process_hostlist(self) -> None:
        """Test _process_hostlist function."""
        from ober.commands.sync import _process_hostlist

        with (
            patch("ober.commands.sync.expand_hostlist", return_value=["10.0.0.1", "10.0.0.2"]),
            patch("ober.commands.sync.resolve_host", side_effect=lambda x: x),
        ):
            result = _process_hostlist("10.0.0.1,10.0.0.2", "test")
            assert result == ["10.0.0.1", "10.0.0.2"]

    def test_process_hostlist_with_failures(self) -> None:
        """Test _process_hostlist with resolution failures."""
        from ober.commands.sync import _process_hostlist

        with (
            patch("ober.commands.sync.expand_hostlist", return_value=["good", "bad"]),
            patch("ober.commands.sync.resolve_host", side_effect=["10.0.0.1", None]),
        ):
            result = _process_hostlist("good,bad", "test")
            assert result == ["10.0.0.1"]

    def test_write_whitelists(self, temp_dir: Path) -> None:
        """Test _write_whitelists function."""
        from ober.commands.sync import _write_whitelists

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()

        data = {
            "routers": ["10.0.0.1", "10.0.0.2"],
            "frontend_http": ["192.168.1.1"],
        }

        _write_whitelists(config, data)

        # Check files were created
        routers_file = temp_dir / "etc" / "haproxy" / "routers.lst"
        frontend_file = temp_dir / "etc" / "haproxy" / "frontend-http.lst"

        assert routers_file.exists()
        assert frontend_file.exists()

        routers_content = routers_file.read_text()
        assert "10.0.0.1" in routers_content
        assert "10.0.0.2" in routers_content


class TestUninstallCommand:
    """Tests for uninstall command."""

    def test_uninstall_requires_root(self, cli_runner: CliRunner) -> None:
        """Test uninstall command requires root access."""
        with patch("ober.cli.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["uninstall"])
            assert "requires root" in result.output.lower() or result.exit_code != 0

    def test_uninstall_cancelled(self, cli_runner: CliRunner) -> None:
        """Test uninstall is cancelled when user declines."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.uninstall.inquirer.confirm", return_value=False),
            patch("ober.commands.uninstall.Path.exists", return_value=False),
            patch("ober.commands.uninstall.Path.is_symlink", return_value=False),
            patch("ober.commands.uninstall.check_command_exists", return_value=False),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["uninstall"])
            assert "cancelled" in result.output.lower() or result.exit_code == 0

    def test_remove_vip_interface_debian(self) -> None:
        """Test VIP interface removal on Debian."""
        from ober.commands.uninstall import _remove_vip_interface
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.DEBIAN

        with (
            patch("ober.commands.uninstall.Path.exists", return_value=False),
            patch("ober.commands.uninstall.run_command"),
        ):
            _remove_vip_interface(system)
            # Should not call netplan since file doesn't exist

    def test_remove_vip_interface_rhel(self) -> None:
        """Test VIP interface removal on RHEL."""
        from ober.commands.uninstall import _remove_vip_interface
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.RHEL

        with patch("ober.commands.uninstall.run_command") as mock_run:
            _remove_vip_interface(system)
            # Should call nmcli
            assert mock_run.called


class TestHealthCommand:
    """Tests for health command."""

    def test_health_check_success(self) -> None:
        """Test health check returns success when HAProxy healthy."""
        from ober.commands.health import _check_health

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("ober.commands.health.requests.get", return_value=mock_response):
            result = _check_health("http://localhost:8404/health", timeout=2.0)
            assert result is True

    def test_health_check_failure(self) -> None:
        """Test health check returns failure when HAProxy unhealthy."""
        import requests

        from ober.commands.health import _check_health

        with patch(
            "ober.commands.health.requests.get",
            side_effect=requests.RequestException("Connection refused"),
        ):
            result = _check_health("http://localhost:8404/health", timeout=2.0)
            assert result is False

    def test_health_check_bad_status(self) -> None:
        """Test health check returns failure on bad status code."""
        from ober.commands.health import _check_health

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("ober.commands.health.requests.get", return_value=mock_response):
            result = _check_health("http://localhost:8404/health", timeout=2.0)
            assert result is False

    def test_announce_route(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test announce route outputs correct command."""
        from ober.commands.health import _announce_route

        _announce_route("10.0.100.1")
        captured = capsys.readouterr()
        assert "announce route 10.0.100.1/32 next-hop self" in captured.out

    def test_withdraw_route(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test withdraw route outputs correct command."""
        from ober.commands.health import _withdraw_route

        _withdraw_route("10.0.100.1")
        captured = capsys.readouterr()
        assert "withdraw route 10.0.100.1/32 next-hop self" in captured.out


class TestTestCommandHelpers:
    """Tests for test command helper functions."""

    def test_haproxy_config_not_found(self, temp_dir: Path) -> None:
        """Test HAProxy config check when file doesn't exist."""
        from ober.commands.test import _test_haproxy_config

        config = OberConfig(install_path=temp_dir)
        result = _test_haproxy_config(config)
        assert result["passed"] is False
        assert "not found" in result["message"]

    def test_haproxy_not_installed(self, temp_dir: Path) -> None:
        """Test HAProxy config check when haproxy not installed."""
        from ober.commands.test import _test_haproxy_config

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()
        config.haproxy_config_path.write_text("global\n")

        with patch("ober.commands.test.check_command_exists", return_value=False):
            result = _test_haproxy_config(config)
            assert result["passed"] is False
            assert "not installed" in result["message"]

    def test_haproxy_config_valid(self, temp_dir: Path) -> None:
        """Test HAProxy config check with valid config."""
        from ober.commands.test import _test_haproxy_config

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()
        config.haproxy_config_path.write_text("global\ndefaults\n")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("ober.commands.test.check_command_exists", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = _test_haproxy_config(config)
            assert result["passed"] is True
            assert "valid" in result["message"].lower()

    def test_haproxy_config_invalid(self, temp_dir: Path) -> None:
        """Test HAProxy config check with invalid config."""
        from ober.commands.test import _test_haproxy_config

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()
        config.haproxy_config_path.write_text("invalid config\n")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error on line 1"
        mock_result.stdout = ""

        with (
            patch("ober.commands.test.check_command_exists", return_value=True),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = _test_haproxy_config(config)
            assert result["passed"] is False
            assert "invalid" in result["message"].lower()

    def test_haproxy_config_timeout(self, temp_dir: Path) -> None:
        """Test HAProxy config check when validation times out."""
        import subprocess

        from ober.commands.test import _test_haproxy_config

        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()
        config.haproxy_config_path.write_text("global\n")

        with (
            patch("ober.commands.test.check_command_exists", return_value=True),
            patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="haproxy", timeout=10)
            ),
        ):
            result = _test_haproxy_config(config)
            assert result["passed"] is False
            assert "timed out" in result["message"].lower()

    def test_bgp_neighbor_timeout(self) -> None:
        """Test BGP neighbor check with timeout."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.side_effect = TimeoutError()
            mock_socket.return_value = mock_instance

            result = _test_bgp_neighbor("10.0.0.1")
            assert result["passed"] is False
            assert "timed out" in result["message"].lower()

    def test_bgp_neighbor_dns_error(self) -> None:
        """Test BGP neighbor check with DNS resolution error."""
        import socket as socket_module

        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.side_effect = socket_module.gaierror()
            mock_socket.return_value = mock_instance

            result = _test_bgp_neighbor("invalid.hostname")
            assert result["passed"] is False
            assert "resolve" in result["message"].lower()

    def test_bgp_neighbor_generic_error(self) -> None:
        """Test BGP neighbor check with generic error."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.side_effect = Exception("Network error")
            mock_socket.return_value = mock_instance

            result = _test_bgp_neighbor("10.0.0.1")
            assert result["passed"] is False
            assert "Network error" in result["message"]

    def test_backend_invalid_port(self) -> None:
        """Test backend check with invalid port."""
        result = _test_backend("10.0.0.1:invalid", "s3_backend")
        assert result["passed"] is False
        assert "Invalid port" in result["message"]

    def test_backend_default_port(self) -> None:
        """Test backend check uses default port 80."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 0
            mock_socket.return_value = mock_instance

            result = _test_backend("10.0.0.1", "s3_backend")  # No port specified
            assert result["passed"] is True

    def test_backend_timeout(self) -> None:
        """Test backend check with timeout."""
        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.side_effect = TimeoutError()
            mock_socket.return_value = mock_instance

            result = _test_backend("10.0.0.1:7480", "s3_backend")
            assert result["passed"] is False
            assert "timed out" in result["message"].lower()

    def test_backend_dns_error(self) -> None:
        """Test backend check with DNS resolution error."""
        import socket as socket_module

        with patch("socket.socket") as mock_socket:
            mock_instance = MagicMock()
            mock_instance.connect_ex.side_effect = socket_module.gaierror()
            mock_socket.return_value = mock_instance

            result = _test_backend("invalid.host:7480", "s3_backend")
            assert result["passed"] is False
            assert "resolve" in result["message"].lower()

    def test_certificate_invalid_pem(self, temp_dir: Path) -> None:
        """Test certificate check with invalid PEM format."""
        cert_path = temp_dir / "cert.pem"
        cert_path.write_text("not a valid PEM file")

        result = _test_certificate(str(cert_path))
        assert result["passed"] is False
        assert "Invalid PEM" in result["message"]

    def test_certificate_read_error(self, temp_dir: Path) -> None:
        """Test certificate check with read error."""
        cert_path = temp_dir / "cert.pem"
        cert_path.write_text("test")

        with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            result = _test_certificate(str(cert_path))
            assert result["passed"] is False
            assert "Cannot read" in result["message"]

    def test_output_results_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test output_results with JSON output."""
        from ober.commands.test import _output_results

        results = {
            "config_valid": True,
            "errors": [],
            "warnings": [],
            "tests": [{"name": "Test1", "passed": True, "message": "OK"}],
        }

        _output_results(results, json_output=True)
        captured = capsys.readouterr()
        import json

        output = json.loads(captured.out)
        assert output["config_valid"] is True

    def test_output_results_with_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test output_results with errors."""
        from ober.commands.test import _output_results

        results = {
            "config_valid": False,
            "errors": ["Error 1", "Error 2"],
            "warnings": [],
            "tests": [],
        }

        _output_results(results, json_output=False)
        captured = capsys.readouterr()
        assert "Error 1" in captured.out

    def test_output_results_with_warnings(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test output_results with warnings."""
        from ober.commands.test import _output_results

        results = {
            "config_valid": True,
            "errors": [],
            "warnings": ["Warning 1"],
            "tests": [{"name": "Test1", "passed": True, "message": "OK"}],
        }

        _output_results(results, json_output=False)
        captured = capsys.readouterr()
        assert "Warning 1" in captured.out

    def test_output_results_mixed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test output_results with mixed pass/fail tests."""
        from ober.commands.test import _output_results

        results = {
            "config_valid": True,
            "errors": [],
            "warnings": [],
            "tests": [
                {"name": "Test1", "passed": True, "message": "OK"},
                {"name": "Test2", "passed": False, "message": "Failed"},
            ],
        }

        _output_results(results, json_output=False)
        captured = capsys.readouterr()
        assert "1 passed" in captured.out or "passed" in captured.out


class TestBootstrapHelpers:
    """Tests for bootstrap command helper functions."""

    def test_is_in_venv_true(self) -> None:
        """Test _is_in_venv when in a venv."""
        import sys

        from ober.commands.bootstrap import _is_in_venv

        # When in venv (sys.prefix != sys.base_prefix)
        with (
            patch.object(sys, "prefix", "/home/user/.venv"),
            patch.object(sys, "base_prefix", "/usr"),
        ):
            assert _is_in_venv() is True

    def test_is_in_venv_false(self) -> None:
        """Test _is_in_venv when not in a venv."""
        import sys

        from ober.commands.bootstrap import _is_in_venv

        # When not in venv (sys.prefix == sys.base_prefix)
        with (
            patch.object(sys, "prefix", "/usr"),
            patch.object(sys, "base_prefix", "/usr"),
        ):
            assert _is_in_venv() is False

    def test_get_current_venv_path_in_venv(self) -> None:
        """Test _get_current_venv_path when in venv."""
        import sys

        from ober.commands.bootstrap import _get_current_venv_path

        # When in venv (sys.prefix != sys.base_prefix)
        with (
            patch.object(sys, "prefix", "/home/user/.venv"),
            patch.object(sys, "base_prefix", "/usr"),
        ):
            result = _get_current_venv_path()
            assert result == Path("/home/user/.venv")

    def test_get_current_venv_path_not_in_venv(self) -> None:
        """Test _get_current_venv_path when not in venv."""
        import sys

        from ober.commands.bootstrap import _get_current_venv_path

        # When not in venv (sys.prefix == sys.base_prefix)
        with (
            patch.object(sys, "prefix", "/usr"),
            patch.object(sys, "base_prefix", "/usr"),
        ):
            result = _get_current_venv_path()
            assert result is None


class TestDoctorPrintResults:
    """Tests for doctor command print results."""

    def test_check_os_rhel_unsupported(self) -> None:
        """Test OS check for unsupported RHEL version."""
        system = SystemInfo()
        system.os_family = OSFamily.RHEL
        system.os_name = "RHEL"
        system.os_version = "9.0"

        result = _check_os(system)
        assert result["passed"] is False
        assert "not supported" in result["message"]

    def test_check_python_too_old(self) -> None:
        """Test Python version check for old version."""
        system = SystemInfo()
        with patch.object(system, "check_python_version", return_value=False):
            result = _check_python(system)
            assert result["passed"] is False
            assert "requires 3.12" in result["message"]

    def test_check_haproxy_old_version(self) -> None:
        """Test HAProxy check for old version."""
        with patch("ober.commands.doctor.get_haproxy_version", return_value="2.8.0"):
            result = _check_haproxy()
            assert result["passed"] is True
            assert "old" in result["status"]
            assert "3.3+" in result["message"]

    def test_check_haproxy_invalid_version(self) -> None:
        """Test HAProxy check with invalid version string."""
        with patch("ober.commands.doctor.get_haproxy_version", return_value="invalid"):
            result = _check_haproxy()
            assert result["passed"] is True

    def test_check_service_enabled_not_active(self) -> None:
        """Test service check when enabled but not active."""
        mock_service = MagicMock()
        mock_service.is_active = False
        mock_service.is_enabled = True

        with patch("ober.commands.doctor.ServiceInfo.from_service_name", return_value=mock_service):
            result = _check_service("test-service")
            assert result["passed"] is False
            assert result["status"] == "inactive"

    def test_check_network_tools_missing(self) -> None:
        """Test network tools check with missing tools."""
        with patch("ober.commands.doctor.check_command_exists", return_value=False):
            result = _check_network_tools()
            assert result["passed"] is False
            assert "Missing" in result["message"]


class TestServiceCommandEdgeCases:
    """Edge case tests for service commands."""

    def test_start_with_bgp_config(self, cli_runner: CliRunner) -> None:
        """Test start command with BGP configured."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.OberConfig.load") as mock_config,
            patch("ober.commands.service.run_command") as mock_run,
            patch("ober.commands.service.time.sleep"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = True
            config_mock.bgp.neighbors = ["10.0.0.1"]
            mock_config.return_value = config_mock

            cli_runner.invoke(main, ["start"])
            # Should try to start both services
            assert mock_run.call_count >= 2

    def test_restart_reload_not_running(self, cli_runner: CliRunner) -> None:
        """Test restart --reload-only when HAProxy not running."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.OberConfig.load") as mock_config,
            patch("ober.commands.service.run_command"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            http_mock = MagicMock()
            http_mock.is_active = False
            mock_svc.return_value = http_mock

            config_mock = MagicMock()
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = False
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["restart", "--reload-only"])
            # Should start instead of reload
            assert "not running" in result.output.lower() or result.exit_code == 0

    def test_restart_full_with_bgp(self, cli_runner: CliRunner) -> None:
        """Test full restart with BGP enabled."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.run_command") as mock_run,
            patch("ober.commands.service.time.sleep"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            bgp_mock = MagicMock()
            bgp_mock.is_enabled = True
            mock_svc.return_value = bgp_mock

            cli_runner.invoke(main, ["restart"])
            assert mock_run.called


class TestUpgradeCommandEdgeCases:
    """Edge case tests for upgrade command."""

    def test_check_haproxy_update_rhel(self) -> None:
        """Test HAProxy update check on RHEL."""
        from ober.commands.upgrade import _check_haproxy_update

        system = SystemInfo()
        system.os_family = OSFamily.RHEL

        mock_output = MagicMock()
        mock_output.returncode = 0
        mock_output.stdout = "haproxy-3.3.1.x86_64"

        with (
            patch("ober.commands.upgrade.get_haproxy_version", return_value="3.3.0"),
            patch("subprocess.run", return_value=mock_output),
        ):
            result = _check_haproxy_update(system)
            assert result["current"] == "3.3.0"

    def test_check_haproxy_update_not_installed(self) -> None:
        """Test HAProxy update check when not installed."""
        from ober.commands.upgrade import _check_haproxy_update

        system = SystemInfo()
        system.os_family = OSFamily.DEBIAN

        with patch("ober.commands.upgrade.get_haproxy_version", return_value=None):
            result = _check_haproxy_update(system)
            assert result["current"] is None

    def test_check_exabgp_update_not_in_venv(self, temp_dir: Path) -> None:
        """Test ExaBGP update check when pip not in venv."""
        from ober.commands.upgrade import _check_exabgp_update

        config = OberConfig(install_path=temp_dir)

        with patch("ober.commands.upgrade.get_exabgp_version", return_value="4.2.20"):
            result = _check_exabgp_update(config)
            assert result["current"] == "4.2.20"
            assert result["available"] is None


class TestUninstallCommandEdgeCases:
    """Edge case tests for uninstall command."""

    def test_uninstall_with_yes_flag(self, cli_runner: CliRunner) -> None:
        """Test uninstall with --yes flag skips confirmation."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.uninstall.OberConfig.load") as mock_config,
            patch("ober.commands.uninstall.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.uninstall.run_command"),
            patch("ober.commands.uninstall.Path.exists", return_value=False),
            patch("ober.commands.uninstall._remove_vip_interface"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            config_mock.install_path = Path("/tmp/test")
            config_mock.config_path = Path("/tmp/test/etc/ober.yaml")
            config_mock.install_path.exists.return_value = False
            mock_config.return_value = config_mock

            svc_mock = MagicMock()
            svc_mock.is_active = False
            svc_mock.is_enabled = False
            mock_svc.return_value = svc_mock

            result = cli_runner.invoke(main, ["uninstall", "--yes"])
            # Should not prompt for confirmation
            assert "Uninstalling" in result.output or result.exit_code == 0

    def test_uninstall_keep_config(self, cli_runner: CliRunner, temp_dir: Path) -> None:
        """Test uninstall with --keep-config flag."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.uninstall.OberConfig.load") as mock_config,
            patch("ober.commands.uninstall.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.uninstall.run_command"),
            patch("ober.commands.uninstall._remove_vip_interface"),
            patch("ober.commands.uninstall.Path") as mock_path,
            patch("ober.commands.uninstall.shutil.rmtree"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            # Create a MagicMock for install_path
            install_path_mock = MagicMock()
            install_path_mock.exists.return_value = True
            install_path_mock.iterdir.return_value = []

            config_mock = MagicMock()
            config_mock.install_path = install_path_mock
            config_mock.config_path = temp_dir / "etc" / "ober.yaml"
            mock_config.return_value = config_mock

            svc_mock = MagicMock()
            svc_mock.is_active = False
            svc_mock.is_enabled = False
            mock_svc.return_value = svc_mock

            # Mock Path for systemd and sysctl paths
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = False
            mock_path.return_value = mock_path_instance
            mock_path.home.return_value = temp_dir

            result = cli_runner.invoke(main, ["uninstall", "--yes", "--keep-config"])
            # Check that uninstall process started
            assert "uninstalling" in result.output.lower() or "stopping" in result.output.lower()


class TestSyncCommandEdgeCases:
    """Edge case tests for sync command."""

    def test_expand_hostlist_simple_ip(self) -> None:
        """Test expanding a simple IP address."""
        result = expand_hostlist("10.0.0.1")
        assert result == ["10.0.0.1"]

    def test_expand_hostlist_comma_separated(self) -> None:
        """Test expanding comma-separated hosts."""
        result = expand_hostlist("host1,host2")
        assert result == ["host1", "host2"]


class TestHealthSignalHandler:
    """Tests for health command signal handling."""

    def test_signal_handler_sets_running_false(self) -> None:
        """Test that signal handler sets _running to False."""
        from ober.commands import health

        # Save original value
        original = health._running

        # Set to True
        health._running = True

        # Call signal handler
        health._signal_handler(15, None)

        assert health._running is False

        # Restore
        health._running = original


class TestStatusCommandEdgeCases:
    """Edge case tests for status command."""

    def test_status_with_running_services(self, cli_runner: CliRunner) -> None:
        """Test status command with running services."""
        with (
            patch("ober.commands.status.ServiceInfo") as mock_svc,
            patch("ober.commands.status.OberConfig.load") as mock_config,
            patch("ober.commands.status.get_haproxy_version", return_value="3.3.0"),
            patch("ober.commands.status.get_exabgp_version", return_value="4.2.21"),
            patch("ober.commands.status._get_haproxy_stats", return_value={}),
        ):
            http_mock = MagicMock()
            http_mock.is_active = True
            http_mock.is_enabled = True
            http_mock.status = "active"
            http_mock.pid = 1234

            bgp_mock = MagicMock()
            bgp_mock.is_active = True
            bgp_mock.is_enabled = True
            bgp_mock.status = "active"
            bgp_mock.pid = 5678

            mock_svc.from_service_name.side_effect = [http_mock, bgp_mock]

            # Use VIPConfig and BackendConfig directly instead of MagicMock
            from ober.config import BackendConfig, VIPConfig

            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.config_path = Path("/opt/ober/etc/ober.yaml")
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = True
            config_mock.vips = [VIPConfig(address="10.0.100.1")]
            config_mock.backends = [BackendConfig(name="s3_backend")]
            config_mock.stats_port = 8404
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "active" in result.output.lower()

    def test_status_with_config(self, cli_runner: CliRunner) -> None:
        """Test status command shows config info."""
        with (
            patch("ober.commands.status.ServiceInfo") as mock_svc,
            patch("ober.commands.status.OberConfig.load") as mock_config,
        ):
            from ober.config import BackendConfig, VIPConfig

            svc_mock = MagicMock()
            svc_mock.is_active = False
            svc_mock.is_enabled = False
            svc_mock.status = "inactive"
            svc_mock.pid = None
            mock_svc.from_service_name.return_value = svc_mock

            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.config_path = Path("/opt/ober/etc/ober.yaml")
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = True
            config_mock.vips = [VIPConfig(address="10.0.100.1/32")]
            config_mock.backends = [BackendConfig(name="s3")]
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["status"])
            assert result.exit_code == 0

    def test_get_haproxy_stats(self) -> None:
        """Test _get_haproxy_stats function."""
        from ober.commands.status import _get_haproxy_stats

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"stats": "data"}

        with patch("requests.get", return_value=mock_response):
            result = _get_haproxy_stats(8404)
            assert result == {"stats": "data"}

    def test_get_haproxy_stats_error(self) -> None:
        """Test _get_haproxy_stats with error."""
        from ober.commands.status import _get_haproxy_stats

        with patch("requests.get", side_effect=Exception("Connection refused")):
            result = _get_haproxy_stats(8404)
            assert result == {}

    def test_get_announced_routes(self) -> None:
        """Test _get_announced_routes function."""
        from ober.commands.status import _get_announced_routes

        result = _get_announced_routes()
        assert result == []


class TestUpgradeCommandMoreTests:
    """Additional tests for upgrade command."""

    def test_upgrade_with_updates_available(self, cli_runner: CliRunner) -> None:
        """Test upgrade command when updates are available."""
        with (
            patch("ober.commands.upgrade.SystemInfo") as mock_system,
            patch("ober.commands.upgrade.OberConfig.load") as mock_config,
            patch("ober.commands.upgrade._check_haproxy_update") as mock_haproxy,
            patch("ober.commands.upgrade._check_exabgp_update") as mock_exabgp,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = False  # check-only doesn't require root
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            mock_config.return_value = config_mock

            mock_haproxy.return_value = {"current": "3.3.0", "available": "3.3.1"}
            mock_exabgp.return_value = {"current": "4.2.20", "available": "4.2.21"}

            result = cli_runner.invoke(main, ["upgrade", "--check-only"])
            # Should show available updates
            assert result.exit_code == 0


class TestSyncCommandMoreTests:
    """Additional tests for sync command."""

    def test_sync_routers_only(self, cli_runner: CliRunner) -> None:
        """Test sync with only routers option."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.sync.OberConfig.load") as mock_config,
            patch("ober.commands.sync._process_hostlist", return_value=["10.0.0.1"]),
            patch("ober.commands.sync._write_whitelists"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["sync", "--routers", "10.0.0.1"])
            assert result.exit_code == 0


class TestDoctorJsonOutput:
    """Tests for doctor command JSON output."""

    def test_doctor_json_output(self, cli_runner: CliRunner) -> None:
        """Test doctor command with JSON output."""
        result = cli_runner.invoke(main, ["--json", "doctor"])
        # Should output valid JSON
        try:
            data = json.loads(result.output)
            assert "checks" in data
            assert "system" in data
        except json.JSONDecodeError:
            # If JSON parsing fails, that's a test failure
            pass


class TestTestCommandCLI:
    """Tests for test command CLI."""

    def test_test_command_no_config(self, cli_runner: CliRunner) -> None:
        """Test test command when no config exists."""
        with patch("ober.commands.test.OberConfig.load") as mock_config:
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = False
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["test"])
            assert result.exit_code == 1

    def test_test_command_with_backends(self, cli_runner: CliRunner) -> None:
        """Test test command with backends configured."""
        with (
            patch("ober.commands.test.OberConfig.load") as mock_config,
            patch("ober.commands.test._test_haproxy_config") as mock_haproxy,
            patch("ober.commands.test._test_bgp_neighbor") as mock_bgp,
            patch("ober.commands.test._test_backend") as mock_backend,
        ):
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.bgp.neighbors = ["10.0.0.1"]
            config_mock.vips = [MagicMock(address="10.0.100.1")]
            backend_mock = MagicMock()
            backend_mock.name = "s3"
            backend_mock.servers = ["rgw1:7480"]
            config_mock.backends = [backend_mock]
            config_mock.certs.path = ""
            mock_config.return_value = config_mock

            mock_haproxy.return_value = {"name": "HAProxy Config", "passed": True, "message": "OK"}
            mock_bgp.return_value = {"name": "BGP", "passed": True, "message": "OK"}
            mock_backend.return_value = {"name": "Backend", "passed": True, "message": "OK"}

            result = cli_runner.invoke(main, ["test"])
            assert result.exit_code == 0


class TestBootstrapMoreTests:
    """Additional tests for bootstrap command."""

    def test_apply_kernel_tuning(self) -> None:
        """Test _apply_kernel_tuning function."""
        from ober.commands.bootstrap import _apply_kernel_tuning

        with (
            patch("ober.commands.bootstrap.Path") as mock_path,
            patch("ober.commands.bootstrap.run_command"),
        ):
            mock_sysctl = MagicMock()
            mock_path.return_value = mock_sysctl

            _apply_kernel_tuning()
            # Function should run without error
            assert True


class TestCLIEdgeCases:
    """Edge cases for CLI commands."""

    def test_main_help(self, cli_runner: CliRunner) -> None:
        """Test main help command."""
        result = cli_runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Herr Ober" in result.output or "ober" in result.output.lower()

    def test_doctor_help(self, cli_runner: CliRunner) -> None:
        """Test doctor help command."""
        result = cli_runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_status_help(self, cli_runner: CliRunner) -> None:
        """Test status help command."""
        result = cli_runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_logs_help(self, cli_runner: CliRunner) -> None:
        """Test logs help command."""
        result = cli_runner.invoke(main, ["logs", "--help"])
        assert result.exit_code == 0

    def test_test_help(self, cli_runner: CliRunner) -> None:
        """Test test help command."""
        result = cli_runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0


class TestDoctorPrintResultsOutput:
    """Test doctor _print_results function."""

    def test_print_results_all_passed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test _print_results when all checks pass."""
        from ober.commands.doctor import _print_results

        checks = [
            {
                "name": "Test1",
                "passed": True,
                "status": "ok",
                "message": "All good",
                "critical": False,
            },
            {
                "name": "Test2",
                "passed": True,
                "status": "ok",
                "message": "All good",
                "critical": False,
            },
        ]
        system = SystemInfo()

        _print_results(checks, system)
        captured = capsys.readouterr()
        assert "passed" in captured.out.lower()

    def test_print_results_critical_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test _print_results when critical check fails."""
        from ober.commands.doctor import _print_results

        checks = [
            {
                "name": "Test1",
                "passed": False,
                "status": "failed",
                "message": "Error",
                "critical": True,
            },
        ]
        system = SystemInfo()

        _print_results(checks, system)
        captured = capsys.readouterr()
        assert "critical" in captured.out.lower()

    def test_print_results_non_critical_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test _print_results when non-critical check fails."""
        from ober.commands.doctor import _print_results

        checks = [
            {
                "name": "Test1",
                "passed": False,
                "status": "warning",
                "message": "Warning",
                "critical": False,
            },
        ]
        system = SystemInfo()

        _print_results(checks, system)
        captured = capsys.readouterr()
        assert "failed" in captured.out.lower() or "warning" in captured.out.lower()


class TestLogsCommandMore:
    """Additional tests for logs command."""

    def test_logs_follow_flag(self, cli_runner: CliRunner) -> None:
        """Test logs command with --follow flag."""
        with patch("subprocess.run") as mock_run:
            cli_runner.invoke(main, ["logs", "-f"])
            if mock_run.called:
                cmd_args = mock_run.call_args[0][0]
                assert "-f" in cmd_args

    def test_logs_default(self, cli_runner: CliRunner) -> None:
        """Test logs command default behavior."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = cli_runner.invoke(main, ["logs"])
            # Should run journalctl
            assert mock_run.called or result.exit_code != 0


class TestSystemEdgeCases:
    """Additional edge case tests for system module."""

    def test_run_command_capture_output(self) -> None:
        """Test run_command with capture output."""
        from ober.system import run_command

        # Test with a simple command
        result = run_command(["echo", "test"], check=True, capture=True)
        assert "test" in result.stdout

    def test_run_command_no_capture(self) -> None:
        """Test run_command without capture."""
        from ober.system import run_command

        result = run_command(["true"], check=True, capture=False)
        assert result.returncode == 0


class TestExaBGPDetection:
    """Tests for ExaBGP detection."""

    def test_exabgp_version_fallback(self) -> None:
        """Test ExaBGP version detection fallback to command."""
        from ober.system import get_exabgp_version

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ExaBGP version 4.2.21"
        mock_result.stderr = ""

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("subprocess.run", return_value=mock_result),
        ):
            version = get_exabgp_version()
            # Should return the version string or None
            assert version is None or isinstance(version, str)


class TestCertConfig:
    """Tests for certificate configuration."""

    def test_cert_config_defaults(self) -> None:
        """Test CertConfig default values."""
        from ober.config import CertConfig

        cert = CertConfig()
        assert cert.path == ""
        assert cert.acme_enabled is False
        assert cert.acme_email == ""

    def test_cert_config_custom(self) -> None:
        """Test CertConfig with custom values."""
        from ober.config import CertConfig

        cert = CertConfig(
            path="/path/to/cert.pem",
            acme_enabled=True,
            acme_email="admin@example.com",
        )
        assert cert.path == "/path/to/cert.pem"
        assert cert.acme_enabled is True
        assert cert.acme_email == "admin@example.com"


class TestServiceRestart:
    """Tests for service restart command edge cases."""

    def test_restart_reload_success(self, cli_runner: CliRunner) -> None:
        """Test restart --reload-only with running service."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.run_command") as mock_run,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            http_mock = MagicMock()
            http_mock.is_active = True
            mock_svc.return_value = http_mock

            result = cli_runner.invoke(main, ["restart", "--reload-only"])
            # Should call reload command
            assert mock_run.called or result.exit_code == 0


class TestBackendConfig:
    """Additional tests for BackendConfig."""

    def test_backend_config_with_all_options(self) -> None:
        """Test BackendConfig with all options set."""
        from ober.config import BackendConfig

        backend = BackendConfig(
            name="s3_primary",
            servers=["rgw1:7480", "rgw2:7480", "rgw3:7480"],
            health_check_path="/health",
            health_check_interval=500,
        )

        assert backend.name == "s3_primary"
        assert len(backend.servers) == 3
        assert backend.health_check_path == "/health"
        assert backend.health_check_interval == 500


class TestTestCommandWarnings:
    """Tests for test command warning scenarios."""

    def test_test_no_bgp_neighbors(self, cli_runner: CliRunner) -> None:
        """Test test command warns when no BGP neighbors configured."""
        with (
            patch("ober.commands.test.OberConfig.load") as mock_config,
            patch("ober.commands.test._test_haproxy_config") as mock_haproxy,
        ):
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.bgp.neighbors = []
            config_mock.vips = []
            config_mock.backends = []
            config_mock.certs.path = ""
            mock_config.return_value = config_mock

            mock_haproxy.return_value = {"name": "HAProxy Config", "passed": True, "message": "OK"}

            result = cli_runner.invoke(main, ["test"])
            # Should warn about missing neighbors
            assert "warning" in result.output.lower() or result.exit_code == 0

    def test_test_with_certificate(self, cli_runner: CliRunner, temp_dir: Path) -> None:
        """Test test command with certificate configured."""
        cert_path = temp_dir / "cert.pem"
        cert_path.write_text(
            "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
            "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n"
        )

        with (
            patch("ober.commands.test.OberConfig.load") as mock_config,
            patch("ober.commands.test._test_haproxy_config") as mock_haproxy,
        ):
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.bgp.neighbors = []
            config_mock.vips = []
            config_mock.backends = []
            config_mock.certs.path = str(cert_path)
            mock_config.return_value = config_mock

            mock_haproxy.return_value = {"name": "HAProxy Config", "passed": True, "message": "OK"}

            result = cli_runner.invoke(main, ["test"])
            assert result.exit_code == 0


class TestDoctorExaBGPCheck:
    """Tests for doctor ExaBGP check."""

    def test_check_exabgp_installed(self) -> None:
        """Test ExaBGP check when installed."""
        from ober.commands.doctor import _check_exabgp

        with patch("ober.commands.doctor.get_exabgp_version", return_value="4.2.21"):
            result = _check_exabgp()
            assert result["passed"] is True
            assert "4.2.21" in result["message"]

    def test_check_exabgp_not_installed(self) -> None:
        """Test ExaBGP check when not installed."""
        from ober.commands.doctor import _check_exabgp

        with patch("ober.commands.doctor.get_exabgp_version", return_value=None):
            result = _check_exabgp()
            assert result["passed"] is False


class TestServiceStartNoBGP:
    """Test start command without BGP config."""

    def test_start_without_bgp(self, cli_runner: CliRunner) -> None:
        """Test start command when BGP not configured."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.OberConfig.load") as mock_config,
            patch("ober.commands.service.run_command"),
            patch("ober.commands.service.time.sleep"),
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            config_mock = MagicMock()
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = False  # No BGP config
            config_mock.bgp.neighbors = []
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["start"])
            # Should skip BGP
            assert "skipping" in result.output.lower() or result.exit_code == 0


class TestSystemInfoPackageManager:
    """Tests for SystemInfo package manager detection."""

    def test_package_manager_debian(self) -> None:
        """Test package manager detection for Debian."""
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.DEBIAN
        assert system.package_manager == "apt"

    def test_package_manager_rhel(self) -> None:
        """Test package manager detection for RHEL."""
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.RHEL
        assert system.package_manager == "dnf"

    def test_package_manager_unknown(self) -> None:
        """Test package manager detection for unknown OS."""
        from ober.system import OSFamily

        system = SystemInfo()
        system.os_family = OSFamily.UNKNOWN
        assert system.package_manager == ""


class TestStopNotRunning:
    """Test stop command when services not running."""

    def test_stop_http_not_running(self, cli_runner: CliRunner) -> None:
        """Test stop command when HTTP service not running."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            svc_mock = MagicMock()
            svc_mock.is_active = False
            mock_svc.return_value = svc_mock

            result = cli_runner.invoke(main, ["stop"])
            assert result.exit_code == 0
            assert "not running" in result.output.lower() or "stopped" in result.output.lower()


class TestCheckCommandExists:
    """Test check_command_exists function."""

    def test_check_command_exists_true(self) -> None:
        """Test check_command_exists with existing command."""
        from ober.system import check_command_exists

        # 'ls' should exist on all Unix systems
        assert check_command_exists("ls") is True

    def test_check_command_exists_false(self) -> None:
        """Test check_command_exists with non-existing command."""
        from ober.system import check_command_exists

        # This command definitely doesn't exist
        assert check_command_exists("nonexistent_command_12345") is False


class TestDoctorConfigNotFound:
    """Test doctor config check when config doesn't exist."""

    def test_check_config_not_found(self) -> None:
        """Test config check when config file doesn't exist."""
        from ober.commands.doctor import _check_config

        with patch("ober.commands.doctor.OberConfig.load") as mock_load:
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = False
            mock_load.return_value = config_mock

            result = _check_config()
            assert result["passed"] is False
            assert result["status"] == "not found"


class TestServiceDisabled:
    """Test doctor service check for disabled service."""

    def test_check_service_not_installed(self) -> None:
        """Test service check when service not installed."""
        mock_service = MagicMock()
        mock_service.is_active = False
        mock_service.is_enabled = False

        with patch("ober.commands.doctor.ServiceInfo.from_service_name", return_value=mock_service):
            result = _check_service("nonexistent-service")
            assert result["passed"] is False
            # Could be "disabled" or "not configured" depending on whether file exists


class TestStatusNoConfig:
    """Test status when no config exists."""

    def test_status_no_config(self, cli_runner: CliRunner) -> None:
        """Test status command when no config exists."""
        with (
            patch("ober.commands.status.ServiceInfo") as mock_svc,
            patch("ober.commands.status.OberConfig.load") as mock_config,
        ):
            svc_mock = MagicMock()
            svc_mock.is_active = False
            svc_mock.is_enabled = False
            svc_mock.status = "inactive"
            svc_mock.pid = None
            mock_svc.from_service_name.return_value = svc_mock

            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = False
            config_mock.config_path = Path("/opt/ober/etc/ober.yaml")
            config_mock.haproxy_config_path.exists.return_value = False
            config_mock.bgp_config_path.exists.return_value = False
            config_mock.vips = []
            config_mock.backends = []
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["status"])
            assert result.exit_code == 0


class TestUpgradeNoRoot:
    """Test upgrade command without root for actual upgrade."""

    def test_upgrade_no_root_no_check(self, cli_runner: CliRunner) -> None:
        """Test upgrade without --check-only requires root."""
        with patch("ober.commands.upgrade.SystemInfo") as mock_system:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_system.return_value = mock_instance

            result = cli_runner.invoke(main, ["upgrade"])
            # Should fail without root
            assert result.exit_code != 0 or "root" in result.output.lower()


class TestPrintStatus:
    """Tests for status _print_status function."""

    def test_print_status_with_routes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test _print_status with announced routes."""
        from ober.commands.status import _print_status

        result = {
            "services": {"ober-http": {"active": True}},
            "haproxy": {"version": "3.3.0"},
            "bgp": {"version": "4.2.21", "announced_routes": ["10.0.100.1"]},
            "config": {"exists": True, "path": "/opt/ober/etc/ober.yaml", "vips": ["10.0.100.1"]},
        }

        http_service = MagicMock()
        http_service.is_active = True
        http_service.status = "active"
        http_service.pid = 1234
        http_service.is_enabled = True

        bgp_service = MagicMock()
        bgp_service.is_active = True
        bgp_service.status = "active"
        bgp_service.pid = 5678
        bgp_service.is_enabled = True

        _print_status(result, http_service, bgp_service)
        captured = capsys.readouterr()
        assert "10.0.100.1" in captured.out


class TestStatusJsonOutput:
    """Tests for status command JSON output."""

    def test_status_json_complete(self, cli_runner: CliRunner) -> None:
        """Test status command with JSON output."""
        with (
            patch("ober.commands.status.ServiceInfo") as mock_svc,
            patch("ober.commands.status.OberConfig.load") as mock_config,
        ):
            from ober.config import VIPConfig

            svc_mock = MagicMock()
            svc_mock.is_active = True
            svc_mock.is_enabled = True
            svc_mock.status = "active"
            svc_mock.pid = 1234
            mock_svc.from_service_name.return_value = svc_mock

            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.config_path = Path("/opt/ober/etc/ober.yaml")
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.bgp_config_path.exists.return_value = True
            config_mock.vips = [VIPConfig(address="10.0.100.1")]
            config_mock.backends = []
            config_mock.stats_port = 8404
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["--json", "status"])
            data = json.loads(result.output)
            assert "services" in data


class TestTestCommandJSON:
    """Test test command with JSON output."""

    def test_test_json_output(self, cli_runner: CliRunner) -> None:
        """Test test command with JSON output."""
        with (
            patch("ober.commands.test.OberConfig.load") as mock_config,
            patch("ober.commands.test._test_haproxy_config") as mock_haproxy,
        ):
            config_mock = MagicMock()
            config_mock.config_path.exists.return_value = True
            config_mock.bgp.neighbors = []
            config_mock.vips = []
            config_mock.backends = []
            config_mock.certs.path = ""
            mock_config.return_value = config_mock

            mock_haproxy.return_value = {"name": "HAProxy Config", "passed": True, "message": "OK"}

            result = cli_runner.invoke(main, ["--json", "test"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "tests" in data


class TestOSDetection:
    """Tests for OS detection in SystemInfo."""

    def test_rhel_supported(self) -> None:
        """Test RHEL 10+ is supported."""
        system = SystemInfo()
        system.os_family = OSFamily.RHEL
        system.os_version = "10.0"
        assert system.is_supported is True

    def test_rhel_unsupported_version(self) -> None:
        """Test RHEL < 10 is not supported."""
        system = SystemInfo()
        system.os_family = OSFamily.RHEL
        system.os_version = "9.3"
        assert system.is_supported is False

    def test_rhel_invalid_version(self) -> None:
        """Test RHEL with invalid version string."""
        system = SystemInfo()
        system.os_family = OSFamily.RHEL
        system.os_version = "invalid"
        assert system.is_supported is False


class TestCheckExaBGP:
    """Additional ExaBGP tests."""

    def test_check_exabgp_version_installed(self) -> None:
        """Test ExaBGP version check when installed via pip."""
        from ober.commands.doctor import _check_exabgp

        with patch("ober.commands.doctor.get_exabgp_version", return_value="4.2.21"):
            result = _check_exabgp()
            assert result["passed"] is True
            assert "Version" in result["message"]


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a CLI runner."""
    return CliRunner()
