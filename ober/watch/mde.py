#!/usr/bin/env python3
"""Microsoft Defender for Endpoint sink: confirmed attacker IPs become indicators.

Facts (see deploy/sentinel/README.md for sources):
    * Batch endpoint ``POST /api/indicators/import``: max 500 indicators per call,
      30 calls per minute, body ``{"Indicators": [...]}``.
    * Single endpoint ``POST /api/indicators``: 100 calls/min, 1,500 calls/hour.
    * At most 15,000 active indicators per tenant.
    * CIDR notation for IP indicators is NOT supported, so only single IPs are sent.
    * Application permission: ``Ti.ReadWrite.All``.
"""

import contextlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from ober.config import MDEConfig
from ober.watch.auth import AuthError, TokenProvider
from ober.watch.models import Finding, KeySummary, severity_rank

logger = logging.getLogger(__name__)

SCOPE = "https://api.securitycenter.microsoft.com/.default"
BATCH_PATH = "/api/indicators/import"
BATCH_SIZE = 500
# Stay below the documented 30 calls/minute for the batch endpoint.
MAX_CALLS_PER_MINUTE = 25
MAX_CALLS_PER_HOUR = 1000
MAX_PENDING = 5000
MAX_RETRY_AFTER_S = 300.0
HTTP_TIMEOUT: tuple[float, float] = (5.0, 60.0)
DEFAULT_BACKOFF_S = 60.0
SEVERITY_MAP = {"high": "High", "medium": "Medium", "low": "Low"}
_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def is_blockable_ip(value: str) -> bool:
    """True if ``value`` is a single, globally routable unicast IP address.

    Private, loopback, link-local, reserved, multicast, CGNAT addresses and
    CIDR ranges (unsupported by the API) are rejected.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return embedded.is_global and not embedded.is_multicast
    return ip.is_global and not ip.is_multicast


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address embedded via NAT64 or IPv4-compatible (``::a.b.c.d``)."""
    if ip in _NAT64 or (ip.packed[:12] == b"\x00" * 12 and int(ip) > 1):
        return ipaddress.IPv4Address(ip.packed[12:])
    return None


