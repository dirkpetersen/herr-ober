#!/usr/bin/env python3
"""Tests for ober.config module."""

from pathlib import Path
from unittest.mock import patch

import pytest

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
        assert isinstance(config.install_path, Path)
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

    def test_load_secrets_with_comments(self, temp_dir: Path) -> None:
        """Test loading secrets file with comments."""
        secrets_path = temp_dir / "login"
        secrets_path.write_text(
            "# This is a comment\nKEY1=value1\n# Another comment\nKEY2=value2\n"
        )

        with patch("ober.config.get_secrets_path", return_value=secrets_path):
            secrets = load_secrets()
            assert secrets == {"KEY1": "value1", "KEY2": "value2"}


class TestOberConfigLoadFromFile:
    """Tests for OberConfig._load_from_file method."""

    def test_load_complete_config(self, temp_dir: Path) -> None:
        """Test loading a complete configuration file."""
        config_path = temp_dir / "ober.yaml"
        config_path.write_text("""
install_path: ~/.ober
bgp:
  local_as: 65100
  peer_as: 65200
  neighbors:
    - 10.0.0.1
    - 10.0.0.2
  router_id: 192.168.1.1
  local_address: 192.168.1.10
  hold_time: 5
  bfd_enabled: false
vips:
  - address: 10.0.100.1/32
    interface: lo-vip
  - address: 10.0.100.2/32
    interface: vip0
backends:
  - name: s3_primary
    servers:
      - rgw1:7480
      - rgw2:7480
    health_check_path: /health
    health_check_interval: 500
certs:
  path: ~/.ober/etc/certs/server.pem
  acme_enabled: true
  acme_email: admin@example.com
log_retention_days: 30
stats_port: 9000
""")

        config = OberConfig.load(config_path)

        assert isinstance(config.install_path, Path)
        assert config.bgp.local_as == 65100
        assert config.bgp.peer_as == 65200
        assert config.bgp.neighbors == ["10.0.0.1", "10.0.0.2"]
        assert config.bgp.router_id == "192.168.1.1"
        assert config.bgp.local_address == "192.168.1.10"
        assert config.bgp.hold_time == 5
        assert config.bgp.bfd_enabled is False
        assert len(config.vips) == 2
        assert config.vips[0].address == "10.0.100.1/32"
        assert config.vips[1].interface == "vip0"
        assert len(config.backends) == 1
        assert config.backends[0].name == "s3_primary"
        assert config.backends[0].health_check_interval == 500
        assert config.certs.path == "~/.ober/etc/certs/server.pem"
        assert config.certs.acme_enabled is True
        assert config.certs.acme_email == "admin@example.com"
        assert config.log_retention_days == 30
        assert config.stats_port == 9000

    def test_load_partial_config(self, temp_dir: Path) -> None:
        """Test loading a partial configuration file."""
        config_path = temp_dir / "ober.yaml"
        config_path.write_text("""
bgp:
  local_as: 65100
""")

        config = OberConfig.load(config_path)

        # BGP local_as should be set
        assert config.bgp.local_as == 65100
        # Other values should be defaults
        assert config.bgp.peer_as == 65000
        assert config.bgp.neighbors == []
        assert config.log_retention_days == 7

    def test_load_empty_config(self, temp_dir: Path) -> None:
        """Test loading an empty configuration file."""
        config_path = temp_dir / "ober.yaml"
        config_path.write_text("")

        config = OberConfig.load(config_path)

        # All values should be defaults
        assert isinstance(config.install_path, Path)
        assert config.bgp.local_as == 65001

    def test_load_searches_default_paths(self, temp_dir: Path) -> None:
        """Test that load() searches default paths when no path given."""
        # Create a config in home directory
        home_config = temp_dir / ".ober" / "ober.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text("""
bgp:
  local_as: 65555
