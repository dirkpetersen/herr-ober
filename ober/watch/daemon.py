#!/usr/bin/env python3
"""The ober watch daemon: UDP syslog in, findings and blocklist out.

Receive path (event loop thread): parse, rules, summaries, enqueue. Anything that
can block (disk, network sinks) runs on worker threads behind bounded queues.
"""

import asyncio
import contextlib
import importlib
import ipaddress
import logging
import queue
import signal
import socket
import threading
import time
from typing import Any, Protocol

from ober.config import OberConfig, load_secrets
from ober.watch.blocklist import BlockList
from ober.watch.models import HEARTBEAT_KEY, Finding, KeySummary
from ober.watch.parser import LogParser
from ober.watch.rules import RuleEngine
from ober.watch.server import WatchAPI, split_listen
from ober.watch.store import Store
from ober.watch.summaries import SummaryAggregator

logger = logging.getLogger(__name__)

RCVBUF = 32 * 1024 * 1024
SINK_QUEUE_SIZE = 10_000


class FindingSink(Protocol):
    """Destination for findings and summaries (Sentinel, Defender, ...)."""

    def send_findings(self, findings: list[Finding]) -> None: ...

    def send_summaries(self, summaries: list[KeySummary]) -> None: ...

    def close(self) -> None: ...


def _load_sinks(config: OberConfig, secrets: dict[str, str]) -> list[FindingSink]:
    """Build sinks via ``ober.watch.integrations.build_sinks`` (tests mock this)."""
    try:
        module = importlib.import_module("ober.watch.integrations")
    except ImportError:
        logger.warning("ober.watch.integrations not available; no sinks configured")
        return []
    sinks: list[FindingSink] = module.build_sinks(config, secrets)
    return sinks


class SinkWorker:
    """Runs sinks on one thread behind a bounded queue; never blocks the producer."""

    def __init__(self, sinks: list[FindingSink], maxsize: int = SINK_QUEUE_SIZE) -> None:
        self.sinks = sinks
        self.dropped = 0
        self.errors = 0
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="ober-watch-sinks", daemon=True)

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()

    def submit(self, kind: str, items: list[Any]) -> None:
        """Queue findings ('f') or summaries ('s'); drops when full."""
        if not self.sinks or not items:
            return
        try:
            self._queue.put_nowait((kind, items))
        except queue.Full:
            self.dropped += len(items)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, items = item
            for sink in self.sinks:
                try:
                    if kind == "f":
                        sink.send_findings(items)
                    else:
                        sink.send_summaries(items)
                except Exception:
                    self.errors += 1
                    logger.exception("Sink %s failed", type(sink).__name__)

    def close(self, timeout: float = 15.0) -> None:
        """Drain the queue, stop the thread and close all sinks."""
        if self._thread.is_alive():
            with contextlib.suppress(queue.Full):
                self._queue.put(None, timeout=timeout)
            self._thread.join(timeout)
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                logger.exception("Closing sink %s failed", type(sink).__name__)


def kernel_udp_drops(port: int) -> int | None:
    """Datagrams the kernel dropped on UDP sockets bound to ``port`` (Linux), else None."""
    if port <= 0:
        return None
    total = 0
    found = False
    for name in ("udp", "udp6"):
        try:
            with open(f"/proc/net/{name}") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        found = True
        for line in lines:
            fields = line.split()
            try:
                if int(fields[1].rsplit(":", 1)[1], 16) == port:
                    total += int(fields[-1])
            except (IndexError, ValueError):
                continue
    return total if found else None


def parse_sources(items: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse the syslog source allowlist once; invalid entries are logged and skipped."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid syslog_allowed_sources entry %r", item[:64])
    return nets


class _Protocol(asyncio.DatagramProtocol):
    """asyncio UDP protocol delegating to the daemon."""

    def __init__(self, daemon: "WatchDaemon") -> None:
        self.daemon = daemon

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.daemon.handle_datagram(data, addr[0] if addr else None)


