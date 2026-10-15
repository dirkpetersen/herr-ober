#!/usr/bin/env python3
"""Microsoft Sentinel sink using the Azure Monitor Logs Ingestion API.

Only findings and per-key summaries are sent here. Raw logs never are.

Notes on the API (see deploy/sentinel/README.md for sources):
    * A call may carry at most 1 MB, compressed and uncompressed alike, so batches
      are split below 1,000,000 bytes of serialized JSON.
    * ``Content-Encoding: gzip`` is supported and used.
    * 429/5xx are retried honoring ``Retry-After``.
    * ``id`` is a reserved column name, so ``Finding.id`` is sent as ``finding_id``.
"""

import gzip
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from ober.config import SentinelConfig
from ober.watch.auth import AuthError, TokenProvider
from ober.watch.models import Finding, KeySummary

logger = logging.getLogger(__name__)

SCOPE = "https://monitor.azure.com/.default"
API_VERSION = "2023-01-01"
MAX_BODY_BYTES = 1_000_000
MAX_SPOOL_BYTES = 1_000_000_000
HTTP_TIMEOUT: tuple[float, float] = (5.0, 60.0)
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
RETRY_AFTER_MAX_S = 120.0
# After a retryable failure, rows are spooled without trying the network for this long.
COOLDOWN_S = 30.0
REPLAY_MAX_FILES = 50

OK = "ok"
RETRY = "retry"  # transient failure: spool
REJECT = "reject"  # permanent failure (4xx): drop, spooling would never succeed


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def finding_row(f: Finding) -> dict[str, Any]:
    """Convert a finding to a table row (``id`` -> ``finding_id``, plus TimeGenerated)."""
    row = f.to_dict()
    row["finding_id"] = row.pop("id")
    row["TimeGenerated"] = _iso(f.ts)
    return row


def summary_row(s: KeySummary) -> dict[str, Any]:
    """Convert a key summary to a table row; TimeGenerated is the window end."""
    row = s.to_dict()
    row["TimeGenerated"] = _iso(s.window_start + s.window_s)
    return row


def split_batches(rows: list[dict[str, Any]], max_bytes: int = MAX_BODY_BYTES) -> list[bytes]:
    """Serialize rows into JSON-array bodies, each strictly below ``max_bytes``.

    A single row that alone exceeds the limit is dropped with an error log.
    """
    bodies: list[bytes] = []
    parts: list[bytes] = []
    size = 2  # "[" and "]"
    for row in rows:
        blob = json.dumps(row, separators=(",", ":"), default=str).encode()
        extra = len(blob) + (1 if parts else 0)
        if len(blob) + 2 >= max_bytes:
            logger.error("dropping oversized row (%d bytes)", len(blob))
            continue
        if size + extra >= max_bytes and parts:
            bodies.append(b"[" + b",".join(parts) + b"]")
            parts, size, extra = [], 2, len(blob)
        parts.append(blob)
        size += extra
    if parts:
        bodies.append(b"[" + b",".join(parts) + b"]")
    return bodies


