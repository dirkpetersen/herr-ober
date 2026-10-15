#!/usr/bin/env python3
"""Parse HAProxy syslog datagrams into :class:`LogRecord` objects.

The payload of each datagram is one JSON object preceded by an RFC 3164 or
RFC 5424 syslog header. The header is skipped by locating the first ``{``.
This module sits on the hot path: no regular expressions, no per-record
allocations beyond the JSON decode and the record itself.
"""

import functools
import ipaddress
import json
import math
import time
from collections.abc import Callable
from typing import Any

from ober.watch.models import LogRecord

MAX_USER_AGENT = 256
MAX_BUCKET = 255
MAX_SHORT = 255  # node, host, access_key, tls_version, method
MAX_IP = 64  # longest textual IPv6 is 45 characters

_TRUE_STRINGS = frozenset({"true", "True", "TRUE", "1", "yes"})
_TRAILING = b"\x00\r\n\t "
MAX_TS_SKEW_S = 86400  # timestamps further from now than this are replaced by now


@functools.lru_cache(maxsize=65536)
def _normalize_ip(value: str) -> str:
    """Canonical form of a single IP address; raises ValueError for anything else.

    Cached: client IPs repeat heavily, and this runs once per log line.
    """
    return str(ipaddress.ip_address(value))


def _str(value: Any, cap: int) -> str:
    """Coerce a JSON value to a capped string; raises TypeError for containers."""
    if type(value) is str:
        return value[:cap] if len(value) > cap else value
    if value is None or isinstance(value, dict | list):
        raise TypeError("not a scalar")
    return str(value)[:cap]


def _int(value: Any) -> int:
    """Coerce a JSON value to a non-negative int."""
    if type(value) is int:
        return value if value > 0 else 0
    if type(value) is bool or value is None or isinstance(value, dict | list):
        raise TypeError("not a number")
    number = int(float(value))
    return number if number > 0 else 0


def _bool(value: Any) -> bool:
    """Coerce a JSON value to a bool."""
    if type(value) is bool:
        return value
    if type(value) is str:
        return value in _TRUE_STRINGS
    return bool(value)


def parse_datagram(data: bytes, now: float | None = None) -> LogRecord | None:
    """Parse one syslog datagram into a record, or None if malformed.

    ``client_ip`` must be a single IP address (it is normalized); anything else, such as a
    CIDR forged over unauthenticated UDP, makes the datagram malformed. A ``ts`` more than
    a day away from ``now`` (default: the wall clock) is replaced by ``now``.
    """
    start = data.find(b"{")
    if start < 0:
        return None
    try:
        obj = json.loads(data[start:].rstrip(_TRAILING))
    except ValueError:
        return None
    if type(obj) is not dict:
        return None
    try:
        ts = float(obj["ts"])
        if not math.isfinite(ts) or ts <= 0:
            return None
        current = time.time() if now is None else now
        if abs(ts - current) > MAX_TS_SKEW_S:
            ts = current
        client_ip = _normalize_ip(_str(obj["client_ip"], MAX_IP).strip())
        status = int(obj["status"])
        if status < 0:
            status = 0  # HAProxy logs -1 for aborted requests (e.g. a banned connection)
        elif status < 100 or status > 599:
            return None
        return LogRecord(
            ts=ts,
            node=_str(obj["node"], MAX_SHORT),
            frontend=_str(obj["frontend"], MAX_SHORT),
            client_ip=client_ip,
            method=_str(obj["method"], 32),
            host=_str(obj["host"], MAX_SHORT),
            bucket=_str(obj["bucket"], MAX_BUCKET),
            access_key=_str(obj["access_key"], MAX_SHORT),
            presigned=_bool(obj["presigned"]),
            status=status,
            bytes_in=_int(obj["bytes_in"]),
            bytes_out=_int(obj["bytes_out"]),
            duration_ms=_int(obj["duration_ms"]),
            user_agent=_str(obj["user_agent"], MAX_USER_AGENT),
            tls_version=_str(obj["tls_version"], 32),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class LogParser:
    """Stateful wrapper around :func:`parse_datagram` that counts outcomes."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.parsed = 0
        self.malformed = 0

    def parse(self, data: bytes) -> LogRecord | None:
        """Parse a datagram; malformed input is counted and returns None."""
        record = parse_datagram(data, self._clock())
        if record is None:
            self.malformed += 1
        else:
            self.parsed += 1
        return record
