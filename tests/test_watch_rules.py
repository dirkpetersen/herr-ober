"""Tests for ober.watch.rules."""

from pathlib import Path

import pytest

from ober.config import OberConfig
from ober.watch.rules import RuleEngine, is_valid_bucket_name, sanitize
from tests.watch_util import rec


@pytest.fixture
def config(temp_dir: Path) -> OberConfig:
    cfg = OberConfig(install_path=temp_dir)
    cfg.security.autoban_auth_failures = 5
    cfg.security.autoban_window_s = 60
    (temp_dir / "etc" / "haproxy").mkdir(parents=True)
    return cfg


@pytest.fixture
def engine(config: OberConfig) -> RuleEngine:
    return RuleEngine(config, network_reload_s=0)


def run(engine: RuleEngine, **kw: object) -> list:  # type: ignore[type-arg]
    return engine.process(rec(**kw))


def test_auth_burst_triggers(engine: RuleEngine) -> None:
    out = []
    for i in range(5):
        out += run(engine, status=403, ts=1000 + i)
    assert [f.rule for f in out] == ["auth_failure_burst"]
    assert out[0].severity == "high"
    assert out[0].action == "block"
    assert out[0].count == 5


def test_auth_burst_below_threshold_or_outside_window(engine: RuleEngine) -> None:
    out = []
    for i in range(4):
        out += run(engine, status=401, ts=1000 + i)
    assert not out
    # spread across more than the window: never fires
    for i in range(20):
        out += run(engine, client_ip="192.0.2.99", status=401, ts=1000 + i * 30)
    assert not out


def test_auth_burst_cooldown(engine: RuleEngine) -> None:
    out = []
    for i in range(30):
        out += run(engine, status=403, ts=1000 + i * 0.1)
    assert len(out) == 1
    out = []
    for i in range(10):  # after the window the cooldown has expired
        out += run(engine, status=403, ts=1200 + i * 0.1)
    assert len(out) == 1


def test_auth_burst_per_ip(engine: RuleEngine) -> None:
    out = []
    for i in range(10):
        out += run(engine, client_ip=f"192.0.2.{i}", status=403, ts=1000 + i)
    assert not out


def test_bucket_enumeration(engine: RuleEngine) -> None:
    out = []
    for i in range(19):
        out += run(engine, bucket=f"bucket{i:03d}", status=404, ts=1000 + i)
    assert not out
    out = run(engine, bucket="bucket999", status=403, ts=1030)
    assert [f.rule for f in out] == ["bucket_enumeration"]
    assert out[0].severity == "medium"
    assert out[0].action == "none"


def test_bucket_enumeration_same_bucket_not_counted(engine: RuleEngine) -> None:
    out = []
    for i in range(50):
        out += run(engine, bucket="same", status=404, ts=1000 + i)
    assert not out


def test_bucket_enumeration_window_expiry(engine: RuleEngine) -> None:
    out = []
    for i in range(19):
        out += run(engine, bucket=f"bucket{i:03d}", status=404, ts=1000 + i)
    out += run(engine, bucket="late", status=404, ts=1000 + 400)
    assert not out


def test_anonymous_probe(engine: RuleEngine) -> None:
    out = []
    for i in range(20):
        out += run(engine, access_key="", status=403, ts=1000 + i, bucket="")
    anon = [f for f in out if f.rule == "anonymous_probe"]
    assert len(anon) == 1
    assert anon[0].action == "none"


def test_anonymous_probe_signed_does_not_count(engine: RuleEngine) -> None:
    out = []
    for i in range(30):
        out += run(engine, access_key="AK", status=403, ts=1000 + i * 10, bucket="")
    assert all(f.rule != "anonymous_probe" for f in out)


@pytest.mark.parametrize(
    "ua", ["sqlmap/1.7", "Mozilla Nikto/2.1", "MASSCAN", "Nuclei - x", "zgrab/0.x"]
)
def test_scanner_ua(engine: RuleEngine, ua: str) -> None:
    out = run(engine, user_agent=ua)
    assert [f.rule for f in out] == ["scanner_user_agent"]
    assert out[0].action == "block"
    assert ua not in out[0].detail
    assert run(engine, user_agent=ua, ts=1_700_000_001) == []  # cooldown


