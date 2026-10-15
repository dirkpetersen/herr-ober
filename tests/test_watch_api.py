"""Tests for the watch HTTP API."""

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ober.watch.blocklist import BlockList
from ober.watch.server import WatchAPI

TOKEN = "s3cret-token"
ADMIN = "adm1n-token"


@pytest.fixture
def api(temp_dir: Path) -> Iterator[WatchAPI]:
    bl = BlockList(temp_dir / "b.json")
    srv = WatchAPI("127.0.0.1:0", bl, TOKEN, lambda: {"received": 3, "malformed": 1}, ADMIN)
    srv.start()
    yield srv
    srv.stop()


def call(
    api: WatchAPI, method: str, path: str, body: Any = None, token: str | None = ADMIN
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{api.port}{path}", data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_needs_no_auth(api: WatchAPI) -> None:
    assert call(api, "GET", "/v1/health", token=None) == (200, {"received": 3, "malformed": 1})


@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_auth_required(api: WatchAPI, token: str | None) -> None:
    assert call(api, "GET", "/v1/blocklist", token=token)[0] == 401
    assert call(api, "POST", "/v1/blocklist", {"target": "1.2.3.4"}, token=token)[0] == 401
    assert call(api, "DELETE", "/v1/blocklist/1.2.3.4", token=token)[0] == 401


def test_read_token_reads_but_cannot_write(api: WatchAPI) -> None:
    assert call(api, "GET", "/v1/blocklist", token=TOKEN)[0] == 200
    assert call(api, "GET", "/v1/blocklist", token=ADMIN)[0] == 200
    assert call(api, "POST", "/v1/blocklist", {"target": "1.2.3.4"}, token=TOKEN)[0] == 401
    assert call(api, "DELETE", "/v1/blocklist/1.2.3.4", token=TOKEN)[0] == 401


def test_writes_disabled_without_admin_token(temp_dir: Path) -> None:
    srv = WatchAPI("127.0.0.1:0", BlockList(temp_dir / "b.json"), TOKEN, dict)
    srv.start()
    try:
        assert call(srv, "GET", "/v1/blocklist", token=TOKEN)[0] == 200
        assert call(srv, "POST", "/v1/blocklist", {"target": "1.2.3.4"}, token=TOKEN)[0] == 403
        assert call(srv, "DELETE", "/v1/blocklist/1.2.3.4", token=TOKEN)[0] == 403
    finally:
        srv.stop()


def test_api_cannot_add_auto_entries(api: WatchAPI) -> None:
    assert call(api, "POST", "/v1/blocklist", {"target": "1.2.3.4", "source": "auto"})[0] == 400
    assert call(api, "POST", "/v1/blocklist", {"target": "1.0.0.0/7"})[0] == 400
    assert call(api, "POST", "/v1/blocklist", {"target": "1.0.0.0/8"})[0] == 200
    entries = call(api, "GET", "/v1/blocklist")[1]["entries"]
    assert [e["source"] for e in entries] == ["manual"]


def test_idle_connection_is_dropped(api: WatchAPI) -> None:
    from ober.watch import server

    # the handler class carries the socket timeout that frees idle threads
    assert 0 < server.REQUEST_TIMEOUT_S <= 10
    assert api._httpd.RequestHandlerClass.timeout == server.REQUEST_TIMEOUT_S  # type: ignore[attr-defined]


def test_empty_server_token_rejects_everything(temp_dir: Path) -> None:
    srv = WatchAPI("127.0.0.1:0", BlockList(temp_dir / "b.json"), "", dict)
    srv.start()
    try:
        assert call(srv, "GET", "/v1/blocklist", token="")[0] == 401
    finally:
        srv.stop()


def test_add_list_delete(api: WatchAPI) -> None:
    code, body = call(api, "POST", "/v1/blocklist", {"target": "203.0.113.5", "reason": "r"})
    assert code == 200 and body["entry"]["expires_at"] == 0
    code, body = call(api, "POST", "/v1/blocklist", {"target": "10.0.0.0/8", "ttl_s": 600})
    assert code == 200 and body["entry"]["expires_at"] > 0
    code, body = call(api, "GET", "/v1/blocklist")
    assert {e["target"] for e in body["entries"]} == {"203.0.113.5", "10.0.0.0/8"}
    assert call(api, "DELETE", "/v1/blocklist/10.0.0.0%2F8")[0] == 200
    assert call(api, "DELETE", "/v1/blocklist/10.0.0.0%2F8")[0] == 404
    assert [e["target"] for e in call(api, "GET", "/v1/blocklist")[1]["entries"]] == ["203.0.113.5"]


@pytest.mark.parametrize(
    "body",
    [
        {"target": "evil; rm -rf /"},
        {"target": "example.com"},
        {"target": "0.0.0.0/0"},
        {"target": 5},
        {"reason": "no target"},
        {"target": "1.2.3.4", "ttl_s": -1},
        {"target": "1.2.3.4", "ttl_s": "soon"},
        ["list"],
    ],
)
def test_post_validation(api: WatchAPI, body: Any) -> None:
    assert call(api, "POST", "/v1/blocklist", body)[0] == 400
    assert call(api, "GET", "/v1/blocklist")[1]["entries"] == []


def test_delete_invalid_target_and_unknown_paths(api: WatchAPI) -> None:
    assert call(api, "DELETE", "/v1/blocklist/not-an-ip")[0] == 400
    assert call(api, "GET", "/v1/nothing")[0] == 404
