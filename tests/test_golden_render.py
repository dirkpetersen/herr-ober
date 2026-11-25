"""Render a 3-node cluster in both HA modes and check every file with the real tool.

Each tool check skips when its binary is missing. ExaBGP binaries come from the
``OBER_TEST_EXABGP`` environment variable (paths separated by ``os.pathsep``, so 4.2 and 5.0
can both be checked) or else from ``exabgp`` on PATH.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ober.commands.config import (
    _generate_exabgp_config,
    _generate_haproxy_config,
    _generate_keepalived_config,
    _netplan_vip_yaml,
    _write_security_lists,
)
from ober.config import (
    BackendConfig,
    BGPConfig,
    CertConfig,
    KeepalivedConfig,
    OberConfig,
    SecurityConfig,
    VIPConfig,
)

NODES = ["10.10.0.11", "10.10.0.12", "10.10.0.13"]
VIPS = [
    VIPConfig(address="10.10.100.1/32"),
    VIPConfig(address="10.10.100.2/32"),
    VIPConfig(address="203.0.113.10/32", exposure="internet"),
    VIPConfig(address="203.0.113.11/32", exposure="internet"),
]


def _bin(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    sbin = Path("/usr/sbin") / name
    return str(sbin) if sbin.exists() else None


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"{' '.join(cmd)}:\n{out}"
    return out


@pytest.fixture(scope="module")
def cert(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("cert")
    pem = root / "server.pem"
    openssl = shutil.which("openssl")
    if openssl:
        subprocess.run(
            [
                openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                "-nodes", "-keyout", str(root / "k.pem"), "-out", str(root / "c.pem"),
                "-days", "2", "-subj", "/CN=s3.example.org",
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        pem.write_text((root / "c.pem").read_text() + (root / "k.pem").read_text())
    return pem


def render(root: Path, cert: Path, mode: str, node: int) -> OberConfig:
    """Render all files of one node into ``root``."""
    ip = NODES[node]
    config = OberConfig(install_path=root)
    config.ensure_directories()
    config.ha_mode = mode
    config.vips = [VIPConfig(address=v.address, exposure=v.exposure) for v in VIPS]
    config.backends = [BackendConfig(name="ceph", servers=["10.20.0.1:7480", "10.20.0.2:7480"])]
    config.certs = CertConfig(path=str(cert))
    config.stats_bind = [ip]
    config.security = SecurityConfig(
        s3_hostnames=["s3.example.org"],
        allowlist=["192.0.2.0/24"],
        trusted_networks=["198.51.100.0/24"],
        internal_networks=["10.0.0.0/8"],
        log_target="10.10.0.50:5514",
    )
    config.bgp = BGPConfig(
        neighbors=["10.10.0.1", "10.10.0.2"], router_id=ip, local_address=ip, bfd_enabled=True
    )
    # interface "" = the default route interface; keepalived -t fails on a missing one
    config.keepalived = KeepalivedConfig(peers=[n for n in NODES if n != ip])
    _write_security_lists(config)
    _generate_haproxy_config(config, socket_path=root / "h.sock")
    if mode == "bgp":
        _generate_exabgp_config(config)
        netplan = root / "netplan-root" / "etc" / "netplan" / "60-vip.yaml"
        netplan.parent.mkdir(parents=True)
        netplan.write_text(_netplan_vip_yaml(config))
        netplan.chmod(0o600)
    else:
        system = MagicMock(hostname=f"node{node + 1}")
        system.get_local_ip.return_value = ip
        with patch("ober.system.SystemInfo", return_value=system):
            _generate_keepalived_config(config)
    return config


@pytest.fixture(scope="module", params=["bgp", "keepalived"])
def cluster(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, cert: Path
) -> list[OberConfig]:
    base = tmp_path_factory.mktemp(request.param)
    return [render(base / f"node{i + 1}", cert, request.param, i) for i in range(len(NODES))]


def test_haproxy_check(cluster: list[OberConfig], cert: Path) -> None:
    haproxy = _bin("haproxy")
    if not haproxy or not cert.exists():
        pytest.skip("haproxy or openssl not installed")
    for config in cluster:
        out = _run([haproxy, "-c", "-f", str(config.haproxy_config_path)])
        assert "WARNING" not in out, out


def test_haproxy_shape(cluster: list[OberConfig]) -> None:
    text = cluster[0].haproxy_config_path.read_text()
    assert "bind 10.10.100.1:443,10.10.100.2:443 ssl" in text
    assert "bind 203.0.113.10:443,203.0.113.11:443 ssl" in text
    assert "bind 127.0.0.1:8404,10.10.0.11:8404\n" in text
    assert "monitor fail if { nbsrv(ceph) lt 1 }" in text


def _exabgp_bins() -> list[str]:
    env = os.environ.get("OBER_TEST_EXABGP")
    bins = env.split(os.pathsep) if env else [shutil.which("exabgp") or ""]
    return [b for b in bins if b]


def test_exabgp_validate(cluster: list[OberConfig]) -> None:
    if cluster[0].ha_mode != "bgp":
        pytest.skip("BGP mode only")
    bins = _exabgp_bins()
    if not bins:
        pytest.skip("exabgp not installed")
    for exabgp in bins:
        version = subprocess.run([exabgp, "--version"], capture_output=True, text=True).stdout
        # 4.x has a --validate flag, 5.x a validate subcommand
        flag = "--validate" if re.search(r"ExaBGP : 4\.", version) else "validate"
        for config in cluster:
            assert "bfd" not in config.bgp_config_path.read_text()
            _run([exabgp, flag, str(config.bgp_config_path)])


def test_netplan_generate(cluster: list[OberConfig]) -> None:
    if cluster[0].ha_mode != "bgp":
        pytest.skip("BGP mode only")
    netplan = _bin("netplan")
    if not netplan:
        pytest.skip("netplan not installed")
    for config in cluster:
        _run([netplan, "generate", "--root-dir", str(config.install_path / "netplan-root")])


def test_keepalived_check(cluster: list[OberConfig]) -> None:
    if cluster[0].ha_mode != "keepalived":
        pytest.skip("keepalived mode only")
    keepalived = _bin("keepalived")
    if not keepalived:
        pytest.skip("keepalived not installed")
    if os.geteuid() != 0:
        pytest.skip("keepalived -t needs root to accept the root script_user")
    for config in cluster:
        _run([keepalived, "-t", "-f", str(config.keepalived_config_path)])


def test_keepalived_one_owner_per_vip(cluster: list[OberConfig]) -> None:
    """All nodes agree on the VRID, and exactly one node owns each VIP."""
    if cluster[0].ha_mode != "keepalived":
        pytest.skip("keepalived mode only")
    per_node = []
    for config in cluster:
        text = config.keepalived_config_path.read_text()
        blocks = re.findall(
            r"virtual_router_id (\d+)\n\s+priority (\d+).*?virtual_ipaddress \{\n\s+(\S+)",
            text,
            re.DOTALL,
        )
        per_node.append({vip: (vrid, prio) for vrid, prio, vip in blocks})
    for vip in (v.address for v in VIPS):
        assert len({node[vip][0] for node in per_node}) == 1
        assert sorted(node[vip][1] for node in per_node) == ["100", "90", "90"]