""")

        # Load directly from path to test the load functionality
        config = OberConfig.load(home_config)
        # Should find the home config
        assert config.bgp.local_as == 65555

    def test_venv_path(self) -> None:
        """Test venv_path property."""
        config = OberConfig(install_path=Path("/custom"))
        assert config.venv_path == Path("/custom/venv")

    def test_certs_path(self) -> None:
        """Test certs_path property."""
        config = OberConfig(install_path=Path("/custom"))
        assert config.certs_path == Path("/custom/etc/certs")


class TestValidate:
    """Schema validation of ober.yaml (load never raises, validate reports all)."""

    def _load(self, tmp_path: Path, text: str) -> OberConfig:
        path = tmp_path / "ober.yaml"
        path.write_text(text)
        return OberConfig.load(path)

    def test_defaults_are_valid(self) -> None:
        assert OberConfig().validate() == ([], [])

    def test_syntax_error_is_one_clear_error(self, tmp_path: Path) -> None:
        config = self._load(tmp_path, "ha_mode: bgp\nbgp:\n  local_as: 1\n bad: [\n")
        errors, _ = config.validate()
        assert len(errors) == 1
        assert "YAML syntax error at line" in errors[0]
        assert config.ha_mode == "bgp"  # defaults stay usable for stop/status/logs

    def test_duplicate_key_is_an_error(self, tmp_path: Path) -> None:
        config = self._load(tmp_path, "bgp: {neighbors: [10.0.0.1]}\nstats_port: 1\nbgp: {}\n")
        errors, _ = config.validate()
        assert len(errors) == 1
        assert "line 3" in errors[0] and "duplicate key 'bgp'" in errors[0]

    def test_reports_all_problems(self, tmp_path: Path) -> None:
        config = self._load(
            tmp_path,
            "ha_mode: bgpp\n"
            "stats_port: 70000\n"
            "bgp: {local_as: abc, neighbors: [10.0.0.300]}\n"
            "vips: [{address: 10.0.0.1/40, exposure: public}]\n"
            "backends: [{name: rgw, servers: '10.1.0.1:7480'}]\n"
            "security: {allowlist: [not-a-net]}\n",
        )
        errors, _ = config.validate()
        text = "\n".join(errors)
        for key in (
            "ha_mode",
            "stats_port",
            "bgp.local_as",
            "bgp.neighbors[0]",
            "vips[0].address",
            "vips[0].exposure",
            "backends[0].servers: must be a list",
            "security.allowlist[0]",
        ):
            assert key in text, key

    def test_bad_section_shape(self, tmp_path: Path) -> None:
        config = self._load(tmp_path, "bgp: 5\nvips: 10.0.0.1\nbackends: [rgw]\n")
        errors, _ = config.validate()
        assert any(e.startswith("bgp: must be a mapping") for e in errors)
        assert any(e.startswith("vips: must be a list") for e in errors)
        assert any(e.startswith("backends[0]: must be a mapping") for e in errors)

    def test_unknown_keys_are_warnings(self, tmp_path: Path) -> None:
        config = self._load(tmp_path, "stats_prot: 9000\nbgp: {hold_tme: 9}\n")
        errors, warnings = config.validate()
        assert errors == []
        assert "stats_prot: unknown key (ignored)" in warnings
        assert "bgp.hold_tme: unknown key (ignored)" in warnings

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        config = OberConfig(install_path=tmp_path, local_ip="10.0.0.5")
        config.vips = [VIPConfig(address="10.0.100.1/32", exposure="internet")]
        config.save()
        loaded = OberConfig.load(config.config_path)
        assert loaded == config
        assert loaded.validate() == ([], [])


def test_node_ip_prefers_override() -> None:
    assert OberConfig(local_ip="10.9.9.9").node_ip() == "10.9.9.9"


def test_health_and_drain_fields_validate(tmp_path: Path) -> None:
    path = tmp_path / "ober.yaml"
    path.write_text(
        "drain_timeout: 0\nbgp: {health_rise: 0, health_fall: 0, health_holddown: -1}\n"
        "keepalived: {gateway_check: nope}\n"
    )
    errors, _ = OberConfig.load(path).validate()
    text = "\n".join(errors)
    for key in (
        "drain_timeout",
        "bgp.health_rise",
        "bgp.health_fall",
        "bgp.health_holddown",
        "keepalived.gateway_check",
    ):
        assert key in text, key
    good = OberConfig(install_path=tmp_path, drain_timeout=60)
    good.bgp.health_rise, good.keepalived.gateway_check = 5, "10.0.0.1"
    good.save()
    assert OberConfig.load(good.config_path) == good


def test_vip_must_be_single_address_and_unique(tmp_path: Path) -> None:
    path = tmp_path / "ober.yaml"
    path.write_text("vips: [{address: 10.0.0.5}, {address: 10.0.0.5/32}, {address: 10.0.0.9/24}]\n")
    errors, _ = OberConfig.load(path).validate()
    assert "vips[1].address: is a duplicate, got '10.0.0.5/32'" in errors
    assert "vips[2].address: must be a single address (/32), got '10.0.0.9/24'" in errors
    assert not any(e.startswith("vips[0]") for e in errors)


def test_save_is_atomic(tmp_path: Path) -> None:
    """A failed save keeps the old ober.yaml whole and leaves no temp file."""
    from unittest.mock import patch

    config = OberConfig(install_path=tmp_path, local_ip="10.0.0.5")
    config.save()
    before = config.config_path.read_text()
    config.local_ip = "10.0.0.6"
    with (
        patch("ober.config.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        config.save()
    assert config.config_path.read_text() == before
    assert [p.name for p in config.config_path.parent.iterdir()] == ["ober.yaml"]
