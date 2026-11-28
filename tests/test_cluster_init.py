"""Tests for ober cluster init (IP classification, config, the steps it runs)."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from ober.commands import cluster_init as ci
from tests.test_cert import make_pem


def probe_out(mid: str, addrs: list[tuple[str, str]], ober: bool = True, path: str = "") -> str:
    lines = "".join(f"2: {dev}    inet {a} brd x scope global {dev}\n" for dev, a in addrs)
    return f"{mid}\n---\n{lines}---\n{'/usr/local/bin/ober' if ober else ''}\n---\n{path}\n"


def test_parse_probe_and_classify() -> None:
    """A NIC address is a node, a /32 or an unreachable IP is floating; one host twice fails."""
    n1 = ci.parse_probe(
        "10.0.0.11", 0, probe_out("a", [("eth0", "10.0.0.11/24"), ("eth0", "10.0.0.100/32")])
    )
    n2 = ci.parse_probe("10.0.0.12", 0, probe_out("b", [("eth0", "10.0.0.12/24")]))
    vip_on_n1 = ci.parse_probe(
        "10.0.0.100", 0, probe_out("a", [("eth0", "10.0.0.11/24"), ("eth0", "10.0.0.100/32")])
    )
    down = ci.parse_probe("10.0.0.101", 255, "ssh: connect to host 10.0.0.101: No route")
    assert n1.is_node and n1.dev == "eth0" and not vip_on_n1.is_node and down.error
    nodes, floating, errors = ci.classify([n1, n2, vip_on_n1, down])
    assert [h.ip for h in nodes] == ["10.0.0.11", "10.0.0.12"]
    assert floating == ["10.0.0.100", "10.0.0.101"] and not errors

    twice = ci.parse_probe("10.0.1.11", 0, probe_out("a", [("eth1", "10.0.1.11/24")]))
    _, _, errors = ci.classify([n1, twice, down])
    assert any("same host" in e for e in errors)
    _, _, errors = ci.classify([n1, n2])
    assert any("no floating IP" in e for e in errors)


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ("s3.example.org", ("s3.example.org:443", True)),
        ("rgw1:7480", ("rgw1:7480", False)),
        ("https://bucket.s3.amazonaws.com/x", ("bucket.s3.amazonaws.com:443", True)),
        ("http://rgw1", ("rgw1:80", False)),
    ],
)
def test_parse_backend(spec: str, want: tuple[str, bool]) -> None:
    assert ci.parse_backend(spec) == want


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_cert_hostnames(tmp_path: Path) -> None:
    pem = make_pem(tmp_path, "c")
    assert ci.cert_hostnames(pem) == ["s3.example.com"]


def test_build_config_is_valid(tmp_path: Path) -> None:
    """The shared ober.yaml validates; a public VIP is internet with the allowlist."""
    from ober.config import OberConfig

    data = ci.build_config(
        ["10.0.0.11", "10.0.0.12"],
        ["10.0.0.100", "8.8.4.4"],
        [("s3.example.org:443", True)],
        "/opt/herr-ober/etc/certs/server.pem",
        ["s3.example.org"],
        ["192.0.2.0/24"],
    )
    assert [v["exposure"] for v in data["vips"]] == ["internal", "internet"]
    assert data["security"]["allowlist"] == ["192.0.2.0/24"]
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "ober.yaml").write_text(yaml.safe_dump(data))
    (tmp_path / "etc" / "ober.local.yaml").write_text("local_ip: 10.0.0.11\n")
    with patch.dict("os.environ", {"OBER_INSTALL_PATH": str(tmp_path)}):
        errors, _ = OberConfig.load().validate()
    assert errors == []


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_run_init_reuses_node_cert_and_runs_steps(tmp_path: Path) -> None:
    """No --cert: the PEM from a node is used; fresh nodes are installed and bootstrapped."""
    pem = make_pem(tmp_path, "c")
    probes = {
        "10.0.0.11": probe_out("a", [("eth0", "10.0.0.11/24")], path="/opt/herr-ober"),
        "10.0.0.12": probe_out("b", [("eth0", "10.0.0.12/24")], ober=False),
    }
    calls: list[tuple[str, str, str | None]] = []

    def run_on(ip: str, _cfg: object, cmd: str, **kw: object):
        calls.append((ip, cmd, kw.get("stdin")))  # type: ignore[arg-type]
        if cmd == ci.PROBE:
            return (0, probes[ip]) if ip in probes else (255, "No route to host")
        if cmd.startswith("ping"):
            return 0, ""  # no answer, no lladdr: free
        if cmd == "sudo -n ober --json status":
            return 2, '{"certificate": {"path": "/opt/herr-ober/etc/certs/old.pem"}}\nwarn\n'
        if cmd.startswith("sudo -n cat"):
            return 0, pem
        if cmd.startswith("ip route get"):
            return 0, "54.1.2.3 via 10.0.0.1 dev eth0 src 10.0.0.11 uid 0"
        return 0, ""

    with patch.object(ci, "_run_on", side_effect=run_on):
        assert ci.run_init(
            ["10.0.0.11", "10.0.0.12", "10.0.0.100"], ["s3.example.com"], None, [], yes=True
        )
    steps = [(ip, cmd) for ip, cmd, _ in calls if cmd != ci.PROBE]
    n2 = [cmd for ip, cmd in steps if ip == "10.0.0.12"]
    assert any(c.startswith("curl -fsSL") for c in n2)
    assert "sudo -n ober bootstrap -y" in n2
    assert not any(c.startswith("curl") for ip, c in steps if ip == "10.0.0.11")
    assert n2[-1] == "sudo -n ober start"
    shared = next(s for ip, c, s in calls if ip == "10.0.0.12" and "ober apply" in c)
    assert shared is not None
    data = yaml.safe_load(shared)
    assert data["vips"] == [{"address": "10.0.0.100", "exposure": "internal"}]
    assert data["certs"]["path"] == "/opt/herr-ober/etc/certs/old.pem"
    assert data["security"]["s3_hostnames"] == ["s3.example.com"]
    local = next(s for ip, c, s in calls if ip == "10.0.0.12" and "ober.local.yaml" in c)
    assert local == "local_ip: 10.0.0.12\n"


def test_run_init_refuses_ip_in_use() -> None:
    """A floating IP that answers ping is in use: nothing is changed."""
    probes = {
        "10.0.0.11": probe_out("a", [("eth0", "10.0.0.11/24")]),
        "10.0.0.12": probe_out("b", [("eth0", "10.0.0.12/24")]),
    }

    def run_on(ip: str, _cfg: object, cmd: str, **_kw: object):
        if cmd == ci.PROBE:
            return (0, probes[ip]) if ip in probes else (255, "Connection refused")
        if cmd.startswith("ping"):
            return 0, "ANSWER\n10.0.0.100 dev eth0 lladdr 52:54:00:00:00:01 REACHABLE"
        raise AssertionError(f"must not run {cmd}")

    with patch.object(ci, "_run_on", side_effect=run_on):
        assert not ci.run_init(
            ["10.0.0.11", "10.0.0.12", "10.0.0.100"], ["s3.example.com"], "x", [], yes=True
        )


def test_wizard_quick_setup_asks_four_things(tmp_path: Path) -> None:
    """With no ober.yaml, 'ober config' asks only IPs, backend, cert and client CIDRs."""
    from ober.commands import config as cfg

    pem = tmp_path / "c.pem"
    pem.write_text("PEM")
    answers = iter(["10.0.0.11, 10.0.0.12,10.0.0.100", "s3.example.org", str(pem), ""])
    with (
        patch.object(cfg.inquirer, "text", side_effect=lambda **_: next(answers)),
        patch.object(ci, "run_init", return_value=True) as run,
    ):
        assert cfg._quick_setup()
    run.assert_called_once_with(
        ["10.0.0.11", "10.0.0.12", "10.0.0.100"], ["s3.example.org"], "PEM", [], yes=False
    )
