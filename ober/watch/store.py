#!/usr/bin/env python3
"""Batched JSONL persistence for raw logs, findings and summaries.

Producers (the asyncio receive path) only enqueue; a single writer thread
serializes and appends in batches, so the receive path never touches disk.
When the bounded queue is full the item is dropped and counted.
"""

import contextlib
import datetime as dt
import gzip
import json
import logging
import queue
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ober.watch.models import Finding, KeySummary, LogRecord

logger = logging.getLogger(__name__)

KINDS = ("raw", "findings", "summaries")
_BATCH_MAX = 5000
_STOP = ("stop", None)


class Store:
    """Daily-rotated JSONL store with a writer thread."""

    def __init__(
        self,
        data_dir: Path,
        raw_retention_days: int = 30,
        queue_size: int = 200_000,
        clock: Callable[[], float] = time.time,
        store_raw: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.raw_retention_days = raw_retention_days
        self.store_raw = store_raw
        self._clock = clock
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=queue_size)
        self.written: dict[str, int] = dict.fromkeys(KINDS, 0)
        self.dropped: dict[str, int] = dict.fromkeys(KINDS, 0)
        self._handles: dict[str, Any] = {}
        self._day = ""
        self._thread: threading.Thread | None = None
        self._maint_lock = threading.Lock()
        self._maint_thread: threading.Thread | None = None

    # -- producer side -----------------------------------------------------

    @property
    def dropped_total(self) -> int:
        """Total items dropped because the queue was full."""
        return sum(self.dropped.values())

    def _put(self, kind: str, obj: Any) -> None:
        try:
            self._queue.put_nowait((kind, obj))
        except queue.Full:
            self.dropped[kind] += 1

    def write_raw(self, rec: LogRecord) -> None:
        """Queue a raw log record (no-op if raw storage is disabled)."""
        if self.store_raw:
            self._put("raw", rec)

    def write_finding(self, finding: Finding) -> None:
        """Queue a finding."""
        self._put("findings", finding)

    def write_summary(self, summary: KeySummary) -> None:
        """Queue a key summary."""
        self._put("summaries", summary)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Create directories, run maintenance, start the writer thread."""
        for kind in KINDS:
            (self.data_dir / kind).mkdir(parents=True, exist_ok=True)
        self.compress_old()
        self._thread = threading.Thread(target=self._run, name="ober-watch-store", daemon=True)
        self._thread.start()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until queued items are written. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True

    def close(self, timeout: float = 10.0) -> None:
        """Flush, stop the writer and close files."""
        if self._thread is not None and self._thread.is_alive():
            with contextlib.suppress(queue.Full):
                self._queue.put(_STOP, timeout=timeout)
            self._thread.join(timeout)
        self._close_handles()
        if self._maint_thread is not None:
            self._maint_thread.join(timeout)

    # -- writer thread -----------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._roll(self._today())
                continue
            batch = [first]
            try:
                while len(batch) < _BATCH_MAX:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            stop = False
            try:
                stop = self._write_batch(batch)
            except Exception:
                logger.exception("Store write failed")
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stop:
                return

    def _today(self) -> str:
        return dt.datetime.fromtimestamp(self._clock(), dt.UTC).strftime("%Y-%m-%d")

    def _roll(self, day: str) -> None:
        """Switch to a new day: close handles and compress/purge in the background."""
        if day == self._day:
            return
        had_previous = bool(self._day)
        self._close_handles()
        self._day = day
        if had_previous:
            self._maint_thread = threading.Thread(
                target=self.compress_old, name="ober-watch-maint", daemon=True
            )
            self._maint_thread.start()

    def _handle(self, kind: str) -> Any:
        handle = self._handles.get(kind)
        if handle is None:
            path = self.data_dir / kind / f"{kind}-{self._day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
            self._handles[kind] = handle
        return handle

    def _write_batch(self, batch: list[tuple[str, Any]]) -> bool:
        self._roll(self._today())
        lines: dict[str, list[str]] = {}
        stop = False
        dumps = json.dumps
        for kind, obj in batch:
            if obj is None:
                stop = True
                continue
            text = dumps(obj.__dict__) if kind == "raw" else dumps(obj.to_dict())
            lines.setdefault(kind, []).append(text)
        for kind, texts in lines.items():
            handle = self._handle(kind)
            handle.write("\n".join(texts) + "\n")
            handle.flush()
            self.written[kind] += len(texts)
        return stop

    def _close_handles(self) -> None:
        for handle in self._handles.values():
            with contextlib.suppress(OSError):
                handle.close()
        self._handles = {}

    # -- maintenance -------------------------------------------------------

    def compress_old(self) -> None:
        """Gzip raw files from before today and delete those past retention."""
        with self._maint_lock:
            today = dt.datetime.fromtimestamp(self._clock(), dt.UTC).date()
            raw_dir = self.data_dir / "raw"
            if not raw_dir.is_dir():
                return
            for path in sorted(raw_dir.glob("raw-*.jsonl")):
                day = _file_day(path.name)
                if day is None or day >= today:
                    continue
                tmp = path.with_name(path.name + ".gz.tmp")
                try:
                    with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    tmp.replace(path.with_name(path.name + ".gz"))
                    path.unlink()
                except OSError:
                    logger.exception("Failed to compress %s", path)
                    tmp.unlink(missing_ok=True)
            cutoff = today - dt.timedelta(days=self.raw_retention_days)
            for path in raw_dir.glob("raw-*.jsonl.gz"):
                day = _file_day(path.name)
                if day is not None and day < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        logger.exception("Failed to delete %s", path)


def _file_day(name: str) -> dt.date | None:
    """Extract the date from ``raw-YYYY-MM-DD.jsonl[.gz]``."""
    try:
        return dt.date.fromisoformat(name.split("-", 1)[1][:10])
    except (IndexError, ValueError):
        return None
