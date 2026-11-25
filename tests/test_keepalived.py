#!/usr/bin/env python3
"""Tests for keepalived-specific functionality."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands.config import (
    _detect_default_interface,
    _parse_hostlist,
    _validate_vip,
    get_vip_owner,
    get_vrrp_router_id,
)
from ober.config import KeepalivedConfig, OberConfig, VIPConfig
from ober.system import get_keepalived_version


class TestVIPValidation:
    """Tests for VIP address validation."""

    def test_validate_vip_valid_ip(self) -> None:
        """Test validation of valid IP without CIDR."""
        is_valid, error = _validate_vip("192.168.1.100")
        assert is_valid is True
        assert error == ""

    def test_validate_vip_valid_ip_with_cidr(self) -> None:
        """Test validation of valid IP with CIDR."""
        is_valid, error = _validate_vip("192.168.1.100/32")
        assert is_valid is True
        assert error == ""

    def test_validate_vip_rejects_cidr_24(self) -> None:
        """A /24 on lo-vip adds a connected route that blackholes replies."""
        is_valid, error = _validate_vip("10.0.0.1/24")
        assert is_valid is False
        assert "must be a single address (/32)" in error

    def test_validate_vip_invalid_ip(self) -> None:
        """Test validation of invalid IP address."""
        is_valid, error = _validate_vip("999.999.999.999")
        assert is_valid is False
        assert "Invalid IP address" in error

    def test_validate_vip_invalid_ip_format(self) -> None:
        """Test validation of malformed IP address."""
        is_valid, error = _validate_vip("not-an-ip")
        assert is_valid is False
        assert "Invalid IP address" in error

    def test_validate_vip_invalid_cidr_too_large(self) -> None:
        """Test validation of CIDR prefix > 32."""
        is_valid, error = _validate_vip("192.168.1.100/33")
        assert is_valid is False
        assert "Invalid CIDR prefix" in error

    def test_validate_vip_invalid_cidr_negative(self) -> None:
        """Test validation of negative CIDR prefix."""
        is_valid, error = _validate_vip("192.168.1.100/-1")
        assert is_valid is False
        assert "Invalid CIDR prefix" in error

    def test_validate_vip_invalid_cidr_non_numeric(self) -> None:
        """Test validation of non-numeric CIDR prefix."""
        is_valid, error = _validate_vip("192.168.1.100/abc")
        assert is_valid is False
        assert "Invalid CIDR prefix" in error

    def test_validate_vip_empty_ip(self) -> None:
        """Test validation of empty IP address."""
        is_valid, error = _validate_vip("")
        assert is_valid is False
        assert "Invalid IP address" in error


class TestVIPAssignment:
    """Tests for VIP assignment algorithm."""

    def test_get_vip_owner_consistent(self) -> None:
        """Test VIP owner assignment is consistent."""
        nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        vip = "192.168.1.100"

        # Call multiple times - should always return same owner
        owner1, priority1 = get_vip_owner(vip, nodes, "10.0.0.1")
        owner2, priority2 = get_vip_owner(vip, nodes, "10.0.0.1")

        assert owner1 == owner2
        assert priority1 == priority2

    def test_get_vip_owner_deterministic_across_nodes(self) -> None:
        """Test all nodes agree on VIP ownership."""
        nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        vip = "192.168.1.100"

        # Each node queries who owns the VIP
        owners = []
        for node in nodes:
            owner, _ = get_vip_owner(vip, nodes, node)
            owners.append(owner)

        # All nodes should agree on the same owner
        assert len(set(owners)) == 1

    def test_get_vip_owner_priority(self) -> None:
        """Test owner gets priority 100, others get 90."""
        nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        vip = "192.168.1.100"

        owner, owner_priority = get_vip_owner(vip, nodes, nodes[0])

        # Check owner's priority
        if owner == nodes[0]:
            assert owner_priority == 100
        else:
            assert owner_priority == 90

        # Check another node's priority for same VIP
        _, non_owner_priority = get_vip_owner(vip, nodes, "10.0.0.99")
        assert non_owner_priority == 90

    def test_get_vip_owner_distribution(self) -> None:
        """Test VIPs are distributed across nodes."""
        nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        vips = [f"192.168.1.{i}" for i in range(100, 110)]  # 10 VIPs

        ownership = dict.fromkeys(nodes, 0)
        for vip in vips:
            owner, _ = get_vip_owner(vip, nodes, nodes[0])
            ownership[owner] += 1

        # Each node should own at least one VIP (statistically likely with 10 VIPs and 3 nodes)
        # At minimum, no node should own all VIPs
        assert len([count for count in ownership.values() if count > 0]) >= 2

    def test_get_vip_owner_node_order_independent(self) -> None:
        """Test VIP ownership is independent of node list order."""
        nodes1 = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        nodes2 = ["10.0.0.3", "10.0.0.1", "10.0.0.2"]  # Different order
        vip = "192.168.1.100"

        owner1, _ = get_vip_owner(vip, nodes1, nodes1[0])
        owner2, _ = get_vip_owner(vip, nodes2, nodes2[0])

        # Should return same owner regardless of input order
        assert owner1 == owner2


class TestVRRPRouterID:
    """Tests for VRRP router ID generation."""

    def test_get_vrrp_router_id_range(self) -> None:
        """Test router ID is in valid range 1-255."""
        vips = [f"192.168.1.{i}" for i in range(1, 256)]
        for vip in vips:
            router_id = get_vrrp_router_id(vip)
            assert 1 <= router_id <= 255

    def test_get_vrrp_router_id_consistent(self) -> None:
        """Test router ID is consistent for same VIP."""
        vip = "192.168.1.100"
        id1 = get_vrrp_router_id(vip)
        id2 = get_vrrp_router_id(vip)
        assert id1 == id2

    def test_get_vrrp_router_id_different_vips(self) -> None:
        """Test different VIPs usually get different router IDs."""
        vips = [f"192.168.1.{i}" for i in range(100, 110)]
        router_ids = [get_vrrp_router_id(vip) for vip in vips]

        # Most should be unique (collisions possible but unlikely with 10 VIPs)
        assert len(set(router_ids)) >= 8


class TestHostlistParsing:
    """Tests for Slurm hostlist parsing."""

    def test_parse_hostlist_simple(self) -> None:
        """Test simple comma-separated list."""
        result = _parse_hostlist("host1,host2,host3")
        assert result == ["host1", "host2", "host3"]

    def test_parse_hostlist_range(self) -> None:
        """Test range expansion."""
        result = _parse_hostlist("node[01-03]")
        assert result == ["node01", "node02", "node03"]

    def test_parse_hostlist_ip_range(self) -> None:
        """Test IP address range expansion."""
        result = _parse_hostlist("10.0.0.[1-3]")
        assert result == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_parse_hostlist_mixed(self) -> None:
        """Test mixed format."""
        result = _parse_hostlist("host1,node[01-02],host2")
        assert result == ["host1", "node01", "node02", "host2"]

    def test_parse_hostlist_empty(self) -> None:
        """Test empty string."""
        result = _parse_hostlist("")
        assert result == []

    def test_parse_hostlist_zero_padding(self) -> None:
        """Test zero-padding preservation."""
        result = _parse_hostlist("node[001-003]")
        assert result == ["node001", "node002", "node003"]

    def test_parse_hostlist_no_range(self) -> None:
        """Test single host."""
        result = _parse_hostlist("10.0.0.1")
        assert result == ["10.0.0.1"]

    def test_parse_hostlist_whitespace(self) -> None:
        """Test whitespace handling."""
        result = _parse_hostlist(" host1 , host2 ")
        assert result == ["host1", "host2"]


class TestDefaultInterfaceDetection:
    """Tests for default interface detection."""

    def test_detect_default_interface_success(self) -> None:
        """Test successful interface detection."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "default via 10.0.0.1 dev eth0 proto dhcp metric 100"

        with patch("ober.system.run_command", return_value=mock_result):
            result = _detect_default_interface()
            assert result == "eth0"

    def test_detect_default_interface_ens_naming(self) -> None:
        """Test detection with modern interface naming (ens3)."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "default via 10.0.0.1 dev ens3 proto static"

        with patch("ober.system.run_command", return_value=mock_result):
            result = _detect_default_interface()
            assert result == "ens3"

    def test_detect_default_interface_fallback_to_link_show(self) -> None:
        """Test fallback to ip link show when default route fails."""
        # First call (ip route show default) fails
        route_result = MagicMock()
        route_result.returncode = 1
        route_result.stdout = ""

        # Second call (ip -o link show) succeeds
        link_result = MagicMock()
        link_result.returncode = 0
        link_result.stdout = "1: lo: <LOOPBACK,UP,LOWER_UP>\n2: eth0: <BROADCAST,MULTICAST,UP>\n"

        def mock_run_command(cmd, **_kwargs):
            if "route" in cmd:
                return route_result
            return link_result

        with patch("ober.system.run_command", side_effect=mock_run_command):
            result = _detect_default_interface()
            assert result == "eth0"

    def test_detect_default_interface_skips_virtual_interfaces(self) -> None:
        """Test that virtual interfaces are skipped in fallback."""
        # First call fails
        route_result = MagicMock()
        route_result.returncode = 1
        route_result.stdout = ""

        # Second call returns only virtual interfaces then a real one
        link_result = MagicMock()
        link_result.returncode = 0
        link_result.stdout = (
            "1: lo: <LOOPBACK>\n"
            "2: docker0: <BROADCAST>\n"
            "3: veth123: <BROADCAST>\n"
            "4: br-abc: <BROADCAST>\n"
            "5: ens192: <BROADCAST,MULTICAST,UP>\n"
        )

        def mock_run_command(cmd, **_kwargs):
            if "route" in cmd:
                return route_result
            return link_result

        with patch("ober.system.run_command", side_effect=mock_run_command):
            result = _detect_default_interface()
            assert result == "ens192"

    def test_detect_default_interface_exception(self) -> None:
        """Test fallback on exception."""
        with patch("ober.system.run_command", side_effect=Exception("error")):
            result = _detect_default_interface()
            # Should return final fallback
            assert result == "eth0"


class TestKeepalivedConfig:
    """Tests for KeepalivedConfig dataclass."""

    def test_keepalived_config_defaults(self) -> None:
        """Test KeepalivedConfig default values."""
        config = KeepalivedConfig()
        assert config.peers == []
        assert config.interface == ""
        assert config.use_multicast is False
        assert config.advert_int == 1

    def test_keepalived_config_custom(self) -> None:
        """Test KeepalivedConfig with custom values."""
        config = KeepalivedConfig(
            peers=["10.0.0.2", "10.0.0.3"],
            interface="eth0",
            use_multicast=True,
            advert_int=2,
        )
        assert config.peers == ["10.0.0.2", "10.0.0.3"]
        assert config.interface == "eth0"
        assert config.use_multicast is True
        assert config.advert_int == 2


class TestKeepalivedConfigGeneration:
    """Tests for keepalived config file generation."""

    def test_generate_keepalived_config_unicast(self, temp_dir: Path) -> None:
        """Test keepalived config generation in unicast mode."""
        from ober.commands.config import _generate_keepalived_config

        config = OberConfig(install_path=temp_dir)
        config.ha_mode = "keepalived"
        config.keepalived = KeepalivedConfig(
            peers=["10.0.0.2", "10.0.0.3"],
            interface="eth0",
            use_multicast=False,
            advert_int=1,
        )
        config.vips = [VIPConfig(address="192.168.1.100/32")]
        config.stats_port = 8404
        config.ensure_directories()

        # Need to patch SystemInfo at the module where it's imported
        from ober.system import SystemInfo as RealSystemInfo

        with patch("ober.commands.config.SystemInfo") as mock_system:
            mock_instance = RealSystemInfo()
            mock_instance.get_local_ip = lambda: "10.0.0.1"
            mock_instance.hostname = "node01"
            mock_system.return_value = mock_instance
            _generate_keepalived_config(config)

        assert config.keepalived_config_path.exists()
        content = config.keepalived_config_path.read_text()

        # Verify key sections
        assert "global_defs" in content
        assert "vrrp_script chk_haproxy" in content
        assert "vrrp_instance VI_1" in content
        assert "unicast_src_ip" in content  # Check for any IP
        # unicast_peer should contain both peers
        assert "unicast_peer" in content
        assert "10.0.0.2" in content
        assert "10.0.0.3" in content
        assert "192.168.1.100/32" in content
        # keepalived already goes to FAULT when its own interface is down
        assert "track_interface" not in content
        assert "enable_script_security" in content

    def test_generate_keepalived_config_multicast(self, temp_dir: Path) -> None:
        """Test keepalived config generation in multicast mode."""
        from ober.commands.config import _generate_keepalived_config

        config = OberConfig(install_path=temp_dir)
        config.ha_mode = "keepalived"
        config.keepalived = KeepalivedConfig(
            peers=["10.0.0.2"],
            interface="eth0",
            use_multicast=True,
            advert_int=1,
        )
        config.vips = [VIPConfig(address="192.168.1.100/32")]
        config.stats_port = 8404
        config.ensure_directories()

        with patch("ober.commands.config.SystemInfo") as mock_system:
            mock_system.return_value.get_local_ip.return_value = "10.0.0.1"
            mock_system.return_value.hostname = "node01"
            _generate_keepalived_config(config)

        assert config.keepalived_config_path.exists()
        content = config.keepalived_config_path.read_text()

        # Verify multicast mode (should NOT have unicast directives)
        assert "Multicast mode" in content
        assert "unicast_src_ip" not in content
        assert "unicast_peer" not in content

    def test_generate_keepalived_config_multiple_vips(self, temp_dir: Path) -> None:
        """Test keepalived config generation with multiple VIPs."""
        from ober.commands.config import _generate_keepalived_config

        config = OberConfig(install_path=temp_dir)
        config.ha_mode = "keepalived"
        config.keepalived = KeepalivedConfig(
            peers=["10.0.0.2"],
            interface="eth0",
            use_multicast=False,
            advert_int=1,
        )
        config.vips = [
            VIPConfig(address="192.168.1.100/32"),
            VIPConfig(address="192.168.1.101/32"),
            VIPConfig(address="192.168.1.102/32"),
        ]
        config.stats_port = 8404
        config.ensure_directories()

        with patch("ober.commands.config.SystemInfo") as mock_system:
            mock_system.return_value.get_local_ip.return_value = "10.0.0.1"
            mock_system.return_value.hostname = "node01"
            _generate_keepalived_config(config)

        content = config.keepalived_config_path.read_text()

        # Verify all VIPs are present
        assert "vrrp_instance VI_1" in content
        assert "vrrp_instance VI_2" in content
        assert "vrrp_instance VI_3" in content
        assert "192.168.1.100/32" in content
        assert "192.168.1.101/32" in content
        assert "192.168.1.102/32" in content


class TestKeepalivedVersion:
    """Tests for keepalived version detection."""

    def test_get_keepalived_version_installed(self) -> None:
        """Test version detection when keepalived is installed."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = "Keepalived v2.2.8"

        with patch("subprocess.run", return_value=mock_result):
            version = get_keepalived_version()
            assert version == "2.2.8"

    def test_get_keepalived_version_not_installed(self) -> None:
        """Test version detection when keepalived is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            version = get_keepalived_version()
            assert version is None

    def test_get_keepalived_version_timeout(self) -> None:
        """Test version detection on timeout."""
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("keepalived", 5)):
            version = get_keepalived_version()
            assert version is None


class TestServiceCommandsKeepalived:
    """Tests for service commands in keepalived mode."""

    def test_start_keepalived_mode(self, cli_runner: CliRunner) -> None:
        """Test start command in keepalived mode."""
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
            config_mock.validate.return_value = ([], [])
            config_mock.ha_mode = "keepalived"
            config_mock.haproxy_config_path.exists.return_value = True
            config_mock.keepalived_config_path.exists.return_value = True
            config_mock.keepalived.peers = ["10.0.0.2"]
            mock_config.return_value = config_mock

            cli_runner.invoke(main, ["start"])
            # Should start ober-ha instead of ober-bgp
            assert mock_run.called
            calls = [str(call) for call in mock_run.call_args_list]
            assert any("ober-ha" in str(call) for call in calls)

    def test_stop_keepalived_mode(self, cli_runner: CliRunner) -> None:
        """Test stop command in keepalived mode."""
        with (
            patch("ober.cli.SystemInfo") as mock_system,
            patch("ober.commands.service.ServiceInfo.from_service_name") as mock_svc,
            patch("ober.commands.service.OberConfig.load") as mock_config,
            patch("ober.commands.service.run_command"),
            patch("ober.commands.service.time.sleep"),
            patch("ober.commands.service._drain", return_value=False) as mock_drain,
        ):
            mock_instance = MagicMock()
            mock_instance.is_root = True
            mock_system.return_value = mock_instance

            bgp_mock = MagicMock()
            bgp_mock.is_active = False
            ka_mock = MagicMock()
            ka_mock.is_active = True
            http_mock = MagicMock()
            http_mock.is_active = True

            mock_svc.side_effect = [bgp_mock, ka_mock, http_mock, bgp_mock, ka_mock]

            config_mock = MagicMock()
            config_mock.ha_mode = "keepalived"
            mock_config.return_value = config_mock

            result = cli_runner.invoke(main, ["stop"])
            assert result.exit_code == 0, result.output
            mock_drain.assert_called_once_with(config_mock, True)


@pytest.fixture
def temp_dir() -> Path:
    """Create a temporary directory."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def cli_runner() -> CliRunner:
    """Create a CLI runner."""
    return CliRunner()


