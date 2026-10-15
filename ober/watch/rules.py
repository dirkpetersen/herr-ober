#!/usr/bin/env python3
"""Layer 1 streaming detection rules with bounded memory.

Every rule keeps per-key state in an LRU-bounded ``OrderedDict`` so that a
flood of distinct source addresses cannot exhaust memory. All per-record work
is O(1) amortised. Event time (``LogRecord.ts``) drives the windows.
"""

import ipaddress
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ober.config import OberConfig
from ober.watch.models import Finding, LogRecord

logger = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

SCANNER_TOKENS: tuple[str, ...] = (
    "sqlmap",
    "nikto",
    "nmap",
    "masscan",
    "zgrab",
    "nuclei",
    "gobuster",
    "dirbuster",
    "dirb/",
    "wpscan",
    "acunetix",
    "nessus",
    "openvas",
    "burpsuite",
    "havij",
    "wfuzz",
    "ffuf",
    "feroxbuster",
    "hydra",
    "metasploit",
    "netsparker",
)
_SCANNER_RE = re.compile("|".join(re.escape(t) for t in SCANNER_TOKENS), re.IGNORECASE)

# S3 bucket naming: 3-63 chars, lowercase letters, digits, dots, hyphens.
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]")
_SAFE_RE = re.compile(r"[^\x20-\x7e]|[<>\"'`\\]")

# Names that are valid S3 syntax but are classic web-scanner probes.
PROBE_BUCKETS = frozenset(
    {
        "wp-admin",
        "wp-login",
        "wp-content",
        "wp-includes",
        "phpmyadmin",
        "cgi-bin",
        "server-status",
        "actuator",
        "xmlrpc",
    }
)

ENUM_THRESHOLD = 20
ENUM_WINDOW_S = 300
ANON_THRESHOLD = 20
ANON_WINDOW_S = 300
COOLDOWN_S = 300

DEFAULT_ACTIONS: dict[str, str] = {
    "auth_failure_burst": "block",
    "scanner_user_agent": "block",
    "admin_probe": "block",
    "bucket_enumeration": "none",
    "anonymous_probe": "none",
    "invalid_bucket_probe": "none",
}


def sanitize(value: str, max_len: int = 64) -> str:
    """Reduce attacker-controlled text to short printable ASCII."""
    return _SAFE_RE.sub("?", value[:max_len])


def is_valid_bucket_name(name: str) -> bool:
    """Return True if name follows S3 bucket naming rules."""
    return bool(_BUCKET_RE.fullmatch(name)) and ".." not in name


def parse_network_file(path: Path) -> list[IPNetwork]:
    """Read CIDRs/IPs, one per line (``#`` comments allowed). Bad lines are skipped."""
    networks: list[IPNetwork] = []
    try:
        text = path.read_text()
    except OSError:
        return networks
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            networks.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid network %r in %s", line[:64], path)
    return networks


class NetworkSet:
    """CIDR membership that reloads its files when their mtime changes."""

    def __init__(
        self,
        paths: Iterable[Path],
        static: Iterable[str] = (),
        reload_interval_s: float = 10.0,
    ) -> None:
        self._paths = list(paths)
        self._static: list[IPNetwork] = []
        for item in static:
            try:
                self._static.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                logger.warning("Ignoring invalid network %r in config", item[:64])
        self._interval = reload_interval_s
        self._checked = float("-inf")
        self._mtimes: dict[Path, float] = {}
        self._loaded: list[IPNetwork] = []
        self._maybe_reload(force=True)

    def _maybe_reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked < self._interval:
            return
        self._checked = now
        mtimes: dict[Path, float] = {}
        for path in self._paths:
            try:
                mtimes[path] = path.stat().st_mtime
            except OSError:
                mtimes[path] = 0.0
        if not force and mtimes == self._mtimes:
            return
        self._mtimes = mtimes
        loaded: list[IPNetwork] = []
        for path in self._paths:
            loaded.extend(parse_network_file(path))
        self._loaded = loaded

    def __contains__(self, ip: object) -> bool:
        if not isinstance(ip, str):
            return False
        self._maybe_reload()
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(
            addr.version == net.version and addr in net for net in (*self._static, *self._loaded)
        )


