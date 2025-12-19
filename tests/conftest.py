#!/usr/bin/env python3
"""Pytest configuration and fixtures."""

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.config import OberConfig


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_config(temp_dir: Path) -> OberConfig:
    """Create a temporary Ober configuration."""
    config = OberConfig(install_path=temp_dir)
    config.ensure_directories()
    return config


@pytest.fixture
def mock_system_info() -> Generator[MagicMock, None, None]:
    """Mock SystemInfo to avoid actual system detection."""
    with patch("ober.system.SystemInfo") as mock:
        instance = mock.return_value
        instance.os_family.value = "debian"
        instance.os_name = "Ubuntu"
        instance.os_version = "24.04"
        instance.os_codename = "noble"
        instance.python_version = "3.12.3"
        instance.is_root = False
        instance.hostname = "test-host"
        instance.arch = "x86_64"
        instance.is_supported = True
        instance.package_manager = "apt"
        instance.check_python_version.return_value = True
        instance.get_local_ip.return_value = "10.0.0.1"
        yield instance


@pytest.fixture
def mock_root_system_info(mock_system_info: MagicMock) -> MagicMock:
    """Mock SystemInfo with root access."""
    mock_system_info.is_root = True
    return mock_system_info


@pytest.fixture
def mock_run_command() -> Generator[MagicMock, None, None]:
    """Mock run_command to avoid actual system calls."""
    with patch("ober.system.run_command") as mock:
        mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield mock


@pytest.fixture
def mock_check_command_exists() -> Generator[MagicMock, None, None]:
    """Mock check_command_exists."""
    with patch("ober.system.check_command_exists") as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def sample_config(temp_config: OberConfig) -> OberConfig:
    """Create a sample configuration with values."""
    from ober.config import BackendConfig, BGPConfig, CertConfig, VIPConfig

    temp_config.bgp = BGPConfig(
        local_as=65001,
        peer_as=65000,
        neighbors=["10.0.0.1", "10.0.0.2"],
        router_id="10.0.1.1",
        local_address="10.0.1.1",
        hold_time=3,
        bfd_enabled=True,
    )
    temp_config.vips = [
        VIPConfig(address="10.0.100.1/32"),
        VIPConfig(address="10.0.100.2/32"),
    ]
    temp_config.backends = [
        BackendConfig(
            name="s3_backend",
            servers=["rgw1:7480", "rgw2:7480"],
            health_check_path="/",
            health_check_interval=1000,
        ),
    ]
    temp_config.certs = CertConfig(path="/opt/ober/etc/certs/server.pem")
    temp_config.log_retention_days = 7
    temp_config.stats_port = 8404

    return temp_config
