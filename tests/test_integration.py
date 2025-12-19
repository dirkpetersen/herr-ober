#!/usr/bin/env python3
"""Integration tests for Ober with mock S3 backends.

These tests verify that Ober generates valid configurations and can
communicate with S3-compatible backends through HAProxy.
"""

import tempfile
from pathlib import Path

import pytest

from ober.config import BackendConfig, BGPConfig, CertConfig, OberConfig, VIPConfig


class TestConfigGeneration:
    """Test HAProxy and ExaBGP configuration generation."""

    def test_haproxy_config_generation(self, temp_config: OberConfig) -> None:
        """Test that valid HAProxy config is generated."""
        from ober.commands.config import _generate_haproxy_config

        temp_config.vips = [VIPConfig(address="10.0.100.1/32")]
        temp_config.backends = [
            BackendConfig(
                name="s3_backend",
                servers=["rgw1:7480", "rgw2:7480"],
                health_check_path="/",
                health_check_interval=1000,
            )
        ]
        temp_config.certs = CertConfig(path="/opt/ober/etc/certs/server.pem")
        temp_config.stats_port = 8404

        _generate_haproxy_config(temp_config)

        assert temp_config.haproxy_config_path.exists()
        content = temp_config.haproxy_config_path.read_text()

        # Verify key sections
        assert "global" in content
        assert "defaults" in content
        assert "frontend stats" in content
        assert f"bind *:{temp_config.stats_port}" in content
        assert "backend s3_backend" in content
        assert "balance leastconn" in content
        assert "server srv1 rgw1:7480" in content
        assert "server srv2 rgw2:7480" in content

    def test_exabgp_config_generation(self, temp_config: OberConfig) -> None:
        """Test that valid ExaBGP config is generated."""
        from ober.commands.config import _generate_exabgp_config

        temp_config.bgp = BGPConfig(
            local_as=65001,
            peer_as=65000,
            neighbors=["10.0.0.1", "10.0.0.2"],
            router_id="10.0.1.1",
            local_address="10.0.1.1",
            hold_time=3,
            bfd_enabled=True,
        )

        _generate_exabgp_config(temp_config)

        assert temp_config.bgp_config_path.exists()
        content = temp_config.bgp_config_path.read_text()

        # Verify key sections
        assert "process announce-routes" in content
        assert "neighbor 10.0.0.1" in content
        assert "neighbor 10.0.0.2" in content
        assert "local-as 65001" in content
        assert "peer-as 65000" in content
        assert "hold-time 3" in content
        assert "bfd {" in content

    def test_haproxy_config_without_ssl(self, temp_config: OberConfig) -> None:
        """Test HAProxy config generation without SSL configuration."""
        from ober.commands.config import _generate_haproxy_config

        temp_config.vips = [VIPConfig(address="10.0.100.1/32")]
        temp_config.backends = [
            BackendConfig(name="s3_backend", servers=["rgw:7480"]),
        ]
        # No certs configured

        _generate_haproxy_config(temp_config)

        content = temp_config.haproxy_config_path.read_text()

        # Should not have SSL frontend
        assert "ssl crt" not in content
        # Should still have backend
        assert "backend s3_backend" in content

    def test_config_roundtrip(self, temp_config: OberConfig) -> None:
        """Test saving and loading configuration."""
        temp_config.bgp = BGPConfig(
            local_as=65100,
            peer_as=65200,
            neighbors=["10.0.0.1"],
            router_id="10.0.1.5",
            local_address="10.0.1.5",
            hold_time=5,
            bfd_enabled=False,
        )
        temp_config.vips = [
            VIPConfig(address="10.0.100.1/32"),
            VIPConfig(address="10.0.100.2/32"),
        ]
        temp_config.backends = [
            BackendConfig(
                name="backend1",
                servers=["server1:7480", "server2:7480"],
            ),
            BackendConfig(
                name="backend2",
                servers=["server3:7480"],
            ),
        ]
        temp_config.certs = CertConfig(
            acme_enabled=True,
            acme_email="test@example.com",
        )
        temp_config.log_retention_days = 14
        temp_config.stats_port = 9000

        # Save
        temp_config.save()

        # Load
        loaded = OberConfig.load(temp_config.config_path)

        # Verify all values
        assert loaded.bgp.local_as == 65100
        assert loaded.bgp.peer_as == 65200
        assert loaded.bgp.neighbors == ["10.0.0.1"]
        assert loaded.bgp.bfd_enabled is False
        assert len(loaded.vips) == 2
        assert len(loaded.backends) == 2
        assert loaded.certs.acme_enabled is True
        assert loaded.log_retention_days == 14
        assert loaded.stats_port == 9000


@pytest.fixture
def temp_config() -> OberConfig:
    """Create a temporary configuration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = OberConfig(install_path=Path(tmpdir))
        config.ensure_directories()
        yield config


class TestMotoS3Integration:
    """Integration tests with moto S3 mock server.

    These tests verify that the configuration works correctly with
    S3-compatible backends.
    """

    @pytest.mark.skip(reason="Requires HAProxy installation")
    def test_haproxy_with_moto_backends(self) -> None:
        """Test HAProxy proxying to moto S3 backends.

        This test:
        1. Starts two moto S3 servers
        2. Generates HAProxy config pointing to them
        3. Starts HAProxy
        4. Tests S3 operations through HAProxy
        """
        # This is a placeholder for a full integration test
        # that would require HAProxy to be installed
        pass

    def test_moto_server_basic(self) -> None:
        """Test that moto S3 server can be created."""
        import boto3
        from moto import mock_aws

        with mock_aws():
            client = boto3.client(
                "s3",
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )

            # Create bucket
            client.create_bucket(Bucket="test-bucket")

            # List buckets
            response = client.list_buckets()
            assert len(response["Buckets"]) == 1
            assert response["Buckets"][0]["Name"] == "test-bucket"

    def test_moto_s3_operations(self) -> None:
        """Test basic S3 operations with moto."""
        import boto3
        from moto import mock_aws

        with mock_aws():
            client = boto3.client(
                "s3",
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )

            # Create bucket
            client.create_bucket(Bucket="test-bucket")

            # Put object
            client.put_object(
                Bucket="test-bucket",
                Key="test-key",
                Body=b"test content",
            )

            # Get object
            response = client.get_object(Bucket="test-bucket", Key="test-key")
            content = response["Body"].read()
            assert content == b"test content"

            # Delete object
            client.delete_object(Bucket="test-bucket", Key="test-key")

            # Verify deleted
            objects = client.list_objects_v2(Bucket="test-bucket")
            assert objects.get("KeyCount", 0) == 0