def test_same_peer_list_on_all_nodes_gives_one_owner(temp_dir: Path) -> None:
    """One ober.yaml lists all nodes (self included): each VIP still has exactly one owner."""
    import re

    from ober.commands.config import _generate_keepalived_config

    nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    vips = ["192.168.1.100", "192.168.1.101", "192.168.1.102", "192.168.1.103"]
    priorities: dict[str, list[int]] = {v: [] for v in vips}
    for node in nodes:
        config = OberConfig(install_path=temp_dir / node, local_ip=node)
        config.ha_mode = "keepalived"
        config.keepalived = KeepalivedConfig(peers=[*nodes, nodes[0]], interface="eth0")
        config.vips = [VIPConfig(address=v) for v in vips]
        config.ensure_directories()
        _generate_keepalived_config(config)
        text = config.keepalived_config_path.read_text()
        for block in text.split("vrrp_instance ")[1:]:
            vip = re.search(r"virtual_ipaddress \{\s+(\S+)/32", block)
            prio = re.search(r"priority (\d+)", block)
            assert vip and prio
            priorities[vip.group(1)].append(int(prio.group(1)))
            peers = re.search(r"unicast_peer \{([^}]*)\}", block)
            assert peers and peers.group(1).split() == [n for n in nodes if n != node]
    for vip, prios in priorities.items():
        assert sorted(prios) == [90, 90, 100], vip


