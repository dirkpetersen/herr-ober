"""Tests for ober.watch.parser."""

import json

from ober.watch.parser import LogParser, parse_datagram
from tests.watch_util import line_dict, syslog_line

NOW = 1_700_000_000.0


def test_rfc3164_header() -> None:
    r = parse_datagram(syslog_line(), now=NOW)
    assert r is not None
    assert r.client_ip == "192.0.2.10"
    assert r.status == 200
    assert r.presigned is False
    assert r.ts == 1_700_000_000.0


def test_rfc5424_header_and_trailing_newline() -> None:
    data = (
        b"<134>1 2026-10-14T10:00:00Z px1 haproxy 123 - - "
        + json.dumps(line_dict()).encode()
        + b"\n"
    )
    assert parse_datagram(data) is not None


def test_bare_json() -> None:
    assert parse_datagram(json.dumps(line_dict()).encode()) is not None


def test_missing_key_is_malformed() -> None:
    d = line_dict()
    del d["bucket"]
    assert parse_datagram(b"<1>x: " + json.dumps(d).encode()) is None


def test_bad_json_and_no_brace() -> None:
    assert parse_datagram(b"<134>no json here") is None
    assert parse_datagram(b"<134>x: {not json") is None
    assert parse_datagram(b"<134>x: [1,2]") is None
    assert parse_datagram(b"") is None


def test_type_coercion() -> None:
    r = parse_datagram(
        syslog_line(status="404", bytes_in="12", duration_ms=3.7, presigned="true", ts="1.5e9"),
        now=1.5e9,
    )
    assert r is not None
    assert (r.status, r.bytes_in, r.duration_ms, r.presigned, r.ts) == (404, 12, 3, True, 1.5e9)


def test_rejects_bad_values() -> None:
    assert parse_datagram(syslog_line(status="abc")) is None
    assert parse_datagram(syslog_line(status=99)) is None
    assert parse_datagram(syslog_line(ts="nan")) is None
    assert parse_datagram(syslog_line(bucket={"a": 1})) is None
    assert parse_datagram(syslog_line(user_agent=None)) is None


def test_truncation() -> None:
    r = parse_datagram(syslog_line(user_agent="A" * 1000, bucket="b" * 1000))
    assert r is not None
    assert len(r.user_agent) == 256
    assert len(r.bucket) == 255


def test_invalid_utf8_does_not_raise() -> None:
    data = b"<1>x: " + json.dumps(line_dict()).encode().replace(b"aws-cli", b"aws\xff\xfe")
    parse_datagram(data)  # may be None or a record, must not raise


def test_counters() -> None:
    p = LogParser()
    assert p.parse(syslog_line()) is not None
    assert p.parse(b"garbage") is None
    assert (p.parsed, p.malformed) == (1, 1)


def test_client_ip_must_be_single_ip() -> None:
    for bad in ("10.0.0.0/8", "10.0.0.1/32", "not-an-ip", "", "1.2.3.4, 5.6.7.8", "0/0"):
        assert parse_datagram(syslog_line(client_ip=bad), now=NOW) is None, bad


def test_client_ip_is_normalized() -> None:
    r = parse_datagram(syslog_line(client_ip=" 2001:DB8:0:0::1 "), now=NOW)
    assert r is not None and r.client_ip == "2001:db8::1"


def test_timestamp_clamped_to_now() -> None:
    for ts in (1e11, 1e12, 1.0, NOW + 90_000, NOW - 90_000):
        r = parse_datagram(syslog_line(ts=ts), now=NOW)
        assert r is not None and r.ts == NOW, ts
    r = parse_datagram(syslog_line(ts=NOW - 3600), now=NOW)
    assert r is not None and r.ts == NOW - 3600


def test_logparser_counts_forged_ip_as_malformed() -> None:
    p = LogParser(clock=lambda: NOW)
    assert p.parse(syslog_line(client_ip="10.0.0.0/8")) is None
    assert p.malformed == 1
