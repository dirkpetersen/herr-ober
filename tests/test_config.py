#!/usr/bin/env python3
"""Tests for ober.config module."""

from pathlib import Path
from unittest.mock import patch

from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    OberConfig,
    VIPConfig,
    get_secrets_path,
    load_secrets,
    save_secrets,
)


class TestBGPConfig:
    """Tests for BGPConfig dataclass."""

    def test_default_values(self) -> None:
        """Test BGPConfig default values."""
        config = BGPConfig()
        assert config.local_as == 65001
        assert config.peer_as == 65000
        assert config.neighbors == []
        assert config.hold_time == 3
        assert config.bfd_enabled is True

    def test_custom_values(self) -> None:
        """Test BGPConfig with custom values."""
        config = BGPConfig(
            local_as=65100,
            peer_as=65200,
            neighbors=["10.0.0.1"],
            hold_time=5,
            bfd_enabled=False,
        )
        assert config.local_as == 65100
        assert config.peer_as == 65200
        assert config.neighbors == ["10.0.0.1"]
        assert config.hold_time == 5
        assert config.bfd_enabled is False


class TestVIPConfig:
    """Tests for VIPConfig dataclass."""

    def test_default_values(self) -> None:
        """Test VIPConfig default values."""
        config = VIPConfig()
        assert config.address == ""
        assert config.interface == "lo-vip"

    def test_custom_values(self) -> None:
        """Test VIPConfig with custom values."""
        config = VIPConfig(address="10.0.100.1/32", interface="vip0")
        assert config.address == "10.0.100.1/32"
        assert config.interface == "vip0"


class TestBackendConfig:
    """Tests for BackendConfig dataclass."""

    def test_default_values(self) -> None:
        """Test BackendConfig default values."""
        config = BackendConfig()
        assert config.name == ""
        assert config.servers == []
        assert config.health_check_path == "/"
        assert config.health_check_interval == 1000

    def test_custom_values(self) -> None:
        """Test BackendConfig with custom values."""
        config = BackendConfig(
            name="s3_backend",
            servers=["rgw1:7480", "rgw2:7480"],
            health_check_path="/health",
            health_check_interval=500,
        )
        assert config.name == "s3_backend"
        assert len(config.servers) == 2
        assert config.health_check_path == "/health"
        assert config.health_check_interval == 500


class TestOberConfig:
    """Tests for OberConfig class."""

    def test_default_values(self) -> None:
        """Test OberConfig default values."""
        config = OberConfig()
        assert config.install_path == Path("/opt/ober")
        assert config.log_retention_days == 7
        assert config.stats_port == 8404

    def test_config_path(self) -> None:
        """Test config_path property."""
        config = OberConfig(install_path=Path("/custom/path"))
        assert config.config_path == Path("/custom/path/etc/ober.yaml")

    def test_haproxy_config_path(self) -> None:
        """Test haproxy_config_path property."""
        config = OberConfig(install_path=Path("/custom/path"))
        assert config.haproxy_config_path == Path("/custom/path/etc/haproxy/haproxy.cfg")

    def test_bgp_config_path(self) -> None:
        """Test bgp_config_path property."""
        config = OberConfig(install_path=Path("/custom/path"))
        assert config.bgp_config_path == Path("/custom/path/etc/bgp/config.ini")

    def test_ensure_directories(self, temp_dir: Path) -> None:
        """Test ensure_directories creates all required directories."""
        config = OberConfig(install_path=temp_dir)
        config.ensure_directories()

        assert (temp_dir / "etc" / "haproxy").exists()
        assert (temp_dir / "etc" / "bgp").exists()
        assert (temp_dir / "etc" / "certs").exists()
        assert (temp_dir / "bin").exists()
        assert (temp_dir / "venv").exists()

    def test_save_and_load(self, temp_dir: Path) -> None:
        """Test saving and loading configuration."""
        config = OberConfig(install_path=temp_dir)
        config.bgp = BGPConfig(local_as=65100, neighbors=["10.0.0.1"])
        config.vips = [VIPConfig(address="10.0.100.1/32")]
        config.backends = [BackendConfig(name="s3", servers=["rgw:7480"])]
        config.certs = CertConfig(path="/path/to/cert.pem")
        config.log_retention_days = 14
        config.stats_port = 9000

        config.ensure_directories()
        config.save()

        # Load the config
        loaded = OberConfig.load(config.config_path)

        assert loaded.bgp.local_as == 65100
        assert loaded.bgp.neighbors == ["10.0.0.1"]
        assert len(loaded.vips) == 1
        assert loaded.vips[0].address == "10.0.100.1/32"
        assert len(loaded.backends) == 1
        assert loaded.backends[0].name == "s3"
        assert loaded.certs.path == "/path/to/cert.pem"
        assert loaded.log_retention_days == 14
        assert loaded.stats_port == 9000

    def test_load_nonexistent(self) -> None:
        """Test loading when config doesn't exist."""
        config = OberConfig.load(Path("/nonexistent/path/config.yaml"))
        # Should return default config
        assert config.bgp.local_as == 65001


class TestSecrets:
    """Tests for secrets management."""

    def test_get_secrets_path(self) -> None:
        """Test get_secrets_path returns correct path."""
        path = get_secrets_path()
        assert path == Path.home() / ".ober" / "login"

    def test_save_and_load_secrets(self, temp_dir: Path) -> None:
        """Test saving and loading secrets."""
        secrets_path = temp_dir / "login"

        with patch("ober.config.get_secrets_path", return_value=secrets_path):
            # Save secrets
            secrets = {"BGP_PASSWORD": "secret123", "API_KEY": "key456"}
            save_secrets(secrets)

            # Check file permissions
            assert (secrets_path.stat().st_mode & 0o777) == 0o600

            # Load secrets
            loaded = load_secrets()
            assert loaded["BGP_PASSWORD"] == "secret123"
            assert loaded["API_KEY"] == "key456"

    def test_load_empty_secrets(self) -> None:
        """Test loading secrets when file doesn't exist."""
        with patch("ober.config.get_secrets_path", return_value=Path("/nonexistent")):
            secrets = load_secrets()
            assert secrets == {}
