"""Tests for summaries, store and blocklist."""

import datetime as dt
import gzip
import json
import os
from pathlib import Path

import pytest

from ober.watch.blocklist import BlockList, normalize_target
from ober.watch.models import Finding, KeySummary
from ober.watch.store import Store
from ober.watch.summaries import SummaryAggregator
from tests.watch_util import rec

# -- summaries -----------------------------------------------------------


def test_summary_window_rollover() -> None:
    agg = SummaryAggregator(300)
    assert agg.add(rec(ts=1000, access_key="A", client_ip="1.1.1.1", bucket="b1")) == []
    agg.add(rec(ts=1100, access_key="A", client_ip="1.1.1.2", bucket="b2", status=403, bytes_in=5))
    agg.add(rec(ts=1100, access_key="", status=500, frontend="s3_internet", node="px2"))
    closed = agg.add(rec(ts=1201 + 300, access_key="A"))
    by_key = {s.access_key: s for s in closed}
    assert set(by_key) == {"A", ""}
    a = by_key["A"]
    assert a.requests == 2 and a.auth_failures == 1 and a.distinct_ips == 2
    assert a.distinct_buckets == 2 and a.bytes_in == 15 and a.window_s == 300
    assert a.window_start == 900
    assert by_key[""].errors_5xx == 1 and by_key[""].internet_requests == 1
    assert agg.flush()[0].requests == 1
    assert agg.flush() == []


def test_summary_tick_and_caps() -> None:
    agg = SummaryAggregator(60, max_keys=2, set_cap=3)
    for i in range(10):
        agg.add(rec(ts=1000, access_key=f"K{i}", client_ip=f"1.1.1.{i}"))
    for i in range(10):
        agg.add(rec(ts=1000, access_key="K0", client_ip=f"2.2.2.{i}"))
    assert agg.tick(1010) == []
    closed = agg.tick(1100)
    assert {s.access_key for s in closed} == {"K0", "K1", "(overflow)"}
    k0 = next(s for s in closed if s.access_key == "K0")
    assert k0.distinct_ips == 3


# -- store ---------------------------------------------------------------


def _ts(day: str) -> float:
    return dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()


def test_store_writes_jsonl(temp_dir: Path) -> None:
    now = [_ts("2026-10-14")]
    store = Store(temp_dir, clock=lambda: now[0])
    store.start()
    store.write_raw(rec())
    store.write_finding(Finding(id="1", ts=1.0, rule="r", severity="low"))
    store.write_summary(KeySummary(1.0, 60, "A"))
    assert store.flush()
    store.close()
    raw = (temp_dir / "raw" / "raw-2026-10-14.jsonl").read_text().splitlines()
    assert json.loads(raw[0])["client_ip"] == "192.0.2.10"
    assert (
        json.loads((temp_dir / "findings" / "findings-2026-10-14.jsonl").read_text())["rule"] == "r"
    )
    assert (temp_dir / "summaries" / "summaries-2026-10-14.jsonl").exists()
    assert store.written["raw"] == 1


def test_store_rotation_compression_and_retention(temp_dir: Path) -> None:
    raw_dir = temp_dir / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "raw-2026-10-12.jsonl").write_text('{"a": 1}\n')
    (raw_dir / "raw-2026-10-14.jsonl").write_text('{"today": 1}\n')
    (raw_dir / "raw-2026-09-01.jsonl.gz").write_bytes(gzip.compress(b"old\n"))
    (raw_dir / "raw-2026-10-10.jsonl.gz").write_bytes(gzip.compress(b"recent\n"))
    store = Store(temp_dir, raw_retention_days=7, clock=lambda: _ts("2026-10-14"))
    store.compress_old()
    names = sorted(p.name for p in raw_dir.iterdir())
    assert names == [
        "raw-2026-10-10.jsonl.gz",
        "raw-2026-10-12.jsonl.gz",
        "raw-2026-10-14.jsonl",
    ]
    assert gzip.decompress((raw_dir / "raw-2026-10-12.jsonl.gz").read_bytes()) == b'{"a": 1}\n'


def test_store_day_rollover_compresses_previous(temp_dir: Path) -> None:
    now = [_ts("2026-10-14")]
    store = Store(temp_dir, clock=lambda: now[0])
    store.start()
    store.write_raw(rec())
    assert store.flush()
    now[0] = _ts("2026-10-15")
    store.write_raw(rec())
    assert store.flush()
    store.close()
    store.compress_old()
    assert (temp_dir / "raw" / "raw-2026-10-14.jsonl.gz").exists()
    assert (temp_dir / "raw" / "raw-2026-10-15.jsonl").exists()


