#!/usr/bin/env python3
"""HTTP API of ober watch: health counters and the blocklist for proxy nodes."""

import hmac
import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

from ober.watch.blocklist import BlockList, normalize_target

logger = logging.getLogger(__name__)

MAX_BODY = 4096
MAX_TTL_S = 10 * 365 * 86400


def split_listen(listen: str) -> tuple[str, int]:
    """Split ``host:port`` (IPv6 hosts may be bracketed)."""
    host, _, port = listen.rpartition(":")
    return host.strip("[]") or "0.0.0.0", int(port)


class WatchAPI:
    """Threaded HTTP server wrapping a :class:`BlockList`."""

    def __init__(
        self,
        listen: str,
        blocklist: BlockList,
        token: str,
        stats: Callable[[], dict[str, Any]],
    ) -> None:
        self.blocklist = blocklist
        self._token = token
        self._stats = stats
        host, port = split_listen(listen)
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound TCP port (useful when listening on port 0)."""
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        """Serve in a background thread."""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ober-watch-api", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the port."""
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(5)
        self._httpd.server_close()

    def _authorized(self, header: str | None) -> bool:
        if not self._token or not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].encode(), self._token.encode())

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ober-watch"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug("api: " + format, *args)

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self) -> bool:
                if api._authorized(self.headers.get("Authorization")):
                    return True
                self._send(401, {"error": "unauthorized"})
                return False

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/v1/health":
                    self._send(200, api._stats())
                elif path == "/v1/blocklist":
                    if self._auth():
                        entries = [e.to_dict() for e in api.blocklist.active()]
                        self._send(200, {"entries": entries})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.split("?", 1)[0] != "/v1/blocklist":
                    self._send(404, {"error": "not found"})
                    return
                if not self._auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_BODY:
                        raise ValueError("bad body size")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                    target = body["target"]
                    reason = body.get("reason", "")
                    ttl = body.get("ttl_s")
                    if not isinstance(target, str) or not isinstance(reason, str):
                        raise ValueError("target and reason must be strings")
                    if ttl is not None and (type(ttl) is not int or ttl < 0 or ttl > MAX_TTL_S):
                        raise ValueError("ttl_s must be a non-negative integer")
                    entry = api.blocklist.add_manual(target, reason, ttl)
                except (KeyError, ValueError, TypeError) as exc:
                    self._send(400, {"error": f"invalid request: {str(exc)[:100]}"})
                    return
                self._send(200, {"entry": entry.to_dict()})

            def do_DELETE(self) -> None:  # noqa: N802
                prefix = "/v1/blocklist/"
                path = self.path.split("?", 1)[0]
                if not path.startswith(prefix):
                    self._send(404, {"error": "not found"})
                    return
                if not self._auth():
                    return
                try:
                    target = normalize_target(unquote(path[len(prefix) :]))
                except ValueError as exc:
                    self._send(400, {"error": f"invalid target: {str(exc)[:100]}"})
                    return
                if api.blocklist.remove(target):
                    self._send(200, {"removed": target})
                else:
                    self._send(404, {"error": "not in blocklist"})

        return Handler