def test_scanner_ua_negative(engine: RuleEngine) -> None:
    assert run(engine, user_agent="aws-sdk-java/2.20 Linux") == []


def test_admin_probe(engine: RuleEngine) -> None:
    assert run(engine, frontend="s3_internal", bucket="admin") == []
    out = run(engine, frontend="s3_internet", bucket="admin")
    assert [f.rule for f in out] == ["admin_probe"]
    assert out[0].action == "block"


@pytest.mark.parametrize("bucket", [".env", "wp-admin", ".git", "UPPER", "a", "x..y", "a_b"])
def test_invalid_bucket(engine: RuleEngine, bucket: str) -> None:
    out = run(engine, frontend="s3_internet", bucket=bucket)
    assert [f.rule for f in out] == ["invalid_bucket_probe"]
    assert out[0].severity == "medium"
    assert out[0].action == "none"


def test_invalid_bucket_negative(engine: RuleEngine) -> None:
    assert run(engine, frontend="s3_internet", bucket="my-bucket.v2") == []
    assert run(engine, frontend="s3_internal", bucket=".env") == []
    assert run(engine, frontend="s3_internet", bucket="") == []


def test_action_override(config: OberConfig) -> None:
    eng = RuleEngine(config, actions={"invalid_bucket_probe": "block", "admin_probe": "none"})
    assert eng.process(rec(frontend="s3_internet", bucket=".env"))[0].action == "block"
    assert eng.process(rec(frontend="s3_internet", bucket="admin"))[0].action == "none"


def test_autoban_disabled(config: OberConfig) -> None:
    config.security.autoban_enabled = False
    eng = RuleEngine(config)
    out = []
    for i in range(5):
        out += eng.process(rec(status=403, ts=1000 + i))
    assert out and out[0].action == "none"


def test_trusted_forces_none_allowlist_does_not(config: OberConfig) -> None:
    config.trusted_path.write_text("# office\n198.51.100.0/24\nbad line\n")
    config.allowlist_path.write_text("203.0.113.0/24\n")
    eng = RuleEngine(config, network_reload_s=0)
    out = eng.process(rec(client_ip="198.51.100.7", frontend="s3_internet", bucket="admin"))
    assert out and out[0].rule == "admin_probe" and out[0].action == "none"
    # allowlisted = allowed to connect, not exempt from blocking
    for ip in ("203.0.113.9", "192.0.2.1"):
        out = eng.process(rec(client_ip=ip, frontend="s3_internet", bucket="admin"))
        assert out[0].action == "block"


def test_network_file_reload(config: OberConfig) -> None:
    import os

    eng = RuleEngine(config, network_reload_s=0)
    assert eng.process(rec(client_ip="198.51.100.7", user_agent="sqlmap"))[0].action == "block"
    config.trusted_path.write_text("198.51.100.0/24\n")
    os.utime(config.trusted_path, (1, 2_000_000_000))
    out = eng.process(rec(client_ip="198.51.100.7", user_agent="sqlmap", ts=1_700_001_000))
    assert out[0].action == "none"


def test_memory_bound(config: OberConfig) -> None:
    eng = RuleEngine(config, max_keys=100)
    for i in range(1000):
        eng.process(rec(client_ip=f"10.1.{i // 256}.{i % 256}", status=403, bucket=f"b{i}"))
    assert len(eng._auth) <= 100
    assert len(eng._enum) <= 100
    assert len(eng._anon) <= 100
    assert len(eng._cooldown) <= 100


def test_sanitize_and_detail_has_no_attacker_text(engine: RuleEngine) -> None:
    evil = "x\x00\n<script>" + "A" * 500
    out = run(engine, frontend="s3_internet", bucket=evil, user_agent="sqlmap " + evil)
    assert out
    for f in out:
        assert evil[:10] not in f.detail
        assert f.bucket.isprintable() and len(f.bucket) <= 64
        assert "<" not in f.bucket
    assert sanitize("a\nb") == "a?b"


def test_bucket_name_validation() -> None:
    assert is_valid_bucket_name("abc")
    assert not is_valid_bucket_name("ab")
    assert not is_valid_bucket_name("a" * 64)