def test_store_drops_when_full(temp_dir: Path) -> None:
    store = Store(temp_dir, queue_size=5)  # writer not started: queue fills
    for _ in range(12):
        store.write_raw(rec())
    store.write_finding(Finding(id="1", ts=1.0, rule="r", severity="low"))
    assert store.dropped["raw"] == 7
    assert store.dropped["findings"] == 1
    assert store.dropped_total == 8


def test_store_raw_disabled(temp_dir: Path) -> None:
    store = Store(temp_dir, store_raw=False)
    store.write_raw(rec())
    assert store.dropped_total == 0 and store._queue.qsize() == 0


# -- blocklist -----------------------------------------------------------


def finding(ip: str = "192.0.2.5", fid: str = "f1") -> Finding:
    return Finding(
        id=fid, ts=1.0, rule="admin_probe", severity="high", client_ip=ip, action="block"
    )


def test_ttl_doubling_and_cap(temp_dir: Path) -> None:
    now = [1000.0]
    bl = BlockList(temp_dir / "b.json", ttl_s=100, ttl_max_s=350, clock=lambda: now[0])
    e1 = bl.add_from_finding(finding())
    assert e1 and e1.expires_at == 1100 and e1.offenses == 1
    assert bl.add_from_finding(finding()) is None  # still active: unchanged
    now[0] = 1101
    assert bl.active() == []
    e2 = bl.add_from_finding(finding())
    assert e2 and e2.expires_at == 1101 + 200 and e2.offenses == 2
    now[0] = 1400
    e3 = bl.add_from_finding(finding())
    assert e3 and e3.expires_at == 1400 + 350  # capped (would be 400)


def test_blocklist_persistence_and_atomic(temp_dir: Path) -> None:
    path = temp_dir / "sub" / "b.json"
    now = [1000.0]
    bl = BlockList(path, ttl_s=100, clock=lambda: now[0])
    bl.add_from_finding(finding())
    bl.add_manual("10.0.0.0/8", "office scan")
    bl.flush()
    assert not list(path.parent.glob("*.tmp"))
    again = BlockList(path, ttl_s=100, clock=lambda: now[0])
    targets = {e.target: e for e in again.active()}
    assert set(targets) == {"192.0.2.5", "10.0.0.0/8"}
    assert targets["10.0.0.0/8"].expires_at == 0 and targets["10.0.0.0/8"].source == "manual"
    now[0] = 1200
    assert [e.target for e in again.active()] == ["10.0.0.0/8"]  # manual never expires
    # offense history survives restarts
    now[0] = 5000
    e = again.add_from_finding(finding())
    assert e and e.offenses == 2


def test_blocklist_manual_remove_and_validation(temp_dir: Path) -> None:
    bl = BlockList(temp_dir / "b.json", clock=lambda: 10.0)
    e = bl.add_manual("203.0.113.9", "x", ttl_s=60)
    assert e.expires_at == 70
    assert bl.remove("203.0.113.9") is True
    assert bl.remove("203.0.113.9") is False
    for bad in ("", "not-an-ip", "0.0.0.0/0", "1.2.3.4; rm", "10.0.0.0/4"):
        with pytest.raises(ValueError):
            bl.add_manual(bad)
    assert normalize_target("10.0.0.7/32") == "10.0.0.7"
    assert normalize_target("10.1.2.3/8") == "10.0.0.0/8"
    assert bl.add_from_finding(finding(ip="not an ip")) is None


def test_blocklist_corrupt_file(temp_dir: Path) -> None:
    p = temp_dir / "b.json"
    p.write_text("{garbage")
    assert BlockList(p).active() == []


def test_blocklist_thread_safety(temp_dir: Path) -> None:
    import threading

    bl = BlockList(temp_dir / "b.json")

    def work(n: int) -> None:
        for i in range(50):
            bl.add_from_finding(finding(ip=f"10.{n}.0.{i}", fid=str(i)))
            bl.active()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(bl) == 400
    bl.flush()
    assert os.path.exists(temp_dir / "b.json")


def _finding_for(ip: str) -> Finding:
    return Finding(id="f", ts=1.0, rule="r", severity="high", client_ip=ip, action="block")