class SentinelSink:
    """Sends findings and key summaries to Sentinel custom tables."""

    def __init__(
        self,
        config: SentinelConfig,
        client_secret: str,
        data_dir: Path,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        spool_cap_bytes: int = MAX_SPOOL_BYTES,
        max_body_bytes: int = MAX_BODY_BYTES,
        token_provider: TokenProvider | None = None,
    ) -> None:
        """Create the sink.

        Args:
            config: Sentinel configuration.
            client_secret: Entra app client secret.
            data_dir: ``OberConfig.watch_data_dir``; the spool lives below it.
            session: Optional ``requests.Session`` (injected in tests).
            sleep: Sleep function (patched in tests).
            clock: Monotonic clock for the failure cooldown.
            spool_cap_bytes: Maximum total spool size before the oldest files are dropped.
            max_body_bytes: Maximum serialized body size per call.
            token_provider: Optional token provider (injected in tests).
        """
        self._cfg = config
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._spool_root = Path(data_dir) / "spool" / "sentinel"
        self._spool_cap = spool_cap_bytes
        self._max_body = max_body_bytes
        self._auth = token_provider or TokenProvider(
            config.tenant_id, config.client_id, client_secret, SCOPE, session=self._session
        )
        self._lock = threading.Lock()
        self._seq = 0
        self._down_until = 0.0
        self.dropped_files = 0
        self.dropped_rows = 0
        self.rejected_rows = 0

    # -- public API -------------------------------------------------------

    def send_findings(self, findings: list[Finding]) -> None:
        """Send findings to the findings stream. Never raises."""
        self._safe_send(self._cfg.findings_stream, [finding_row(f) for f in findings])

    def send_summaries(self, summaries: list[KeySummary]) -> None:
        """Send key summaries to the summary stream. Never raises."""
        self._safe_send(self._cfg.summary_stream, [summary_row(s) for s in summaries])

    def close(self) -> None:
        """Nothing is buffered in memory; failed rows are already spooled."""
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            logger.debug("session close failed", exc_info=True)

    # -- internals --------------------------------------------------------

    def _safe_send(self, stream: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            with self._lock:
                self._send(stream, rows)
        except Exception:  # noqa: BLE001 - sinks must never raise
            logger.exception("sentinel sink error for stream %s", stream)

    def _send(self, stream: str, rows: list[dict[str, Any]]) -> None:
        all_ok = True
        for body in split_batches(rows, self._max_body):
            if self._clock() < self._down_until:
                self._spool_body(stream, body)
                all_ok = False
                continue
            result = self._post(stream, body)
            if result == RETRY:
                self._down_until = self._clock() + COOLDOWN_S
                self._spool_body(stream, body)
                all_ok = False
            elif result == REJECT:
                self.rejected_rows += body.count(b'"TimeGenerated"')
        if all_ok:
            self._replay()

    def _post(self, stream: str, body: bytes) -> str:
        url = (
            f"{self._cfg.dce_endpoint.rstrip('/')}/dataCollectionRules/"
            f"{self._cfg.dcr_immutable_id}/streams/{stream}?api-version={API_VERSION}"
        )
        payload = gzip.compress(body)
        refreshed = False
        for attempt in range(MAX_ATTEMPTS):
            try:
                token = self._auth.get_token()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                }
                resp = self._session.post(url, data=payload, headers=headers, timeout=HTTP_TIMEOUT)
            except AuthError as e:
                logger.warning("sentinel auth failed: %s", e)
                return RETRY
            except requests.RequestException as e:
                logger.warning("sentinel request failed: %s", type(e).__name__)
                if attempt + 1 < MAX_ATTEMPTS:
                    self._sleep(self._backoff(attempt, None))
                continue
            code = resp.status_code
            if 200 <= code < 300:
                return OK
            if code == 401 and not refreshed:
                refreshed = True
                self._auth.invalidate()
                continue
            if code == 429 or code >= 500:
                logger.warning("sentinel returned HTTP %d (attempt %d)", code, attempt + 1)
                if attempt + 1 < MAX_ATTEMPTS:
                    self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            logger.error("sentinel rejected batch with HTTP %d: %s", code, resp.text[:300])
            return REJECT
        return RETRY

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), RETRY_AFTER_MAX_S)
            except ValueError:
                pass
        return min(BACKOFF_BASE_S * float(2**attempt), BACKOFF_MAX_S)

    # -- spool ------------------------------------------------------------

    def _spool_body(self, stream: str, body: bytes) -> None:
        """Persist a JSON-array body as a JSONL file."""
        try:
            rows = json.loads(body)
            d = self._spool_root / stream
            d.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            name = f"{time.time_ns():020d}-{self._seq:06d}.jsonl"
            tmp = d / (name + ".tmp")
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
            os.replace(tmp, d / name)
            self._enforce_cap()
        except OSError:
            logger.exception(
                "cannot spool sentinel rows; %s rows lost", body.count(b'"TimeGenerated"')
            )

    def _spool_files(self) -> list[Path]:
        if not self._spool_root.exists():
            return []
        files = [p for p in self._spool_root.glob("*/*.jsonl") if p.is_file()]
        return sorted(files, key=lambda p: p.name)

    def spool_size(self) -> int:
        """Total bytes currently spooled."""
        return sum(p.stat().st_size for p in self._spool_files())

    def _enforce_cap(self) -> None:
        files = self._spool_files()
        total = sum(p.stat().st_size for p in files)
        for p in files:  # oldest first (names start with a ns timestamp)
            if total <= self._spool_cap:
                break
            try:
                size = p.stat().st_size
                rows = sum(1 for _ in p.open("rb"))
                p.unlink()
            except OSError:
                continue
            total -= size
            self.dropped_files += 1
            self.dropped_rows += rows
            logger.warning("spool over cap: dropped %s (%d rows)", p.name, rows)

    def _replay(self) -> None:
        """Re-send spooled files, oldest first, stopping at the first failure."""
        for path in self._spool_files()[:REPLAY_MAX_FILES]:
            stream = path.parent.name
            try:
                rows = [json.loads(line) for line in path.read_text().splitlines() if line]
            except (OSError, ValueError):
                logger.error("unreadable spool file %s removed", path)
                path.unlink(missing_ok=True)
                continue
            result = OK
            for body in split_batches(rows, self._max_body):
                result = self._post(stream, body)
                if result != OK:
                    break
            if result == RETRY:
                self._down_until = self._clock() + COOLDOWN_S
                return
            path.unlink(missing_ok=True)
            if result == REJECT:
                self.rejected_rows += len(rows)