class WatchDaemon:
    """Wires receiver, analysis, storage, blocklist, API and sinks together."""

    def __init__(
        self,
        config: OberConfig,
        secrets: dict[str, str] | None = None,
        store_raw: bool = True,
    ) -> None:
        self.config = config
        self.secrets = load_secrets() if secrets is None else secrets
        w = config.watch
        data_dir = config.watch_data_dir
        self.parser = LogParser()
        self.rules = RuleEngine(config)
        self.summaries = SummaryAggregator(w.summary_interval_s)
        self.store = Store(data_dir, w.raw_log_retention_days, store_raw=store_raw)
        self.blocklist = BlockList(data_dir / "blocklist.json", w.block_ttl_s, w.block_ttl_max_s)
        self.sinks = SinkWorker([])
        self.findings = 0
        self.blocks = 0
        self.rejected_sources = 0
        self._sources = parse_sources(w.syslog_allowed_sources)
        self._source_ok: dict[str, bool] = {}
        self.api: WatchAPI | None = None
        self.syslog_port = 0
        self.started_at = 0.0
        self._transport: asyncio.DatagramTransport | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._hb_start = 0.0
        self._hb_parsed = 0

    # -- hot path ----------------------------------------------------------

    def _source_allowed(self, source: str) -> bool:
        """Check the sender address against ``syslog_allowed_sources`` (cached)."""
        cached = self._source_ok.get(source)
        if cached is not None:
            return cached
        try:
            addr = ipaddress.ip_address(source.split("%", 1)[0])
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            ok = any(addr.version == net.version and addr in net for net in self._sources)
        except ValueError:
            ok = False
        if len(self._source_ok) >= 4096:
            self._source_ok.clear()
        self._source_ok[source] = ok
        return ok

    def handle_datagram(self, data: bytes, source: str | None = None) -> None:
        """Process one datagram. Must stay O(1) and never block.

        UDP syslog is unauthenticated, so when ``syslog_allowed_sources`` is configured a
        datagram from any other sender address is dropped and counted.
        """
        if (
            source is not None
            and self.config.watch.syslog_allowed_sources
            and not self._source_allowed(source)
        ):
            self.rejected_sources += 1
            return
        rec = self.parser.parse(data)
        if rec is None:
            return
        self.store.write_raw(rec)
        found = self.rules.process(rec)
        if found:
            self._handle_findings(found)
        closed = self.summaries.add(rec)
        if closed:
            self._emit_summaries(closed)

    def _handle_findings(self, found: list[Finding]) -> None:
        for finding in found:
            self.findings += 1
            self.store.write_finding(finding)
            if finding.action == "block" and self.blocklist.add_from_finding(finding):
                self.blocks += 1
        self.sinks.submit("f", found)

    def _emit_summaries(self, closed: list[KeySummary]) -> None:
        for summary in closed:
            self.store.write_summary(summary)
        self.sinks.submit("s", closed)

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counters served by /v1/health."""
        return {
            "status": "ok",
            "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
            "received": self.parser.parsed + self.parser.malformed,
            "parsed": self.parser.parsed,
            "malformed": self.parser.malformed,
            "rejected_sources": self.rejected_sources,
            "udp_kernel_drops": kernel_udp_drops(self.syslog_port),
            "dropped": self.store.dropped_total + self.sinks.dropped,
            "store_dropped": dict(self.store.dropped),
            "sink_dropped": self.sinks.dropped,
            "sink_errors": self.sinks.errors,
            "findings": self.findings,
            "blocks": self.blocks,
            "active_blocks": len(self.blocklist),
        }

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start store, sinks, API and the UDP receiver."""
        self.started_at = time.time()
        self.config.watch_data_dir.mkdir(parents=True, exist_ok=True)
        self.store.start()
        self.sinks = SinkWorker(_load_sinks(self.config, self.secrets))
        self.sinks.start()
        if not self.config.watch.syslog_allowed_sources:
            logger.warning(
                "watch.syslog_allowed_sources is empty: accepting syslog from ANY sender. "
                "UDP is unauthenticated, so anyone who can reach udp/%s can forge logs. "
                "Set it to the proxy node addresses.",
                self.config.watch.syslog_listen,
            )
        elif not self._sources:
            logger.error("watch.syslog_allowed_sources has no valid entry: all syslog rejected")
        token = self.secrets.get("OBER_WATCH_TOKEN", "")
        admin_token = self.secrets.get("OBER_WATCH_ADMIN_TOKEN", "")
        if not token and not admin_token:
            logger.warning("OBER_WATCH_TOKEN not set: blocklist API will reject all requests")
        if not admin_token:
            logger.warning("OBER_WATCH_ADMIN_TOKEN not set: blocklist writes are disabled")
        self.api = WatchAPI(
            self.config.watch.api_listen, self.blocklist, token, self.stats, admin_token
        )
        self.api.start()

        host, port = split_listen(self.config.watch.syslog_listen)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        except OSError:
            logger.warning("Could not enlarge UDP receive buffer")
        sock.bind((host, port))
        sock.setblocking(False)
        self.syslog_port = int(sock.getsockname()[1])
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        self._transport = transport  # type: ignore[assignment]
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.tick(flush=False)
            # fsync can take long: keep it off the event loop that receives datagrams
            await asyncio.get_running_loop().run_in_executor(None, self._flush_blocklist)

    def _flush_blocklist(self) -> None:
        try:
            self.blocklist.flush()
        except OSError:
            logger.exception("Could not persist blocklist")

    def tick(self, flush: bool = True) -> None:
        """Periodic housekeeping: close idle summary windows, persist the blocklist.

        The event loop passes ``flush=False`` and flushes in an executor instead.
        """
        now = time.time()
        closed = self.summaries.tick(now)
        if closed:
            self._emit_summaries(closed)
        self._heartbeat(now)
        self.blocklist.purge_expired()
        if flush:
            self._flush_blocklist()

    def _heartbeat(self, now: float) -> None:
        """Emit one heartbeat summary per interval, even when no traffic arrives."""
        if not self._hb_start:
            self._hb_start, self._hb_parsed = now, self.parser.parsed
            return
        if now - self._hb_start < self.config.watch.summary_interval_s:
            return
        hb = KeySummary(
            window_start=self._hb_start,
            window_s=int(now - self._hb_start),
            access_key=HEARTBEAT_KEY,
            requests=self.parser.parsed - self._hb_parsed,
            nodes=[socket.gethostname()],
        )
        self._hb_start, self._hb_parsed = now, self.parser.parsed
        self._emit_summaries([hb])

    async def stop(self) -> None:
        """Graceful shutdown: stop receiving, flush everything, close sinks."""
        if self._tick_task is not None:
            self._tick_task.cancel()
        if self._transport is not None:
            self._transport.close()
        closed = self.summaries.flush()
        if closed:
            self._emit_summaries(closed)
        if self.api is not None:
            self.api.stop()
        self.store.flush()
        self.store.close()
        try:
            self.blocklist.save()
        except OSError:
            logger.exception("Could not persist blocklist")
        self.sinks.close()

    async def _main(self) -> None:
        await self.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("ober watch listening on udp/%d", self.syslog_port)
        await stop.wait()
        logger.info("Shutting down")
        await self.stop()

    def run(self) -> None:
        """Run in the foreground until SIGTERM/SIGINT."""
        asyncio.run(self._main())
