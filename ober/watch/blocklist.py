#!/usr/bin/env python3
"""Thread-safe blocklist with TTLs, repeat-offender doubling and atomic persistence."""

import ipaddress
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ober.watch.models import BlockEntry, Finding

logger = logging.getLogger(__name__)

MIN_PREFIX_V4 = 8
MIN_PREFIX_V6 = 16
HISTORY_MAX = 100_000


def normalize_target(target: str) -> str:
    """Validate an IP or CIDR and return its canonical text.

    Raises:
        ValueError: if the target is not an IP/CIDR, or the network is too broad.
    """
    text = target.strip()
    if not text or len(text) > 64:
        raise ValueError("invalid target")
    if "/" in text:
        net = ipaddress.ip_network(text, strict=False)
        minimum = MIN_PREFIX_V4 if net.version == 4 else MIN_PREFIX_V6
        if net.prefixlen < minimum:
            raise ValueError(f"network too broad (minimum /{minimum})")
        if net.num_addresses == 1:
            return str(net.network_address)
        return str(net)
    return str(ipaddress.ip_address(text))


class BlockList:
    """Active blocks plus per-target offense history."""

    def __init__(
        self,
        path: Path,
        ttl_s: int = 3600,
        ttl_max_s: int = 604800,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s
        self.ttl_max_s = ttl_max_s
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, BlockEntry] = {}
        self._history: dict[str, int] = {}
        self._last_offense: dict[str, float] = {}
        self._io_lock = threading.Lock()
        self._dirty = False
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        """Load state from disk; a missing or corrupt file yields an empty list."""
        with self._lock:
            try:
                data = json.loads(self.path.read_text())
                entries = {}
                for item in data.get("entries", []):
                    entry = BlockEntry(**item)
                    entries[entry.target] = entry
                history = {str(k): int(v) for k, v in data.get("history", {}).items()}
                loaded_at = self._clock()
                last = {
                    str(k): float(v)
                    for k, v in data.get("last_offense", {}).items()
                    if str(k) in history
                }
                for key in history:
                    last.setdefault(key, loaded_at)  # state from before decay existed
            except FileNotFoundError:
                return
            except (OSError, ValueError, TypeError, AttributeError):
                logger.warning("Ignoring unreadable blocklist state %s", self.path)
                return
            self._entries = entries
            self._history = history
            self._last_offense = last

    def save(self) -> None:
        """Atomically persist state (tmp file + rename).

        The state is snapshotted under the data lock; the slow write and fsync run
        under a separate lock so they never block readers or writers of the list.
        """
        with self._lock:
            payload = {
                "entries": [e.to_dict() for e in self._entries.values()],
                "history": dict(self._history),
                "last_offense": dict(self._last_offense),
            }
            self._dirty = False
        try:
            with self._io_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                with open(tmp, "w") as fh:
                    json.dump(payload, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
        except BaseException:
            with self._lock:
                self._dirty = True
            raise

    def flush(self) -> None:
        """Persist only if something changed since the last save."""
        with self._lock:
            dirty = self._dirty
        if dirty:
            self.save()

    # -- mutation ----------------------------------------------------------

    def _purge_locked(self) -> int:
        now = self._clock()
        expired = [t for t, e in self._entries.items() if e.expires_at and e.expires_at <= now]
        for target in expired:
            del self._entries[target]
        if expired:
            self._dirty = True
        return len(expired)

    def purge_expired(self) -> int:
        """Remove expired entries; returns how many were removed."""
        with self._lock:
            return self._purge_locked()

    def add_from_finding(self, finding: Finding) -> BlockEntry | None:
        """Block the finding's client IP. Returns the new entry, or None if not added.

        Only single IP addresses are accepted: a finding is derived from unauthenticated
        log data and must never be able to block a network. If the IP's last offense is
        older than ``ttl_max_s`` the repeat count starts over at 1.

        An already-active block is left unchanged (requests may still be in flight
        until proxies sync, which must not count as a repeat offense).
        """
        try:
            target = str(ipaddress.ip_address(finding.client_ip.strip()))
        except ValueError:
            return None
        with self._lock:
            self._purge_locked()
            if target in self._entries:
                return None
            now = self._clock()
            previous = self._history.get(target, 0)
            if previous and now - self._last_offense.get(target, now) > self.ttl_max_s:
                previous = 0
            offenses = previous + 1
            ttl = min(self.ttl_s * (2 ** min(offenses - 1, 30)), self.ttl_max_s)
            entry = BlockEntry(
                target=target,
                reason=f"{finding.rule}: {finding.detail}"[:256],
                expires_at=now + ttl,
                source="auto",
                finding_id=finding.id,
                offenses=offenses,
            )
            self._entries[target] = entry
            self._history.pop(target, None)
            self._history[target] = offenses
            self._last_offense[target] = now
            while len(self._history) > HISTORY_MAX:
                oldest = next(iter(self._history))
                del self._history[oldest]
                self._last_offense.pop(oldest, None)
            self._dirty = True
            return entry

    def add_manual(self, target: str, reason: str = "", ttl_s: int | None = None) -> BlockEntry:
        """Add or replace a manual block; ttl_s None or 0 means never expires.

        Raises:
            ValueError: for an invalid target.
        """
        norm = normalize_target(target)
        with self._lock:
            expires = self._clock() + ttl_s if ttl_s and ttl_s > 0 else 0.0
            entry = BlockEntry(
                target=norm,
                reason=reason[:256] or "manual",
                expires_at=expires,
                source="manual",
                offenses=self._entries[norm].offenses if norm in self._entries else 1,
            )
            self._entries[norm] = entry
        self.save()
        return entry

    def remove(self, target: str) -> bool:
        """Remove a block (and forget its offense history). Returns True if it existed."""
        norm = normalize_target(target)
        with self._lock:
            existed = self._entries.pop(norm, None) is not None
            self._history.pop(norm, None)
            self._last_offense.pop(norm, None)
        if existed:
            self.save()
        return existed

    def active(self) -> list[BlockEntry]:
        """Return current (non-expired) entries."""
        with self._lock:
            self._purge_locked()
            return list(self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
