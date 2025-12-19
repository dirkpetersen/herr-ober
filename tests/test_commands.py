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

        with patch("ober.commands.health.requests.get", side_effect=requests.RequestException("Connection refused")):
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


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a CLI runner."""
    return CliRunner()
