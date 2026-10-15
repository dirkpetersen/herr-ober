"""End-to-end test of the watch daemon, units and throughput."""

import asyncio
import json
import socket
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.config import OberConfig
from ober.watch.daemon import WatchDaemon
from ober.watch.models import Finding, KeySummary
from ober.watch.parser import parse_datagram
from ober.watch.rules import RuleEngine
from ober.watch.units import write_units
from tests.watch_util import syslog_line


class RecordingSink:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.summaries: list[KeySummary] = []
        self.closed = False

    def send_findings(self, findings: list[Finding]) -> None:
        self.findings.extend(findings)

    def send_summaries(self, summaries: list[KeySummary]) -> None:
        self.summaries.extend(summaries)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def config(temp_dir: Path) -> OberConfig:
    cfg = OberConfig(install_path=temp_dir)
    cfg.watch.syslog_listen = "127.0.0.1:0"
    cfg.watch.api_listen = "127.0.0.1:0"
    cfg.watch.summary_interval_s = 60
    cfg.security.autoban_auth_failures = 5
    return cfg


def test_daemon_end_to_end(config: OberConfig) -> None:
    sink = RecordingSink()
    daemon = WatchDaemon(config, secrets={"OBER_WATCH_TOKEN": "tok"})

    async def scenario() -> None:
        with patch("ober.watch.daemon._load_sinks", return_value=[sink]) as loader:
            await daemon.start()
            loader.assert_called_once()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dest = ("127.0.0.1", daemon.syslog_port)
        now = time.time()
        for i in range(5):  # auth failure burst from one IP
            tx.sendto(syslog_line(client_ip="198.51.100.9", status=403, ts=now + i), dest)
        tx.sendto(syslog_line(client_ip="198.51.100.20", user_agent="sqlmap/1.0", ts=now), dest)
        tx.sendto(b"<134>garbage without json", dest)
        tx.sendto(syslog_line(client_ip="192.0.2.77", ts=now), dest)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if daemon.parser.parsed + daemon.parser.malformed >= 8:
                break

        health = await asyncio.to_thread(
            lambda: json.loads(
                urllib.request.urlopen(
                    f"http://127.0.0.1:{daemon.api.port}/v1/health"  # type: ignore[union-attr]
                ).read()
            )
        )
        assert health["malformed"] == 1
        assert health["findings"] == 2 and health["blocks"] == 2
        assert health["received"] == 8
        await daemon.stop()

    asyncio.run(scenario())

    assert {e.target for e in daemon.blocklist.active()} == {"198.51.100.9", "198.51.100.20"}
    persisted = json.loads((config.watch_data_dir / "blocklist.json").read_text())
    assert len(persisted["entries"]) == 2
    assert {f.rule for f in sink.findings} == {"auth_failure_burst", "scanner_user_agent"}
    assert sink.closed
    assert sink.summaries  # flushed at shutdown
    findings_file = next((config.watch_data_dir / "findings").glob("*.jsonl"))
    assert len(findings_file.read_text().splitlines()) == 2
    assert next((config.watch_data_dir / "raw").glob("*.jsonl")).read_text().count("\n") == 7


def test_sink_failure_does_not_break_pipeline(config: OberConfig) -> None:
    class Boom(RecordingSink):
        def send_findings(self, findings: list[Finding]) -> None:  # noqa: ARG002
            raise RuntimeError("network down")

    sink = Boom()
    daemon = WatchDaemon(config, secrets={})

    async def scenario() -> None:
        with patch("ober.watch.daemon._load_sinks", return_value=[sink]):
            await daemon.start()
        daemon.handle_datagram(syslog_line(user_agent="nikto", ts=time.time()))
        await daemon.stop()

    asyncio.run(scenario())
    assert daemon.sinks.errors == 1 and sink.closed and daemon.blocks == 1


def test_units_written(config: OberConfig, temp_dir: Path) -> None:
    out = temp_dir / "units"
    config.watch.sync_interval_s = 45
    paths = write_units(config, out, ober_bin="/opt/ober/venv/bin/ober")
    assert {p.name for p in paths} == {
        "ober-watch.service",
        "ober-blocklist.service",
        "ober-blocklist.timer",
    }
    watch = (out / "ober-watch.service").read_text()
    assert "ExecStart=/opt/ober/venv/bin/ober watch run" in watch
    assert "Nice=10" in watch and f"ReadWritePaths={config.watch_data_dir}" in watch
    assert "ober blocklist sync" in (out / "ober-blocklist.service").read_text()
    assert "OnUnitActiveSec=45s" in (out / "ober-blocklist.timer").read_text()
    assert [p.name for p in write_units(config, temp_dir / "u2", role="proxy")] == [
        "ober-blocklist.service",
        "ober-blocklist.timer",
    ]


def test_watch_status_cli(config: OberConfig) -> None:
    class FakeResp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, int]:
            return {"received": 5, "malformed": 0}

    with (
        patch("ober.commands.watch.OberConfig.load", return_value=config),
        patch("ober.commands.watch.requests.get", return_value=FakeResp()),
    ):
        res = CliRunner().invoke(main, ["--json", "watch", "status"])
    assert res.exit_code == 0 and json.loads(res.output)["received"] == 5


def test_throughput_benchmark(config: OberConfig) -> None:
    """Parse + rules on 100k synthetic lines; loose floor to catch regressions."""
    engine = RuleEngine(config)
    base = time.time()
    lines = [
        syslog_line(
            ts=base + i * 0.001,
            client_ip=f"10.{(i // 65536) % 256}.{(i // 256) % 256}.{i % 256}",
            status=403 if i % 10 == 0 else 200,
            bucket=f"bucket-{i % 500}",
            frontend="s3_internet" if i % 3 == 0 else "s3_internal",
        )
        for i in range(100_000)
    ]
    start = time.perf_counter()
    findings = 0
    for line in lines:
        rec = parse_datagram(line)
        assert rec is not None
        findings += len(engine.process(rec))
    elapsed = time.perf_counter() - start
    rate = len(lines) / elapsed
    print(f"\nthroughput: {rate:,.0f} lines/s (parse + rules, {findings} findings)")
    assert rate > 20_000