def test_auto_block_rejects_cidr_and_garbage(temp_dir: Path) -> None:
    bl = BlockList(temp_dir / "b.json")
    for bad in ("10.0.0.0/8", "10.0.0.1/32", "1.2.3.0/24", "", "nope", "0.0.0.0/0"):
        assert bl.add_from_finding(_finding_for(bad)) is None, bad
    assert len(bl) == 0
    entry = bl.add_from_finding(_finding_for("2001:DB8::1"))
    assert entry is not None and entry.target == "2001:db8::1"
    assert bl.add_manual("10.0.0.0/8", "ops").target == "10.0.0.0/8"
    with pytest.raises(ValueError):
        bl.add_manual("10.0.0.0/7")


def test_offense_decay_after_ttl_max(temp_dir: Path) -> None:
    now = [1000.0]
    bl = BlockList(temp_dir / "b.json", ttl_s=100, ttl_max_s=1000, clock=lambda: now[0])
    first = bl.add_from_finding(_finding_for("203.0.113.4"))
    assert first is not None and first.offenses == 1
    now[0] += 150  # block expired, still a recent offender
    second = bl.add_from_finding(_finding_for("203.0.113.4"))
    assert second is not None and second.offenses == 2
    now[0] += 5000  # last offense older than ttl_max: start over
    third = bl.add_from_finding(_finding_for("203.0.113.4"))
    assert third is not None and third.offenses == 1
    assert third.expires_at == now[0] + 100
    # the decay state survives a reload
    bl.save()
    again = BlockList(temp_dir / "b.json", ttl_s=100, ttl_max_s=1000, clock=lambda: now[0])
    assert again._last_offense["203.0.113.4"] == now[0]


def test_save_does_not_hold_data_lock_during_io(temp_dir: Path) -> None:
    bl = BlockList(temp_dir / "b.json")
    bl.add_manual("203.0.113.1", "x")
    seen = []
    real_fsync = os.fsync

    def probing_fsync(fd: int) -> None:
        # another thread must be able to take the data lock while we fsync
        import threading

        got: list[bool] = []

        def probe() -> None:
            ok = bl._lock.acquire(timeout=1)
            got.append(ok)
            if ok:
                bl._lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        seen.append(got[0])
        real_fsync(fd)

    bl._dirty = True
    from unittest.mock import patch

    with patch("ober.watch.blocklist.os.fsync", probing_fsync):
        bl.flush()
    assert seen == [True]


def test_summary_folds_failed_keys() -> None:
    from ober.watch.models import FAILED_KEYS

    agg = SummaryAggregator(60)
    agg.add(rec(access_key="GOOD", status=200, ts=1000))
    agg.add(rec(access_key="GOOD", status=403, ts=1001))
    agg.add(rec(access_key="", status=403, ts=1001))
    for i in range(50):
        agg.add(rec(access_key=f"EVIL{i}", status=403, ts=1002, client_ip=f"192.0.2.{i}"))
    rows = {s.access_key: s for s in agg.flush()}
    assert set(rows) == {"GOOD", "", FAILED_KEYS}
    assert rows[FAILED_KEYS].requests == 50 and rows[FAILED_KEYS].auth_failures == 50
    assert rows[FAILED_KEYS].distinct_ips == 50
    assert rows["GOOD"].requests == 2


def test_store_file_modes_and_derived_retention(temp_dir: Path) -> None:
    store = Store(temp_dir, raw_retention_days=2)
    store.start()
    store.write_finding(Finding(id="a", ts=1.0, rule="r", severity="low"))
    store.write_raw(rec())
    store.flush()
    store.close()
    for kind in ("raw", "findings"):
        f = next((temp_dir / kind).glob("*.jsonl"))
        assert (f.stat().st_mode & 0o777) == 0o640
    old = dt.date.today() - dt.timedelta(days=7)  # raw retention 2 -> derived retention 6
    recent = dt.date.today() - dt.timedelta(days=4)
    for kind in ("findings", "summaries"):
        (temp_dir / kind / f"{kind}-{old.isoformat()}.jsonl").write_text("{}\n")
        (temp_dir / kind / f"{kind}-{recent.isoformat()}.jsonl").write_text("{}\n")
    store = Store(temp_dir, raw_retention_days=2)
    store.start()
    store.close()
    for kind in ("findings", "summaries"):
        names = {p.name for p in (temp_dir / kind).glob("*.jsonl")}
        assert f"{kind}-{old.isoformat()}.jsonl" not in names
        assert f"{kind}-{recent.isoformat()}.jsonl" in names