def test_status_expected_owner_matches_generator() -> None:
    from ober.commands.status import _expected_owner

    nodes = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    owners = set()
    for node in nodes:
        config = OberConfig(local_ip=node, keepalived=KeepalivedConfig(peers=nodes))
        owners.add(_expected_owner(config, "192.168.1.100"))
    assert owners == {get_vip_owner("192.168.1.100", nodes, "")[0]}


def test_vrrp_router_ids_are_unique() -> None:
    from ober.commands.config import vrrp_router_ids

    assert get_vrrp_router_id("10.0.0.2") == get_vrrp_router_id("10.0.0.6")
    ids = vrrp_router_ids(["10.0.0.6", "10.0.0.2"])
    assert ids == vrrp_router_ids(["10.0.0.2", "10.0.0.6"])  # same on every node
    assert ids["10.0.0.2"] == get_vrrp_router_id("10.0.0.2")
    assert ids["10.0.0.6"] != ids["10.0.0.2"]
    many = vrrp_router_ids([f"10.0.{i // 250}.{i % 250}" for i in range(255)])
    assert sorted(many.values()) == list(range(1, 256))
    with pytest.raises(ValueError, match="at most 255"):
        vrrp_router_ids([f"10.1.{i // 250}.{i % 250}" for i in range(256)])
