#!/usr/bin/env python3
"""Tests for ober.cli module."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from ober import __version__
from ober.cli import main


class TestMainCLI:
    """Tests for main CLI entry point."""

    def test_version(self, cli_runner: CliRunner) -> None:
        """Test --version flag."""
        result = cli_runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self, cli_runner: CliRunner) -> None:
        """Test --help flag."""
        result = cli_runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Ober - High-performance S3 ingress controller" in result.output

    def test_json_flag(self, cli_runner: CliRunner) -> None:
        """Test --json flag is accepted."""
        result = cli_runner.invoke(main, ["--json", "--help"])
        assert result.exit_code == 0

    def test_quiet_flag(self, cli_runner: CliRunner) -> None:
        """Test -q/--quiet flag is accepted."""
        result = cli_runner.invoke(main, ["-q", "--help"])
        assert result.exit_code == 0

    def test_verbose_flag(self, cli_runner: CliRunner) -> None:
        """Test -v/--verbose flag is accepted."""
        result = cli_runner.invoke(main, ["-v", "--help"])
        assert result.exit_code == 0


class TestDoctorCommand:
    """Tests for doctor command."""

    def test_doctor_help(self, cli_runner: CliRunner) -> None:
        """Test doctor --help."""
        result = cli_runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "diagnostic" in result.output.lower()

    def test_doctor_runs(self, cli_runner: CliRunner) -> None:
        """Test doctor command runs."""
        # Mock system info to avoid real system detection
        with patch("ober.commands.doctor.SystemInfo") as mock_sys:
            mock_instance = MagicMock()
            mock_instance.os_family.value = "debian"
            mock_instance.os_name = "Ubuntu"
            mock_instance.os_version = "24.04"
            mock_instance.python_version = "3.12.3"
            mock_instance.is_root = False
            mock_instance.is_supported = True
            mock_instance.hostname = "test"
            mock_instance.arch = "x86_64"
            mock_instance.check_python_version.return_value = True
            mock_sys.return_value = mock_instance

            result = cli_runner.invoke(main, ["doctor"])
            # Exit code 1 because not running as root
            assert "Operating System" in result.output

    def test_doctor_json_output(self, cli_runner: CliRunner) -> None:
        """Test doctor with JSON output."""
        with patch("ober.commands.doctor.SystemInfo") as mock_sys:
            mock_instance = MagicMock()
            mock_instance.os_family.value = "debian"
            mock_instance.os_name = "Ubuntu"
            mock_instance.os_version = "24.04"
            mock_instance.python_version = "3.12.3"
            mock_instance.is_root = False
            mock_instance.is_supported = True
            mock_instance.hostname = "test"
            mock_instance.arch = "x86_64"
            mock_instance.check_python_version.return_value = True
            mock_sys.return_value = mock_instance

            result = cli_runner.invoke(main, ["--json", "doctor"])
            assert '"checks"' in result.output
            assert '"system"' in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_help(self, cli_runner: CliRunner) -> None:
        """Test status --help."""
        result = cli_runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output.lower()

    def test_status_runs(self, cli_runner: CliRunner) -> None:
        """Test status command runs."""
        with patch("ober.commands.status.ServiceInfo") as mock_service:
            mock_instance = MagicMock()
            mock_instance.is_active = False
            mock_instance.is_enabled = False
            mock_instance.status = "inactive"
            mock_instance.pid = None
            mock_service.from_service_name.return_value = mock_instance

            result = cli_runner.invoke(main, ["status"])
            assert result.exit_code == 0
            assert "Services" in result.output


class TestLogsCommand:
    """Tests for logs command."""

    def test_logs_help(self, cli_runner: CliRunner) -> None:
        """Test logs --help."""
        result = cli_runner.invoke(main, ["logs", "--help"])
        assert result.exit_code == 0
        assert "-f" in result.output or "--follow" in result.output

    def test_logs_service_option(self, cli_runner: CliRunner) -> None:
        """Test logs --service option."""
        result = cli_runner.invoke(main, ["logs", "--help"])
        assert "--service" in result.output


class TestBootstrapCommand:
    """Tests for bootstrap command."""

    def test_bootstrap_help(self, cli_runner: CliRunner) -> None:
        """Test bootstrap --help."""
        result = cli_runner.invoke(main, ["bootstrap", "--help"])
        assert result.exit_code == 0
        assert "Bootstrap" in result.output

    def test_bootstrap_requires_root(self, cli_runner: CliRunner) -> None:
        """Test bootstrap requires root access."""
        with patch("ober.commands.bootstrap.SystemInfo") as mock_sys:
            mock_instance = MagicMock()
            mock_instance.is_root = False
            mock_instance.is_supported = True
            mock_sys.return_value = mock_instance

            # Mock the parent context
            result = cli_runner.invoke(main, ["bootstrap"])
            assert result.exit_code == 1
            assert "root" in result.output.lower()


class TestConfigCommand:
    """Tests for config command."""

    def test_config_help(self, cli_runner: CliRunner) -> None:
        """Test config --help."""
        result = cli_runner.invoke(main, ["config", "--help"])
        assert result.exit_code == 0
        assert "configuration" in result.output.lower() or "wizard" in result.output.lower()

    def test_config_dry_run_option(self, cli_runner: CliRunner) -> None:
        """Test config --dry-run option exists."""
        result = cli_runner.invoke(main, ["config", "--help"])
        assert "--dry-run" in result.output


class TestServiceCommands:
    """Tests for start/stop/restart commands."""

    def test_start_help(self, cli_runner: CliRunner) -> None:
        """Test start --help."""
        result = cli_runner.invoke(main, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start" in result.output

    def test_stop_help(self, cli_runner: CliRunner) -> None:
        """Test stop --help."""
        result = cli_runner.invoke(main, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop" in result.output
        assert "--force" in result.output

    def test_restart_help(self, cli_runner: CliRunner) -> None:
        """Test restart --help."""
        result = cli_runner.invoke(main, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart" in result.output
        assert "--reload-only" in result.output


class TestHealthCommand:
    """Tests for health command."""

    def test_health_help(self, cli_runner: CliRunner) -> None:
        """Test health --help."""
        result = cli_runner.invoke(main, ["health", "--help"])
        assert result.exit_code == 0
        assert "health" in result.output.lower()


class TestSyncCommand:
    """Tests for sync command."""

    def test_sync_help(self, cli_runner: CliRunner) -> None:
        """Test sync --help."""
        result = cli_runner.invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "--routers" in result.output
        assert "--frontend-http" in result.output
        assert "--backend-http" in result.output


class TestTestCommand:
    """Tests for test command."""

    def test_test_help(self, cli_runner: CliRunner) -> None:
        """Test test --help."""
        result = cli_runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "BGP" in result.output or "connectivity" in result.output.lower()


class TestUninstallCommand:
    """Tests for uninstall command."""

    def test_uninstall_help(self, cli_runner: CliRunner) -> None:
        """Test uninstall --help."""
        result = cli_runner.invoke(main, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "--yes" in result.output or "-y" in result.output
        assert "--keep-config" in result.output


class TestUpgradeCommand:
    """Tests for upgrade command."""

    def test_upgrade_help(self, cli_runner: CliRunner) -> None:
        """Test upgrade --help."""
        result = cli_runner.invoke(main, ["upgrade", "--help"])
        assert result.exit_code == 0
        assert "--check-only" in result.output
