# ruff: noqa: ARG002  (fixtures used only for their side effects)
"""Tests for `ober status`, `ober doctor`, `ober logs`, per-command --json and cert expiry."""

import json
import time
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ober.certs import cert_not_after, check_cert_expiry
from ober.cli import main
from ober.commands import doctor, status
from ober.config import BackendConfig, OberConfig, VIPConfig
from ober.system import ServiceInfo

VIP1, VIP2 = "10.99.0.100", "10.99.0.101"

STAT_CSV = (
    "# pxname,svname,status,lastchg,\n"
    "s3,FRONTEND,OPEN,,\n"
    "rgw,rgw01,UP,100,\n"
    "rgw,rgw02,DOWN,720,\n"
    "rgw,BACKEND,UP,100,\n"
    "dead,d1,DOWN,5,\n"
)


@pytest.fixture
def node(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> OberConfig:
    """A saved BGP-mode config in temp_dir that `OberConfig.load()` finds."""
    monkeypatch.setenv("OBER_INSTALL_PATH", str(temp_dir))
    config = OberConfig(install_path=temp_dir)
    config.vips = [VIPConfig(address=f"{VIP1}/32"), VIPConfig(address=VIP2)]
    config.backends = [BackendConfig(name="rgw", servers=["rgw01:7480"])]
    config.save()
    return config


def services(active: dict[str, bool]) -> Callable[[str], ServiceInfo]:
    """Fake ServiceInfo.from_service_name: units named in `active` exist with that state."""

    def make(name: str) -> ServiceInfo:
        on = active.get(name, False)
        return ServiceInfo(name=name, is_active=on, is_enabled=on, status="active" if on else "x")

    return make


@pytest.fixture
def healthy() -> Generator[None, None, None]:
    """All ober units up, both VIPs local, all backend servers UP."""
    with (
        patch.object(
            ServiceInfo,
            "from_service_name",
            side_effect=services({"ober-http": True, "ober-bgp": True, "ober-ha": True}),
        ),
        patch.object(status, "local_ipv4_addresses", return_value={VIP1, VIP2}),
        patch.object(
            status, "backend_summary", return_value={"rgw": {"up": 2, "total": 2, "down": []}}
        ),
    ):
        yield


def write_state(config: OberConfig, rel: Path, data: object) -> None:
    path = config.install_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def run_check(cli_runner: CliRunner) -> tuple[int, str]:
    result = cli_runner.invoke(main, ["status", "--check"])
    return result.exit_code, result.output.strip()


# --- certificate expiry -------------------------------------------------------------


class TestCertExpiry:
    def test_levels(self, pem_cert: Path) -> None:
        end = cert_not_after(pem_cert)
        day = 86400
        assert check_cert_expiry(pem_cert, now=end - 30 * day)["level"] == "ok"
        assert check_cert_expiry(pem_cert, now=end - 21 * day - 60)["level"] == "ok"
        assert check_cert_expiry(pem_cert, now=end - 20 * day)["level"] == "warn"
        assert check_cert_expiry(pem_cert, now=end - 7 * day - 60)["level"] == "warn"
        assert check_cert_expiry(pem_cert, now=end - 6 * day)["level"] == "crit"
        expired = check_cert_expiry(pem_cert, now=end + day)
        assert expired["level"] == "crit" and "Expired" in expired["message"]

    def test_fresh_cert_has_about_100_days(self, pem_cert: Path) -> None:
        assert check_cert_expiry(pem_cert)["days_left"] in (99, 100)

    def test_unreadable(self, temp_dir: Path) -> None:
        result = check_cert_expiry(temp_dir / "missing.pem")
        assert result["level"] == "crit" and result["days_left"] is None


# --- status ---------------------------------------------------------------------------


class TestStatusCheck:
    def test_ok(self, cli_runner: CliRunner, node: OberConfig, healthy: None) -> None:
        write_state(node, status.HEALTH_STATE_FILE, {"announced": [f"{VIP1}/32", f"{VIP2}/32"]})
        code, out = run_check(cli_runner)
        assert code == 0, out
        assert out.startswith("OBER OK")
        assert "\n" not in out

    def test_bgp_session_down_warns(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        node.bgp.neighbors = ["10.0.0.1", "10.0.0.2"]
        node.save()
        write_state(node, status.HEALTH_STATE_FILE, {"announced": [f"{VIP1}/32", f"{VIP2}/32"]})
        sessions = {"10.0.0.1": True, "10.0.0.2": False}
        with patch.object(status, "bgp_sessions", return_value=sessions):
            code, out = run_check(cli_runner)
            human = cli_runner.invoke(main, ["status"]).output
        assert code == 1
        assert "BGP neighbor 10.0.0.2: no established session" in out
        assert "10.0.0.1:" not in out
        assert "BGP 10.0.0.2: not established" in human
        with patch.object(status, "bgp_sessions", return_value=None):
            code, out = run_check(cli_runner)
        assert code == 1 and "BGP session state unknown" in out

    def test_bgp_sessions_parses_ss(self) -> None:
        from unittest.mock import MagicMock

        ss = "0 0 10.0.0.5:41234 10.0.0.1:179\n0 0 [::ffff:10.0.0.5]:179 [::ffff:10.0.0.3]:5\n"
        with patch.object(
            status.subprocess, "run", return_value=MagicMock(returncode=0, stdout=ss)
        ):
            got = status.bgp_sessions(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        assert got == {"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": True}
        with patch.object(status.subprocess, "run", side_effect=OSError):
            assert status.bgp_sessions(["10.0.0.1"]) is None
        with patch.object(status.subprocess, "run", return_value=MagicMock(returncode=1)):
            assert status.bgp_sessions(["10.0.0.1"]) is None

    def test_missing_health_state_is_unknown_warn(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        code, out = run_check(cli_runner)
        assert code == 1
        assert f"{VIP1}: announce state unknown" in out

    def test_not_announced_warns(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        write_state(node, status.HEALTH_STATE_FILE, {"announced": [f"{VIP1}/32"]})
        code, out = run_check(cli_runner)
        assert code == 1
        assert f"{VIP2}: not announced" in out and f"{VIP1}:" not in out

    def test_http_down_is_crit(self, cli_runner: CliRunner, node: OberConfig) -> None:
        write_state(node, status.HEALTH_STATE_FILE, {"announced": []})
        with (
            patch.object(ServiceInfo, "from_service_name", side_effect=services({})),
            patch.object(status, "local_ipv4_addresses", return_value=set()),
            patch.object(status, "backend_summary", return_value=None),
        ):
            code, out = run_check(cli_runner)
        assert code == 2
        assert out.startswith("OBER CRIT") and "ober-http" in out

    def test_cert_warn(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None, pem_cert: Path
    ) -> None:
        write_state(node, status.HEALTH_STATE_FILE, {"announced": [f"{VIP1}/32", f"{VIP2}/32"]})
        node.certs.path = str(pem_cert)
        node.save()
        soon = cert_not_after(pem_cert) - 10 * 86400
        with patch("ober.certs.time.time", return_value=soon):
            code, out = run_check(cli_runner)
        assert code == 1 and "certificate: 10 days left" in out

    def test_invalid_config_still_works(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        node.config_path.write_text("ha_mode: [unclosed\n")
        result = cli_runner.invoke(main, ["status"])
        assert result.exit_code == 0, result.output
        assert "Config invalid" in result.output
        code, out = run_check(cli_runner)
        assert code == 2 and "config invalid" in out

    def test_logs_with_invalid_config(self, cli_runner: CliRunner, node: OberConfig) -> None:
        node.config_path.write_text("ha_mode: [unclosed\n")
        with patch("subprocess.run") as run:
            result = cli_runner.invoke(main, ["logs"])
        assert result.exit_code == 0
        assert run.called


class TestStatusKeepalived:
    @pytest.fixture
    def ka_node(self, node: OberConfig) -> OberConfig:
        node.ha_mode = "keepalived"
        node.save()
        return node

    def test_vrrp_state_from_file(
        self, cli_runner: CliRunner, ka_node: OberConfig, healthy: None
    ) -> None:
        write_state(ka_node, status.VRRP_STATE_FILE, {"VI_1": "MASTER", "VI_2": "BACKUP"})
        with patch.object(status, "local_ipv4_addresses", return_value={VIP1}):
            result = cli_runner.invoke(main, ["status", "--json"])
        data = json.loads(result.output)
        states = {v["address"]: v["vrrp_state"] for v in data["vips"]}
        assert states == {VIP1: "MASTER", VIP2: "BACKUP"}
        assert data["check"]["level"] == "OK"
        assert "ober-ha" in data["services"] and "ober-bgp" not in data["services"]
        assert all(v["expected_owner"] for v in data["vips"])

    def test_vrrp_missing_is_unknown(
        self, cli_runner: CliRunner, ka_node: OberConfig, healthy: None
    ) -> None:
        code, out = run_check(cli_runner)
        assert code == 1 and "VRRP state unknown" in out

    def test_backup_holding_vip_warns_fault_crit(
        self, cli_runner: CliRunner, ka_node: OberConfig, healthy: None
    ) -> None:
        write_state(ka_node, status.VRRP_STATE_FILE, {"VI_1": "BACKUP", "VI_2": "FAULT"})
        code, out = run_check(cli_runner)
        assert code == 2
        assert f"{VIP1}: VRRP BACKUP but VIP present" in out
        assert f"{VIP2}: VRRP FAULT" in out


class TestBackendSummary:
    def test_parses_show_stat(self, temp_config: OberConfig) -> None:
        with patch("ober.watch.runtime.HAProxyRuntime.send", return_value=STAT_CSV):
            summary = status.backend_summary(temp_config)
        assert summary is not None
        assert summary["rgw"]["up"] == 1 and summary["rgw"]["total"] == 2
        assert summary["rgw"]["down"] == [{"server": "rgw02", "status": "DOWN", "since_s": 720}]
        assert summary["dead"]["up"] == 0
        assert "s3" not in summary

    def test_socket_missing_is_unknown(self, temp_config: OberConfig) -> None:
        with patch("ober.watch.runtime.HAProxyRuntime.send", side_effect=OSError("nope")):
            assert status.backend_summary(temp_config) is None

    def test_backend_levels(self) -> None:
        result = {
            "config": {"error": None, "exists": True, "path": "x"},
            "services": {},
            "vips": [],
            "backends": {
                "rgw": {"up": 1, "total": 2, "down": [{"server": "rgw02"}]},
                "dead": {"up": 0, "total": 1, "down": [{"server": "d1"}]},
            },
        }
        check = status.evaluate(result)
        assert check["level"] == "CRIT"
        assert "rgw: 1/2 UP (rgw02 down)" in check["problems"]
        assert "dead: 0/1 servers UP" in check["problems"]


def test_maintenance_is_warn() -> None:
    result = {
        "config": {"error": None, "exists": True, "path": "x"},
        "services": {},
        "vips": [],
        "backends": {},
        "maintenance": True,
    }
    check = status.evaluate(result)
    assert check["level"] == "WARN"
    assert "maintenance mode on (node out of rotation)" in check["problems"]


def test_local_ipv4_addresses() -> None:
    out = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
        "3: lo-vip    inet 10.99.0.100/32 scope global lo-vip\n"
    )
    with patch("ober.commands.status.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = out
        assert status.local_ipv4_addresses() == {"127.0.0.1", VIP1}
        run.side_effect = FileNotFoundError
        assert status.local_ipv4_addresses() is None


# --- per-command --json ------------------------------------------------------------------


class TestJsonFlag:
    def test_status_json_after_command(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        result = cli_runner.invoke(main, ["status", "--json"])
        assert result.exit_code == 0
        assert "ober-http" in json.loads(result.output)["services"]

    def test_group_json_still_works(
        self, cli_runner: CliRunner, node: OberConfig, healthy: None
    ) -> None:
        result = cli_runner.invoke(main, ["--json", "status"])
        assert "services" in json.loads(result.output)

    def test_every_command_accepts_json(self, cli_runner: CliRunner) -> None:
        for name in ("doctor", "test", "upgrade", "status", "logs", "watch", "blocklist"):
            result = cli_runner.invoke(main, [name, "--help"])
            assert "--json" in result.output, name

    def test_logs_json(self, cli_runner: CliRunner) -> None:
        with patch("subprocess.run") as run:
            cli_runner.invoke(main, ["logs", "--json"])
        assert "--output=json" in run.call_args[0][0]


# --- logs --------------------------------------------------------------------------------


class TestLogs:
    def cmd(self, cli_runner: CliRunner, *args: str) -> list[str]:
        with patch("subprocess.run") as run:
            result = cli_runner.invoke(main, ["logs", *args])
        assert result.exit_code == 0, result.output
        return list(run.call_args[0][0])

    def test_all_units(self, cli_runner: CliRunner) -> None:
        cmd = self.cmd(cli_runner)
        for unit in ("ober-http", "ober-bgp", "ober-ha", "ober-blocklist", "ober-watch"):
            assert unit in cmd
        assert cmd[cmd.index("-n") + 1] == "50"

    def test_watch_and_blocklist(self, cli_runner: CliRunner) -> None:
        assert "ober-watch" in self.cmd(cli_runner, "--service", "watch")
        cmd = self.cmd(cli_runner, "--service", "blocklist")
        assert "ober-blocklist" in cmd and "ober-http" not in cmd

    def test_since_shows_all_lines(self, cli_runner: CliRunner) -> None:
        cmd = self.cmd(cli_runner, "--since", "1 hour ago")
        assert cmd[cmd.index("--since") + 1] == "1 hour ago"
        assert "-n" not in cmd
        cmd = self.cmd(cli_runner, "--since", "today", "-n", "5")
        assert cmd[cmd.index("-n") + 1] == "5"


# --- doctor ------------------------------------------------------------------------------


def checks_by_name(cli_runner: CliRunner) -> dict[str, dict[str, object]]:
    result = cli_runner.invoke(main, ["doctor", "--json"])
    return {c["name"]: c for c in json.loads(result.output)["checks"]}


class TestDoctor:
    def test_version_compare(self) -> None:
        for version, status_str in (("4.0.1", "installed"), ("3.3.0", "installed")):
            with patch.object(doctor, "get_haproxy_version", return_value=version):
                assert doctor._check_haproxy()["status"] == status_str
        with patch.object(doctor, "get_haproxy_version", return_value="3.2.25"):
            assert doctor._check_haproxy()["status"] == "installed (old)"

    def test_root_is_not_critical(self) -> None:
        from ober.system import SystemInfo

        system = SystemInfo()
        system.is_root = False
        assert doctor._check_root(system)["critical"] is False

    def test_only_current_mode_and_off_units(self, cli_runner: CliRunner, node: OberConfig) -> None:
        active = {"ober-http": True, "ober-bgp": True, "ober-ha": True, "haproxy": True}
        with (
            patch.object(ServiceInfo, "from_service_name", side_effect=services(active)),
            patch.object(doctor, "local_ipv4_addresses", return_value={VIP1, VIP2}),
            patch.object(
                doctor, "_check_restart_pending", return_value={"name": "r", "passed": True}
            ),
        ):
            checks = checks_by_name(cli_runner)
        assert checks["Service: ober-bgp"]["passed"] is True
        assert "Service: ober-ha" not in checks
        assert checks["Unit off: ober-ha"]["passed"] is False
        assert checks["Unit off: haproxy"]["passed"] is False
        assert checks["Unit off: keepalived"]["passed"] is True
        assert checks["VIPs present"]["passed"] is True
        assert "ExaBGP" in checks and "Keepalived" not in checks

    def test_vips_missing_bgp(self, node: OberConfig) -> None:
        with patch.object(doctor, "local_ipv4_addresses", return_value={VIP1}):
            result = doctor._check_vips(node)
        assert result["passed"] is False and f"{VIP2} missing" in str(result["message"])

    def test_cert_check(self, pem_cert: Path) -> None:
        assert doctor._check_cert(str(pem_cert))["passed"] is True
        soon = cert_not_after(pem_cert) - 3 * 86400
        with patch("ober.certs.time.time", return_value=soon):
            result = doctor._check_cert(str(pem_cert))
        assert result["passed"] is False and result["critical"] is True

    def test_invalid_config_is_critical(self, cli_runner: CliRunner, node: OberConfig) -> None:
        node.config_path.write_text("ha_mode: [unclosed\n")
        with patch.object(ServiceInfo, "from_service_name", side_effect=services({})):
            result = cli_runner.invoke(main, ["doctor", "--json"])
        assert result.exit_code == 1
        checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
        assert checks["Configuration"]["status"] == "invalid"


class TestRestartPending:
    svc = ServiceInfo(name="ober-http", is_active=True, pid=4242)

    def test_deleted_binary(self, temp_config: OberConfig) -> None:
        with patch("ober.commands.doctor.os.readlink", return_value="/usr/sbin/haproxy (deleted)"):
            result = doctor._check_restart_pending(temp_config, self.svc)
        assert result["status"] == "restart needed"

    def test_config_newer_than_process(self, temp_config: OberConfig) -> None:
        temp_config.haproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_config.haproxy_config_path.write_text("global\n")
        with (
            patch("ober.commands.doctor.os.readlink", return_value="/usr/sbin/haproxy"),
            patch.object(doctor, "_haproxy_loaded_at", return_value=time.time() - 3600),
        ):
            assert doctor._check_restart_pending(temp_config, self.svc)["status"] == "reload needed"
        with (
            patch("ober.commands.doctor.os.readlink", return_value="/usr/sbin/haproxy"),
            patch.object(doctor, "_haproxy_loaded_at", return_value=time.time() + 5),
        ):
            assert doctor._check_restart_pending(temp_config, self.svc)["passed"] is True

    def test_worker_uptime_from_socket(self, temp_config: OberConfig) -> None:
        with patch("ober.watch.runtime.HAProxyRuntime.send", return_value="Uptime_sec: 100\n"):
            loaded = doctor._haproxy_loaded_at(temp_config, 1)
        assert abs(loaded - (time.time() - 100)) < 5

    def test_process_start_time_of_self(self) -> None:
        import os

        start = doctor._process_start_time(os.getpid())
        assert time.time() - 86400 * 365 < start <= time.time() + 1

    def test_not_root_is_unknown(self, temp_config: OberConfig) -> None:
        with patch("ober.commands.doctor.os.readlink", side_effect=PermissionError("denied")):
            result = doctor._check_restart_pending(temp_config, self.svc)
        assert result["status"] == "unknown" and not result.get("critical")
