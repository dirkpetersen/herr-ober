"""Tests for the HAProxy runtime client and blocklist sync."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.config import OberConfig
from ober.watch.runtime import HAProxyRuntime, RuntimeAPIError, SocketUnavailableError
from ober.watch.sync import sync_blocklist
from tests.watch_util import FakeHAProxy, free_path


@pytest.fixture
def config(temp_dir: Path) -> OberConfig:
    cfg = OberConfig(install_path=temp_dir)
    cfg.watch.url = "http://watch.invalid:8405"
    cfg.blocklist_path.parent.mkdir(parents=True)
    return cfg


@pytest.fixture
def haproxy(config: OberConfig) -> Iterator[FakeHAProxy]:
    fake = FakeHAProxy(str(config.blocklist_path))
    with patch.object(OberConfig, "admin_socket_path", property(lambda _self: Path(fake.path))):
        yield fake
    fake.close()


def fake_api(targets: list[str]) -> object:
    return patch("ober.watch.sync.fetch_targets", return_value=set(targets))


def test_runtime_helpers(config: OberConfig, haproxy: FakeHAProxy) -> None:
    rt = HAProxyRuntime(haproxy.path)
    acl = config.blocklist_path
    rt.add_acl(acl, "1.2.3.4")
    rt.add_acl(acl, "10.0.0.0/8")
    assert rt.show_acl(acl) == ["1.2.3.4", "10.0.0.0/8"]
    assert rt.del_acl(acl, "1.2.3.4") == 1
    assert rt.del_acl(acl, "1.2.3.4") == 0  # "Key not found." is not an error
    with pytest.raises(RuntimeAPIError):
        rt.add_acl(acl, "bogus")
    with pytest.raises(RuntimeAPIError):
        rt.show_acl("/unknown")


def test_runtime_socket_missing() -> None:
    with pytest.raises(SocketUnavailableError):
        HAProxyRuntime(free_path()).send("show info")


def test_sync_diff(config: OberConfig, haproxy: FakeHAProxy) -> None:
    config.blocklist_path.write_text("1.1.1.1\n2.2.2.2\n")
    haproxy.values = ["1.1.1.1", "2.2.2.2"]
    with fake_api(["2.2.2.2", "3.3.3.3", "10.0.0.0/8"]):
        result = sync_blocklist(config, "tok")
    assert result.added == ["10.0.0.0/8", "3.3.3.3"]
    assert result.removed == ["1.1.1.1"]
    assert sorted(haproxy.values) == ["10.0.0.0/8", "2.2.2.2", "3.3.3.3"]
    assert config.blocklist_path.read_text().split() == ["10.0.0.0/8", "2.2.2.2", "3.3.3.3"]
    # add happens before del
    cmds = [c.split()[0] for c in haproxy.commands if not c.startswith("show")]
    assert cmds == ["add", "add", "del", "del"][: len(cmds)] and cmds[0] == "add"
    assert not list(config.blocklist_path.parent.glob("blocklist.lst.*"))


def test_sync_idempotent_no_runtime_writes(config: OberConfig, haproxy: FakeHAProxy) -> None:
    config.blocklist_path.write_text("1.1.1.1\n")
    haproxy.values = ["1.1.1.1"]
    with fake_api(["1.1.1.1"]):
        result = sync_blocklist(config, "tok")
    assert result.added == [] and result.removed == [] and haproxy.commands == []


def test_sync_does_not_duplicate_runtime_entries(config: OberConfig, haproxy: FakeHAProxy) -> None:
    haproxy.values = ["3.3.3.3"]  # already live although the file does not list it
    with fake_api(["3.3.3.3"]):
        sync_blocklist(config, "tok")
    assert haproxy.values == ["3.3.3.3"]
    assert config.blocklist_path.read_text() == "3.3.3.3\n"


def test_sync_unreachable_keeps_state(config: OberConfig, haproxy: FakeHAProxy) -> None:
    import requests

    config.blocklist_path.write_text("1.1.1.1\n")
    with patch("ober.watch.sync.fetch_targets", side_effect=requests.ConnectionError("down")):
        result = sync_blocklist(config, "tok")
    assert not result.reachable and result.warnings
    assert config.blocklist_path.read_text() == "1.1.1.1\n"
    assert haproxy.commands == []


def test_sync_socket_missing_updates_file(config: OberConfig) -> None:
    config.blocklist_path.write_text("1.1.1.1\n")
    with (
        patch.object(OberConfig, "admin_socket_path", property(lambda _self: free_path())),
        fake_api(["9.9.9.9"]),
    ):
        result = sync_blocklist(config, "tok")
    assert not result.socket_ok and result.warnings
    assert config.blocklist_path.read_text() == "9.9.9.9\n"


@pytest.mark.usefixtures("haproxy")
def test_sync_runtime_rejection_keeps_item_out_of_file(config: OberConfig) -> None:
    with (
        patch("ober.watch.sync.normalize_target", side_effect=lambda t: t),
        fake_api(["bogus", "4.4.4.4"]),
    ):
        result = sync_blocklist(config, "tok")
    assert any("bogus" in e for e in result.errors)
    assert config.blocklist_path.read_text() == "4.4.4.4\n"


@pytest.mark.usefixtures("haproxy")
def test_cli_sync_exit_codes(config: OberConfig) -> None:
    import requests

    runner = CliRunner()
    with (
        patch("ober.commands.watch.OberConfig.load", return_value=config),
        patch("ober.commands.watch.load_secrets", return_value={"OBER_WATCH_TOKEN": "t"}),
    ):
        with fake_api(["5.5.5.5"]):
            res = runner.invoke(main, ["--json", "blocklist", "sync"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["added"] == ["5.5.5.5"]
        with patch("ober.watch.sync.fetch_targets", side_effect=requests.Timeout("t")):
            assert runner.invoke(main, ["blocklist", "sync"]).exit_code == 0
        config.watch.url = ""
        assert runner.invoke(main, ["blocklist", "sync"]).exit_code == 1


def test_cli_local_blocklist_commands(config: OberConfig) -> None:
    runner = CliRunner()
    with (
        patch("ober.commands.watch.OberConfig.load", return_value=config),
        patch("ober.commands.watch._api_base", return_value=None),
    ):
        assert (
            runner.invoke(main, ["blocklist", "add", "198.51.100.1", "--ttl", "60"]).exit_code == 0
        )
        res = runner.invoke(main, ["--json", "blocklist", "list"])
        assert [e["target"] for e in json.loads(res.output)] == ["198.51.100.1"]
        assert runner.invoke(main, ["blocklist", "add", "nonsense"]).exit_code == 1
        assert runner.invoke(main, ["blocklist", "remove", "198.51.100.1"]).exit_code == 0
        assert runner.invoke(main, ["blocklist", "remove", "198.51.100.1"]).exit_code == 1
