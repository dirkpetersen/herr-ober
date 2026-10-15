#!/usr/bin/env python3
"""Per-access-key activity summaries, emitted when a window closes."""

from ober.watch.models import FAILED_KEYS, KeySummary, LogRecord

OVERFLOW_KEY = "(overflow)"
SET_CAP = 10_000
NODE_CAP = 64


class _Acc:
    """Mutable accumulator for one access key."""

    __slots__ = (
        "requests",
        "bytes_in",
        "bytes_out",
        "auth_failures",
        "errors_5xx",
        "ok",
        "internet",
        "ips",
        "buckets",
        "nodes",
    )

    def __init__(self) -> None:
        self.requests = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.auth_failures = 0
        self.errors_5xx = 0
        self.ok = 0  # requests with status < 400
        self.internet = 0
        self.ips: set[str] = set()
        self.buckets: set[str] = set()
        self.nodes: set[str] = set()


class SummaryAggregator:
    """Aggregate records into fixed, aligned windows keyed by access key."""

    def __init__(self, window_s: int, max_keys: int = 50_000, set_cap: int = SET_CAP) -> None:
        self.window_s = max(1, window_s)
        self._max_keys = max_keys
        self._set_cap = set_cap
        self._start: float | None = None
        self._accs: dict[str, _Acc] = {}

    def _align(self, ts: float) -> float:
        return float(int(ts // self.window_s) * self.window_s)

    def add(self, rec: LogRecord) -> list[KeySummary]:
        """Add a record; returns summaries of the previous window if it just closed."""
        closed: list[KeySummary] = []
        if self._start is None:
            self._start = self._align(rec.ts)
        elif rec.ts >= self._start + self.window_s:
            closed = self._close()
            self._start = self._align(rec.ts)

        key = rec.access_key
        acc = self._accs.get(key)
        if acc is None:
            if len(self._accs) >= self._max_keys:
                key = OVERFLOW_KEY
                acc = self._accs.get(key)
            if acc is None:
                acc = _Acc()
                self._accs[key] = acc
        acc.requests += 1
        acc.bytes_in += rec.bytes_in
        acc.bytes_out += rec.bytes_out
        if rec.status < 400:
            acc.ok += 1
        if rec.status == 401 or rec.status == 403:
            acc.auth_failures += 1
        elif rec.status >= 500:
            acc.errors_5xx += 1
        if rec.frontend == "s3_internet":
            acc.internet += 1
        if len(acc.ips) < self._set_cap:
            acc.ips.add(rec.client_ip)
        if rec.bucket and len(acc.buckets) < self._set_cap:
            acc.buckets.add(rec.bucket)
        if len(acc.nodes) < NODE_CAP:
            acc.nodes.add(rec.node)
        return closed

    def tick(self, now: float) -> list[KeySummary]:
        """Close the window if wall-clock time has passed its end (no traffic case)."""
        if self._start is not None and now >= self._start + self.window_s:
            closed = self._close()
            self._start = None
            return closed
        return []

    def flush(self) -> list[KeySummary]:
        """Close the current window immediately (shutdown)."""
        closed = self._close()
        self._start = None
        return closed

    def _close(self) -> list[KeySummary]:
        start = self._start if self._start is not None else 0.0
        kept: dict[str, _Acc] = {}
        folded: _Acc | None = None
        for key, a in self._accs.items():
            # Keys that never succeeded are mostly attacker-chosen: fold them into one
            # row. "" (unsigned) and the overflow row are already single rows.
            if a.ok or key in ("", OVERFLOW_KEY):
                kept[key] = a
                continue
            if folded is None:
                folded = _Acc()
            folded.requests += a.requests
            folded.bytes_in += a.bytes_in
            folded.bytes_out += a.bytes_out
            folded.auth_failures += a.auth_failures
            folded.errors_5xx += a.errors_5xx
            folded.internet += a.internet
            for source, target in ((a.ips, folded.ips), (a.buckets, folded.buckets)):
                if len(target) < self._set_cap:
                    target.update(source)
            if len(folded.nodes) < NODE_CAP:
                folded.nodes.update(a.nodes)
        if folded is not None:
            kept[FAILED_KEYS] = folded
        out = [
            KeySummary(
                window_start=start,
                window_s=self.window_s,
                access_key=key,
                requests=a.requests,
                bytes_in=a.bytes_in,
                bytes_out=a.bytes_out,
                auth_failures=a.auth_failures,
                errors_5xx=a.errors_5xx,
                distinct_ips=len(a.ips),
                distinct_buckets=len(a.buckets),
                internet_requests=a.internet,
                nodes=sorted(a.nodes)[:NODE_CAP],
            )
            for key, a in kept.items()
        ]
        self._accs = {}
        return out
