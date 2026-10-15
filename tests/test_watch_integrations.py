"""Tests for the Sentinel and MDE sinks (no network)."""

import gzip
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from ober.config import MDEConfig, OberConfig, SentinelConfig
from ober.watch.auth import AuthError, TokenProvider
from ober.watch.integrations import build_sinks
from ober.watch.mde import MDESink, RateLimiter, is_blockable_ip
from ober.watch.models import Finding, KeySummary
from ober.watch.sentinel import SentinelSink, finding_row, split_batches, summary_row


def resp(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = json.dumps(body) if body is not None else ""
    r.json.return_value = body if body is not None else {}
    return r


def token_resp(expires_in: int = 3600, token: str = "tok") -> MagicMock:
    return resp(200, {"access_token": token, "expires_in": expires_in})


def finding(**kw: Any) -> Finding:
    base: dict[str, Any] = {
        "id": "abc",
        "ts": 1_700_000_000.0,
        "rule": "auth_failure_burst",
        "severity": "high",
        "client_ip": "8.8.8.8",
        "count": 60,
        "window_s": 60,
        "action": "block",
    }
    base.update(kw)
    return Finding(**base)


def sentinel_cfg() -> SentinelConfig:
    return SentinelConfig(
        enabled=True,
        tenant_id="t",
        client_id="c",
        dce_endpoint="https://dce.example.ingest.monitor.azure.com",
        dcr_immutable_id="dcr-123",
    )


class FakeToken:
    def get_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


def make_sentinel(tmp_path: Path, session: MagicMock, **kw: Any) -> SentinelSink:
    kw.setdefault("sleep", lambda _s: None)
    return SentinelSink(
        sentinel_cfg(),
        "secret",
        tmp_path,
        session=session,
        token_provider=FakeToken(),  # type: ignore[arg-type]
        **kw,
    )


# -- auth --------------------------------------------------------------------


def test_token_cached_and_refreshed_before_expiry() -> None:
    now = [1000.0]
    session = MagicMock()
    session.post.return_value = token_resp(expires_in=3600)
    tp = TokenProvider("tenant", "cid", "sec", "scope", session=session, clock=lambda: now[0])
    assert tp.get_token() == "tok"
    assert tp.get_token() == "tok"
    assert session.post.call_count == 1
    url = session.post.call_args.args[0]
    assert url == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    data = session.post.call_args.kwargs["data"]
    assert data["grant_type"] == "client_credentials" and data["scope"] == "scope"
    assert session.post.call_args.kwargs["timeout"]
    now[0] += 3600 - 61  # still outside the 60 s margin
    tp.get_token()
    assert session.post.call_count == 1
    now[0] += 2  # inside the margin
    tp.get_token()
    assert session.post.call_count == 2


def test_token_errors_raise_autherror_without_secret() -> None:
    session = MagicMock()
    session.post.side_effect = requests.ConnectionError("boom sec")
    tp = TokenProvider("t", "c", "sec", "s", session=session)
    with pytest.raises(AuthError) as ei:
        tp.get_token()
    assert "sec" not in str(ei.value)
    session.post.side_effect = None
    session.post.return_value = resp(401, {"error": "invalid_client"})
    with pytest.raises(AuthError, match="invalid_client"):
        tp.get_token()


# -- sentinel ----------------------------------------------------------------


def test_rows_have_timegenerated_and_finding_id() -> None:
    row = finding_row(finding())
    assert row["finding_id"] == "abc" and "id" not in row
    assert row["TimeGenerated"].endswith("Z") and row["TimeGenerated"].startswith("2023-11-14")
    s = KeySummary(window_start=1_700_000_000.0, window_s=300, access_key="AK", nodes=["a"])
    srow = summary_row(s)
    assert srow["nodes"] == ["a"] and srow["TimeGenerated"].startswith("2023-11-14T22:18:20")


def test_split_batches_under_limit() -> None:
    rows = [{"x": "y" * 1000, "n": i} for i in range(3000)]
    bodies = split_batches(rows, 1_000_000)
    assert len(bodies) >= 3
    assert all(len(b) < 1_000_000 for b in bodies)
    assert sum(len(json.loads(b)) for b in bodies) == 3000
    assert split_batches([{"x": "z" * 2_000_000}]) == []


def test_sentinel_posts_gzip_to_correct_url(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(204)
    sink = make_sentinel(tmp_path, session)
    sink.send_findings([finding()])
    url = session.post.call_args.args[0]
    assert url == (
        "https://dce.example.ingest.monitor.azure.com/dataCollectionRules/dcr-123/"
        "streams/Custom-OberFindings_CL?api-version=2023-01-01"
    )
    kw = session.post.call_args.kwargs
    assert kw["headers"]["Content-Encoding"] == "gzip"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    rows = json.loads(gzip.decompress(kw["data"]))
    assert rows[0]["rule"] == "auth_failure_burst" and "TimeGenerated" in rows[0]
    sink.send_summaries([KeySummary(window_start=1.0, window_s=300, access_key="K")])
    assert "Custom-OberKeySummary_CL" in session.post.call_args.args[0]


def test_retry_429_honors_retry_after(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.side_effect = [
        resp(429, headers={"Retry-After": "7"}),
        resp(503),
        resp(204),
    ]
    sleeps: list[float] = []
    sink = make_sentinel(tmp_path, session, sleep=sleeps.append)
    sink.send_findings([finding()])
    assert sleeps[0] == 7.0
    assert sleeps[1] == 2.0  # exponential backoff for attempt 1
    assert session.post.call_count == 3
    assert sink.spool_size() == 0


def test_spool_on_failure_and_replay(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.side_effect = requests.ConnectionError("down")
    now = [0.0]
    sink = make_sentinel(tmp_path, session, clock=lambda: now[0])
    sink.send_findings([finding(id="one")])
    spool = tmp_path / "spool" / "sentinel" / "Custom-OberFindings_CL"
    files = list(spool.glob("*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text().splitlines()[0])["finding_id"] == "one"

    # During cooldown, no network attempt is made; rows are spooled.
    calls = session.post.call_count
    sink.send_findings([finding(id="two")])
    assert session.post.call_count == calls
    assert len(list(spool.glob("*.jsonl"))) == 2

    # Recovery: the next call succeeds and the spool is replayed (oldest first).
    now[0] += 100
    session.post.side_effect = None
    session.post.return_value = resp(204)
    sink.send_findings([finding(id="three")])
    assert list(spool.glob("*.jsonl")) == []
    ids = []
    for c in session.post.call_args_list[-3:]:
        ids += [r["finding_id"] for r in json.loads(gzip.decompress(c.kwargs["data"]))]
    assert ids == ["three", "one", "two"]


def test_spool_cap_drops_oldest(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.side_effect = requests.ConnectionError("down")
    sink = make_sentinel(tmp_path, session, spool_cap_bytes=2500)
    for i in range(10):
        sink.send_findings([finding(id=f"id{i}", detail="d" * 400)])
    assert sink.spool_size() <= 2500
    assert sink.dropped_files > 0 and sink.dropped_rows == sink.dropped_files
    remaining = sorted((tmp_path / "spool" / "sentinel").glob("*/*.jsonl"))
    last = json.loads(remaining[-1].read_text().splitlines()[0])
    assert last["finding_id"] == "id9"  # newest survives


def test_permanent_4xx_is_dropped_not_spooled(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(400, {"error": "bad"})
    sink = make_sentinel(tmp_path, session)
    sink.send_findings([finding()])
    assert sink.spool_size() == 0 and sink.rejected_rows == 1
    assert session.post.call_count == 1


def test_401_refreshes_token_once(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.side_effect = [resp(401), resp(204)]
    sink = make_sentinel(tmp_path, session)
    sink.send_findings([finding()])
    assert session.post.call_count == 2


def test_sentinel_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    session = MagicMock()
    session.post.side_effect = RuntimeError("unexpected")
    sink = make_sentinel(tmp_path, session)
    with caplog.at_level(logging.ERROR):
        sink.send_findings([finding()])
        sink.send_summaries([KeySummary(window_start=1.0, window_s=300, access_key="K")])
    sink.close()
    ro = tmp_path / "ro"
    ro.write_text("file, not dir")  # spooling impossible
    session.post.side_effect = requests.Timeout()
    bad = make_sentinel(ro, session)
    bad.send_findings([finding()])


# -- MDE ---------------------------------------------------------------------


def mde_cfg(**kw: Any) -> MDEConfig:
    return MDEConfig(enabled=True, tenant_id="t", client_id="c", **kw)


def make_mde(tmp_path: Path, session: MagicMock, **kw: Any) -> MDESink:
    kw.setdefault("clock", lambda: 1_700_000_000.0)
    cfg = kw.pop("cfg", mde_cfg())
    return MDESink(
        cfg,
        "secret",
        tmp_path,
        session=session,
        token_provider=FakeToken(),
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("8.8.8.8", True),
        ("2001:4860:4860::8888", True),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("100.64.0.1", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("8.8.8.0/24", False),
        ("not-an-ip", False),
        ("", False),
    ],
)
def test_is_blockable_ip(value: str, ok: bool) -> None:
    assert is_blockable_ip(value) is ok


def test_mde_filter_and_payload(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(200, {"value": [{"indicator": "8.8.8.8", "isFailed": False}]})
    sink = make_mde(tmp_path, session)
    sink.send_findings(
        [
            finding(severity="medium"),  # below min
            finding(action="none", client_ip="9.9.9.9"),
            finding(client_ip="10.1.1.1"),
            finding(client_ip="1.2.3.0/24"),
            finding(),  # accepted
        ]
    )
    assert session.post.call_count == 1
    url = session.post.call_args.args[0]
    assert url == "https://api.securitycenter.microsoft.com/api/indicators/import"
    inds = session.post.call_args.kwargs["json"]["Indicators"]
    assert len(inds) == 1
    ind = inds[0]
    assert ind["indicatorValue"] == "8.8.8.8" and ind["indicatorType"] == "IpAddress"
    assert ind["action"] == "Block" and ind["severity"] == "High"
    assert ind["title"] == "ober: auth_failure_burst"
    assert ind["generateAlert"] is True and ind["application"] == "Herr Ober"
    assert ind["expirationTime"] == "2023-11-15T22:13:20Z"
    assert "60" in ind["description"]


def test_mde_dedup_persisted(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(200, {"value": []})
    # clock must be real-ish for the state loader, which compares against time.time()
    import time

    now = time.time()
    sink = make_mde(tmp_path, session, clock=lambda: now)
    sink.send_findings([finding()])
    sink.send_findings([finding(id="again")])
    assert session.post.call_count == 1
    state = json.loads((tmp_path / "mde_state.json").read_text())
    assert "8.8.8.8" in state
    # A new sink instance (restart) still dedups.
    sink2 = make_mde(tmp_path, session, clock=lambda: now)
    sink2.send_findings([finding()])
    assert session.post.call_count == 1
    # After expiry the IP is resubmitted.
    later = now + 2 * 86400
    sink3 = make_mde(tmp_path, session, clock=lambda: later)
    sink3.send_findings([finding()])
    assert session.post.call_count == 2


def test_mde_cap_nat64_retry_after_and_flush(tmp_path: Path) -> None:
    assert not is_blockable_ip("64:ff9b::a00:1")
    assert not is_blockable_ip("::10.0.0.1")
    assert is_blockable_ip("64:ff9b::808:808")
    session = MagicMock()
    session.post.return_value = resp(200, {"value": []})
    cfg = MDEConfig(enabled=True, tenant_id="t", client_id="c", max_active_indicators=1)
    sink = MDESink(cfg, "s", tmp_path, session=session, token_provider=FakeToken())  # type: ignore[arg-type]
    sink.send_findings([finding(), finding(client_ip="8.8.4.4")])
    assert sink.skipped_cap == 1 and session.post.call_count == 1
    # Retry-After is capped and pending is flushed by send_summaries.
    session2 = MagicMock()
    session2.post.side_effect = [
        resp(429, headers={"Retry-After": "99999"}),
        resp(200, {"value": []}),
    ]
    now = [1000.0]
    s2 = MDESink(
        MDEConfig(enabled=True, tenant_id="t", client_id="c"),
        "s",
        tmp_path / "b",
        session=session2,
        clock=lambda: now[0],
        token_provider=FakeToken(),  # type: ignore[arg-type]
    )
    s2.send_findings([finding()])
    assert s2.pending == 1 and s2._backoff_until == 1300.0
    now[0] = 1301.0
    s2.send_summaries([])
    assert s2.pending == 0


def test_mde_failed_indicator_not_marked_submitted(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(
        200, {"value": [{"indicator": "8.8.8.8", "isFailed": True, "failureReason": "x"}]}
    )
    sink = make_mde(tmp_path, session)
    sink.send_findings([finding()])
    session.post.return_value = resp(200, {"value": [{"indicator": "8.8.8.8", "isFailed": False}]})
    sink.send_findings([finding()])
    assert session.post.call_count == 2


def test_mde_rate_limit_defers_and_batches(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(200, {"value": []})
    t = [0.0]
    limiter = RateLimiter(1, 100, clock=lambda: t[0])
    sink = make_mde(tmp_path, session, limiter=limiter)
    fs = [finding(client_ip=f"8.{i // 250}.{i % 250}.1") for i in range(600)]
    sink.send_findings(fs)
    assert session.post.call_count == 1
    assert len(session.post.call_args.kwargs["json"]["Indicators"]) == 500
    assert sink.pending == 100
    t[0] = 61.0
    sink.send_findings([])
    assert session.post.call_count == 2 and sink.pending == 0


def test_rate_limiter_hourly() -> None:
    t = [0.0]
    lim = RateLimiter(100, 3, clock=lambda: t[0])
    assert [lim.try_acquire() for _ in range(4)] == [True, True, True, False]
    t[0] = 3601.0
    assert lim.try_acquire()


def test_mde_429_backs_off_and_keeps_pending(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.return_value = resp(429, headers={"Retry-After": "30"})
    now = [1_700_000_000.0]
    sink = make_mde(tmp_path, session, clock=lambda: now[0])
    sink.send_findings([finding()])
    sink.send_findings([])
    assert session.post.call_count == 1 and sink.pending == 1
    now[0] += 31
    session.post.return_value = resp(200, {"value": []})
    sink.send_findings([])
    assert session.post.call_count == 2 and sink.pending == 0


def test_mde_never_raises_and_summaries_noop(tmp_path: Path) -> None:
    session = MagicMock()
    session.post.side_effect = RuntimeError("boom")
    sink = make_mde(tmp_path, session)
    sink.send_findings([finding()])
    session.post.side_effect = requests.ConnectionError()
    sink.send_findings([finding(client_ip="9.9.9.9")])
    sink.send_summaries([KeySummary(window_start=1.0, window_s=300, access_key="K")])
    sink.close()
    assert session.post.call_count == 2  # both attempts failed without raising


# -- build_sinks -------------------------------------------------------------


def test_build_sinks_disabled_returns_empty() -> None:
    assert build_sinks(OberConfig(), {}) == []


def test_build_sinks_enabled(tmp_path: Path) -> None:
    cfg = OberConfig()
    cfg.watch.data_dir = str(tmp_path)
    cfg.sentinel = sentinel_cfg()
    cfg.mde = mde_cfg()
    sinks = build_sinks(cfg, {"SENTINEL_CLIENT_SECRET": "a", "MDE_CLIENT_SECRET": "b"})
    assert [type(s).__name__ for s in sinks] == ["SentinelSink", "MDESink"]


def test_build_sinks_misconfigured_warns_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = OberConfig()
    cfg.watch.data_dir = str(tmp_path)
    cfg.sentinel = SentinelConfig(enabled=True, tenant_id="t")  # missing most fields
    cfg.mde = mde_cfg()  # secret missing
    with caplog.at_level(logging.WARNING):
        sinks = build_sinks(cfg, {})
    assert sinks == []
    text = caplog.text
    assert "Sentinel sink disabled" in text and "sentinel.client_id is empty" in text
    assert "SENTINEL_CLIENT_SECRET" in text
    assert "MDE sink disabled" in text and "MDE_CLIENT_SECRET" in text


def test_build_sinks_one_good_one_bad(tmp_path: Path) -> None:
    cfg = OberConfig()
    cfg.watch.data_dir = str(tmp_path)
    cfg.sentinel = sentinel_cfg()
    cfg.mde = mde_cfg(action="Nonsense")
    sinks = build_sinks(cfg, {"SENTINEL_CLIENT_SECRET": "a", "MDE_CLIENT_SECRET": "b"})
    assert [type(s).__name__ for s in sinks] == ["SentinelSink"]
