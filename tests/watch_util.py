"""Shared helpers for ober watch tests."""

import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from ober.watch.models import LogRecord


def line_dict(**overrides: Any) -> dict[str, Any]:
    """A valid HAProxy JSON log object."""
    base: dict[str, Any] = {
        "ts": 1_700_000_000.0,
        "node": "px1",
        "frontend": "s3_internal",
        "client_ip": "192.0.2.10",
        "method": "GET",
        "host": "s3.example.org",
        "bucket": "mybucket",
        "access_key": "AKIAEXAMPLE",
        "presigned": False,
        "status": 200,
        "bytes_in": 10,
        "bytes_out": 2000,
        "duration_ms": 12,
        "user_agent": "aws-cli/2.0",
        "tls_version": "TLSv1.3",
    }
    base.update(overrides)
    return base


def syslog_line(**overrides: Any) -> bytes:
    """A datagram with an RFC 3164 header."""
    return b"<134>Oct 14 10:00:00 px1 haproxy[123]: " + json.dumps(line_dict(**overrides)).encode()


def rec(**overrides: Any) -> LogRecord:
    """A LogRecord with sensible defaults."""
    return LogRecord(**line_dict(**overrides))


class FakeHAProxy:
    """Fake HAProxy runtime socket that emulates add/del/show acl."""

    def __init__(self, acl_id: str, initial: list[str] | None = None) -> None:
        self.dir = tempfile.mkdtemp(prefix="obt")  # short path: AF_UNIX limit
        self.path = os.path.join(self.dir, "s.sock")
        self.acl_id = acl_id
        self.values: list[str] = list(initial or [])
        self.commands: list[str] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(4096).decode().strip()
                conn.sendall(self._handle(data).encode())

    def _handle(self, cmd: str) -> str:
        self.commands.append(cmd)
        parts = cmd.split()
        if len(parts) >= 3 and parts[2] != self.acl_id:
            return "Unknown ACL identifier. Please use #<id> or <file>.\n\n"
        if parts[:2] == ["show", "acl"]:
            return "".join(f"0x{i:x} {v}\n" for i, v in enumerate(self.values, 1)) + "\n"
        if parts[:2] == ["add", "acl"]:
            if parts[3] == "bogus":
                return "'bogus' is not a valid IPv4 or IPv6 address.\n\n"
            self.values.append(parts[3])
            return "\n"
        if parts[:2] == ["del", "acl"]:
            if parts[3] in self.values:
                self.values.remove(parts[3])
                return "\n"
            return "Key not found.\n\n"
        return "Unknown command\n\n"

    def close(self) -> None:
        self._stop = True
        self._srv.close()
        try:
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass


def free_path() -> Path:
    """A short, unused socket path."""
    d = tempfile.mkdtemp(prefix="obt")
    return Path(d) / "s.sock"