class RuleEngine:
    """Evaluate L1 rules against a stream of log records."""

    def __init__(
        self,
        config: OberConfig,
        actions: dict[str, str] | None = None,
        max_keys: int = 100_000,
        network_reload_s: float = 10.0,
    ) -> None:
        self._sec = config.security
        self._actions = dict(DEFAULT_ACTIONS)
        if actions:
            self._actions.update(actions)
        self._max_keys = max_keys
        self._exempt = NetworkSet(
            # Trusted only, matching HAProxy's inline auto-ban: the allowlist grants
            # access, not immunity (in phase 1 every Internet client is allowlisted).
            [config.trusted_path],
            [*self._sec.trusted_networks],
            network_reload_s,
        )
        self._auth: OrderedDict[str, deque[float]] = OrderedDict()
        self._enum: OrderedDict[str, OrderedDict[str, float]] = OrderedDict()
        self._anon: OrderedDict[str, deque[float]] = OrderedDict()
        self._cooldown: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._auth_threshold = max(1, self._sec.autoban_auth_failures)
        self._auth_window = max(1, self._sec.autoban_window_s)

    # -- internal helpers -------------------------------------------------

    def _bound(self, table: OrderedDict[Any, Any]) -> None:
        while len(table) > self._max_keys:
            table.popitem(last=False)

    def _cooled(self, rule: str, ip: str, now: float, window: float) -> bool:
        """Return True (and arm the cooldown) if a finding may fire now."""
        key = (rule, ip)
        last = self._cooldown.get(key)
        if last is not None and 0 <= now - last < window:
            return False
        self._cooldown[key] = now
        self._cooldown.move_to_end(key)
        self._bound(self._cooldown)
        return True

    def _make(
        self,
        rule: str,
        severity: str,
        rec: LogRecord,
        count: int,
        window_s: int,
        detail: str,
    ) -> Finding:
        action = self._actions.get(rule, "none")
        if rule == "auth_failure_burst" and not self._sec.autoban_enabled:
            action = "none"
        if action == "block" and rec.client_ip in self._exempt:
            action = "none"
            detail += " (trusted network: not blocked)"
        return Finding(
            id=uuid.uuid4().hex,
            ts=rec.ts,
            rule=rule,
            severity=severity,
            client_ip=rec.client_ip,
            access_key=sanitize(rec.access_key, 128),
            bucket=sanitize(rec.bucket, 64),
            node=sanitize(rec.node, 64),
            count=count,
            window_s=window_s,
            detail=detail,
            action=action,
        )

    # -- evaluation --------------------------------------------------------

    def process(self, rec: LogRecord) -> list[Finding]:
        """Evaluate one record; returns zero or more new findings."""
        out: list[Finding] = []
        ip = rec.client_ip
        now = rec.ts
        status = rec.status

        match = _SCANNER_RE.search(rec.user_agent)
        if match:
            token = match.group(0).lower()
            if self._cooled("scanner_user_agent", ip, now, COOLDOWN_S):
                out.append(
                    self._make(
                        "scanner_user_agent",
                        "high",
                        rec,
                        1,
                        COOLDOWN_S,
                        f"User agent contains known scanner token '{token}'",
                    )
                )

        if rec.frontend == "s3_internet" and rec.bucket:
            if rec.bucket == "admin":
                if self._cooled("admin_probe", ip, now, COOLDOWN_S):
                    out.append(
                        self._make(
                            "admin_probe",
                            "high",
                            rec,
                            1,
                            COOLDOWN_S,
                            "Request for the RGW admin bucket on an internet frontend",
                        )
                    )
            elif (
                rec.bucket in PROBE_BUCKETS or not is_valid_bucket_name(rec.bucket)
            ) and self._cooled("invalid_bucket_probe", ip, now, COOLDOWN_S):
                out.append(
                    self._make(
                        "invalid_bucket_probe",
                        "medium",
                        rec,
                        1,
                        COOLDOWN_S,
                        "Bucket name violates S3 naming rules on an internet frontend",
                    )
                )

        if status == 401 or status == 403:
            out.extend(self._auth_failure(rec, ip, now))
            if not rec.access_key and status == 403:
                out.extend(self._anonymous(rec, ip, now))

        if (status == 403 or status == 404) and rec.bucket:
            out.extend(self._enumeration(rec, ip, now))
        return out

    def _auth_failure(self, rec: LogRecord, ip: str, now: float) -> list[Finding]:
        events = self._auth.get(ip)
        if events is None:
            events = deque(maxlen=self._auth_threshold)
            self._auth[ip] = events
            self._bound(self._auth)
        else:
            self._auth.move_to_end(ip)
        events.append(now)
        if (
            len(events) >= self._auth_threshold
            and now - events[0] <= self._auth_window
            and self._cooled("auth_failure_burst", ip, now, self._auth_window)
        ):
            return [
                self._make(
                    "auth_failure_burst",
                    "high",
                    rec,
                    len(events),
                    self._auth_window,
                    f"{len(events)} authentication failures (401/403) within "
                    f"{self._auth_window}s from one client",
                )
            ]
        return []

    def _anonymous(self, rec: LogRecord, ip: str, now: float) -> list[Finding]:
        events = self._anon.get(ip)
        if events is None:
            events = deque(maxlen=ANON_THRESHOLD)
            self._anon[ip] = events
            self._bound(self._anon)
        else:
            self._anon.move_to_end(ip)
        events.append(now)
        if (
            len(events) >= ANON_THRESHOLD
            and now - events[0] <= ANON_WINDOW_S
            and self._cooled("anonymous_probe", ip, now, ANON_WINDOW_S)
        ):
            return [
                self._make(
                    "anonymous_probe",
                    "medium",
                    rec,
                    len(events),
                    ANON_WINDOW_S,
                    f"{len(events)} unsigned requests denied (403) within {ANON_WINDOW_S}s",
                )
            ]
        return []

    def _enumeration(self, rec: LogRecord, ip: str, now: float) -> list[Finding]:
        seen = self._enum.get(ip)
        if seen is None:
            seen = OrderedDict()
            self._enum[ip] = seen
            self._bound(self._enum)
        else:
            self._enum.move_to_end(ip)
        seen[rec.bucket] = now
        seen.move_to_end(rec.bucket)
        cutoff = now - ENUM_WINDOW_S
        while seen:
            oldest = next(iter(seen.values()))
            if oldest >= cutoff and len(seen) <= 1000:
                break
            seen.popitem(last=False)
        if len(seen) >= ENUM_THRESHOLD and self._cooled(
            "bucket_enumeration", ip, now, ENUM_WINDOW_S
        ):
            return [
                self._make(
                    "bucket_enumeration",
                    "medium",
                    rec,
                    len(seen),
                    ENUM_WINDOW_S,
                    f"{len(seen)} distinct buckets answered 403/404 within {ENUM_WINDOW_S}s",
                )
            ]
        return []
