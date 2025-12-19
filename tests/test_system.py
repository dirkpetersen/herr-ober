#!/usr/bin/env python3
"""Tests for ober.system module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from ober.system import (
    OSFamily,
    ServiceInfo,
    SystemInfo,
    check_command_exists,
    get_exabgp_version,
    get_haproxy_version,
    run_command,
)


class TestSystemInfo:
    """Tests for SystemInfo class."""

    def test_init_detects_os(self) -> None:
        """Test that SystemInfo detects OS on initialization."""
        with patch.object(SystemInfo, "_detect_os") as mock_detect:
            SystemInfo()
            mock_detect.assert_called_once()

    def test_parse_os_release_ubuntu(self, tmp_path: Path) -> None:
        """Test parsing Ubuntu os-release file."""
        os_release = tmp_path / "os-release"
        os_release.write_text('ID=ubuntu\nID_LIKE=debian\nNAME="Ubuntu"\nVERSION_ID="24.04"\n')

        with patch("ober.system.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=os_release.read_text())):
                info = SystemInfo()
                info._detect_os()
                assert info.os_family == OSFamily.DEBIAN

    def test_parse_os_release_rhel(self) -> None:
        """Test parsing RHEL os-release file."""
        content = 'ID=rhel\nNAME="Red Hat Enterprise Linux"\nVERSION_ID="10.0"\n'

        with (
            patch("builtins.open", mock_open(read_data=content)),
            patch.object(Path, "exists", return_value=True),
        ):
            info = SystemInfo()
            # Manually call detection with our mock
            result = info._parse_os_release()
            assert result.get("ID") == "rhel"

    def test_is_supported_debian(self) -> None:
        """Test that Debian/Ubuntu is supported."""
        info = SystemInfo()
        info.os_family = OSFamily.DEBIAN
        assert info.is_supported is True

    def test_is_supported_rhel_10(self) -> None:
        """Test that RHEL 10+ is supported."""
        info = SystemInfo()
        info.os_family = OSFamily.RHEL
        info.os_version = "10.0"
        assert info.is_supported is True

    def test_is_not_supported_rhel_9(self) -> None:
        """Test that RHEL 9 is not supported."""
        info = SystemInfo()
        info.os_family = OSFamily.RHEL
        info.os_version = "9.3"
        assert info.is_supported is False

    def test_is_not_supported_unknown(self) -> None:
        """Test that unknown OS is not supported."""
        info = SystemInfo()
        info.os_family = OSFamily.UNKNOWN
        assert info.is_supported is False

    def test_package_manager_debian(self) -> None:
        """Test package manager detection for Debian."""
        info = SystemInfo()
        info.os_family = OSFamily.DEBIAN
        assert info.package_manager == "apt"

    def test_package_manager_rhel(self) -> None:
        """Test package manager detection for RHEL."""
        info = SystemInfo()
        info.os_family = OSFamily.RHEL
        assert info.package_manager == "dnf"

    def test_check_python_version_ok(self) -> None:
        """Test Python version check passes for 3.12+."""
        info = SystemInfo()
        assert info.check_python_version((3, 12)) is True

    def test_get_local_ip(self) -> None:
        """Test getting local IP address."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.1.5 uid 0"

        with patch("subprocess.run", return_value=mock_result):
            info = SystemInfo()
            ip = info.get_local_ip()
            assert ip == "10.0.1.5"


class TestServiceInfo:
    """Tests for ServiceInfo class."""

    def test_from_service_name(self) -> None:
        """Test creating ServiceInfo from service name."""
        mock_active = MagicMock(returncode=0, stdout="active\n")
        mock_enabled = MagicMock(returncode=0, stdout="enabled\n")
        mock_pid = MagicMock(returncode=0, stdout="1234\n")

        with patch("subprocess.run", side_effect=[mock_active, mock_enabled, mock_pid]):
            info = ServiceInfo.from_service_name("test-service")
            assert info.name == "test-service"
            assert info.is_active is True
            assert info.is_enabled is True
            assert info.pid == 1234

    def test_inactive_service(self) -> None:
        """Test ServiceInfo for inactive service."""
        mock_inactive = MagicMock(returncode=3, stdout="inactive\n")
        mock_disabled = MagicMock(returncode=1, stdout="disabled\n")

        with patch("subprocess.run", side_effect=[mock_inactive, mock_disabled]):
            info = ServiceInfo.from_service_name("test-service")
            assert info.is_active is False
            assert info.is_enabled is False


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_check_command_exists_true(self) -> None:
        """Test check_command_exists returns True for existing command."""
        with patch("shutil.which", return_value="/usr/bin/python3"):
            assert check_command_exists("python3") is True

    def test_check_command_exists_false(self) -> None:
        """Test check_command_exists returns False for missing command."""
        with patch("shutil.which", return_value=None):
            assert check_command_exists("nonexistent") is False

    def test_get_haproxy_version(self) -> None:
        """Test getting HAProxy version."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "HAProxy version 3.3.1 2024/01/01"

        with patch("subprocess.run", return_value=mock_result):
            version = get_haproxy_version()
            assert version == "3.3.1"

    def test_get_haproxy_version_not_installed(self) -> None:
        """Test HAProxy version when not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            version = get_haproxy_version()
            assert version is None

    def test_get_exabgp_version(self) -> None:
        """Test getting ExaBGP version."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = "ExaBGP 4.2.21"

        with patch("subprocess.run", return_value=mock_result):
            version = get_exabgp_version()
            assert version == "4.2.21"

    def test_run_command_success(self) -> None:
        """Test run_command with successful command."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"

        with patch("subprocess.run", return_value=mock_result):
            result = run_command(["echo", "test"])
            assert result.returncode == 0

    def test_run_command_failure(self) -> None:
        """Test run_command with failing command."""
        with (
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd")),
            pytest.raises(subprocess.CalledProcessError),
        ):
            run_command(["false"], check=True)