class RateLimiter:
    """Sliding-window limiter: ``max_per_minute`` and ``max_per_hour`` calls."""

    def __init__(
        self,
        max_per_minute: int,
        max_per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a limiter."""
        self._min = max_per_minute
        self._hour = max_per_hour
        self._clock = clock
        self._calls: deque[float] = deque()

    def try_acquire(self) -> bool:
        """Record a call and return True if allowed, else return False."""
        now = self._clock()
        while self._calls and now - self._calls[0] >= 3600:
            self._calls.popleft()
        recent = sum(1 for t in self._calls if now - t < 60)
        if recent >= self._min or len(self._calls) >= self._hour:
            return False
        self._calls.append(now)
        return True


class MDESink:
    """Submits IP indicators for confirmed high-severity block findings."""

    def __init__(
        self,
        config: MDEConfig,
        client_secret: str,
        data_dir: Path,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        limiter: RateLimiter | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        """Create the sink.

        Args:
            config: MDE configuration.
            client_secret: Entra app client secret.
            data_dir: ``OberConfig.watch_data_dir``; ``mde_state.json`` lives there.
            session: Optional ``requests.Session`` (injected in tests).
            clock: Wall clock (epoch seconds).
            limiter: Optional rate limiter (injected in tests).
            token_provider: Optional token provider (injected in tests).
        """
        self._cfg = config
        self._session = session or requests.Session()
        self._clock = clock
        self._limiter = limiter or RateLimiter(MAX_CALLS_PER_MINUTE, MAX_CALLS_PER_HOUR)
        self._auth = token_provider or TokenProvider(
            config.tenant_id, config.client_id, client_secret, SCOPE, session=self._session
        )
        self._state_path = Path(data_dir) / "mde_state.json"
        self._lock = threading.Lock()
        self._submitted: dict[str, float] = self._load_state()
        self._pending: dict[str, Finding] = {}
        self._backoff_until = 0.0
        self.skipped_cap = 0

    # -- public API -------------------------------------------------------

    def send_findings(self, findings: list[Finding]) -> None:
        """Queue eligible findings and submit them. Never raises."""
        try:
            with self._lock:
                self._enqueue(findings)
                self._flush()
        except Exception:  # noqa: BLE001 - sinks must never raise
            logger.exception("MDE sink error")

    def send_summaries(self, summaries: list[KeySummary]) -> None:  # noqa: ARG002
        """Retry pending indicators (called every summary interval); never raises."""
        try:
            with self._lock:
                if self._pending:
                    self._flush()
        except Exception:  # noqa: BLE001
            logger.exception("MDE sink error")

    def close(self) -> None:
        """Persist state and close the HTTP session."""
        try:
            with self._lock:
                if self._pending:
                    self._flush()
                self._save_state()
            self._session.close()
        except Exception:  # noqa: BLE001
            logger.debug("MDE close failed", exc_info=True)

    @property
    def pending(self) -> int:
        """Number of IPs waiting for submission."""
        return len(self._pending)

    # -- filtering --------------------------------------------------------

    def eligible(self, f: Finding) -> bool:
        """True if the finding should become an indicator (ignoring dedup)."""
        return (
            f.action == "block"
            and severity_rank(f.severity) >= severity_rank(self._cfg.min_severity) >= 0
            and is_blockable_ip(f.client_ip)
        )

    def _enqueue(self, findings: list[Finding]) -> None:
        now = self._clock()
        for f in findings:
            if not self.eligible(f):
                continue
            if self._submitted.get(f.client_ip, 0.0) > now or f.client_ip in self._pending:
                continue
            active = sum(1 for v in self._submitted.values() if v > now)
            if active + len(self._pending) >= self._cfg.max_active_indicators:
                self.skipped_cap += 1
                logger.warning(
                    "MDE max_active_indicators (%d) reached; skipping %s (%d skipped)",
                    self._cfg.max_active_indicators,
                    f.client_ip,
                    self.skipped_cap,
                )
                continue
            if len(self._pending) >= MAX_PENDING:
                logger.warning("MDE pending queue full; dropping %s", f.client_ip)
                continue
            self._pending[f.client_ip] = f

    # -- submission -------------------------------------------------------

    def _indicator(self, f: Finding, now: float) -> dict[str, Any]:
        rule = _SAFE.sub("_", f.rule)[:64]
        expiry = datetime.fromtimestamp(now, UTC) + timedelta(days=self._cfg.expiry_days)
        return {
            "indicatorValue": f.client_ip,
            "indicatorType": "IpAddress",
            "action": self._cfg.action,
            "title": f"ober: {rule}",
            "description": (
                f"Herr Ober rule {rule} ({f.severity}) matched {f.count} events "
                f"in a {f.window_s}s window."
            ),
            "severity": SEVERITY_MAP.get(f.severity, "Informational"),
            "expirationTime": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generateAlert": True,
            "application": "Herr Ober",
        }

    def _flush(self) -> None:
        while self._pending:
            now = self._clock()
            if now < self._backoff_until:
                return
            if not self._limiter.try_acquire():
                logger.info(
                    "MDE client-side rate limit reached; %d IPs pending", len(self._pending)
                )
                return
            ips = list(self._pending)[:BATCH_SIZE]
            indicators = [self._indicator(self._pending[ip], now) for ip in ips]
            ok_ips = self._submit(ips, indicators)
            if ok_ips is None:
                return
            expires = now + self._cfg.expiry_days * 86400
            for ip in ips:
                del self._pending[ip]
                if ip in ok_ips:
                    self._submitted[ip] = expires
            self._save_state()

    def _submit(self, ips: list[str], indicators: list[dict[str, Any]]) -> set[str] | None:
        """POST a batch; return the accepted IPs, or None to retry later."""
        url = self._cfg.api_base.rstrip("/") + BATCH_PATH
        try:
            token = self._auth.get_token()
            resp = self._session.post(
                url,
                json={"Indicators": indicators},
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
        except AuthError as e:
            logger.warning("MDE auth failed: %s", e)
            self._backoff_until = self._clock() + DEFAULT_BACKOFF_S
            return None
        except requests.RequestException as e:
            logger.warning("MDE request failed: %s", type(e).__name__)
            self._backoff_until = self._clock() + DEFAULT_BACKOFF_S
            return None
        code = resp.status_code
        if code == 401:
            self._auth.invalidate()
        if code == 401 or code == 429 or code >= 500:
            wait = DEFAULT_BACKOFF_S
            with contextlib.suppress(ValueError):
                wait = float(resp.headers.get("Retry-After", wait))
            wait = max(0.0, min(wait, MAX_RETRY_AFTER_S))
            self._backoff_until = self._clock() + wait
            logger.warning("MDE returned HTTP %d; retrying in %.0fs", code, wait)
            return None
        if code != 200:
            # 400 means the body was bad; retrying would loop forever, so drop the batch.
            logger.error("MDE rejected batch with HTTP %d: %s", code, resp.text[:300])
            return set()
        try:
            results = resp.json().get("value", [])
            failed = {r.get("indicator") for r in results if r.get("isFailed")}
            for r in results:
                if r.get("isFailed"):
                    logger.warning(
                        "MDE indicator %s failed: %s", r.get("indicator"), r.get("failureReason")
                    )
            return {ip for ip in ips if ip not in failed}
        except (ValueError, AttributeError):
            return set(ips)

    # -- state ------------------------------------------------------------

    def _load_state(self) -> dict[str, float]:
        try:
            data = json.loads(self._state_path.read_text())
            now = time.time()
            return {str(k): float(v) for k, v in data.items() if float(v) > now}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_state(self) -> None:
        now = self._clock()
        live = {k: v for k, v in self._submitted.items() if v > now}
        self._submitted = live
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(live))
            os.replace(tmp, self._state_path)
        except OSError:
            logger.exception("cannot write MDE state")
