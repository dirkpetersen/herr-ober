#!/usr/bin/env python3
"""Minimal client for the HAProxy runtime API (unix stats socket)."""

import socket
from pathlib import Path


class RuntimeAPIError(OSError):
    """Raised when the runtime API rejects a command."""


class SocketUnavailableError(OSError):
    """Raised when the runtime socket is missing or refuses connections."""


class HAProxyRuntime:
    """Send commands to HAProxy over its admin socket (one connection per command)."""

    def __init__(self, socket_path: Path | str, timeout: float = 5.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def send(self, command: str) -> str:
        """Send one command and return the full response text.

        Raises:
            SocketUnavailableError: if the socket cannot be reached.
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect(self.socket_path)
            except OSError as exc:
                raise SocketUnavailableError(f"{self.socket_path}: {exc}") from exc
            sock.sendall(command.strip().encode() + b"\n")
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        return b"".join(chunks).decode("utf-8", "replace")

    def _checked(self, command: str) -> str:
        response = self.send(command)
        if response.strip():
            raise RuntimeAPIError(f"{command.split(' ', 3)[0:2]}: {response.strip()[:200]}")
        return response

    def add_acl(self, acl_file: Path | str, value: str) -> None:
        """Add a value to a file-backed ACL (id is the file path).

        HAProxy accepts duplicates silently, so callers should check ``show_acl`` first.
        """
        self._checked(f"add acl {acl_file} {value}")

    def del_acl(self, acl_file: Path | str, value: str) -> int:
        """Delete a value from a file-backed ACL, including duplicate entries.

        HAProxy does not de-duplicate ``add acl`` and answers ``Key not found.`` once
        nothing is left to delete. Returns how many entries were removed (0 is fine).
        """
        removed = 0
        for _ in range(100):
            response = self.send(f"del acl {acl_file} {value}").strip()
            if not response:
                removed += 1
            elif response.startswith("Key not found"):
                break
            else:
                raise RuntimeAPIError(f"del acl: {response[:200]}")
        return removed

    def show_acl(self, acl_file: Path | str) -> list[str]:
        """Return the values of an ACL.

        HAProxy prints ``<ref> <pattern>`` per line; only the pattern is returned.
        """
        response = self.send(f"show acl {acl_file}")
        values: list[str] = []
        for line in response.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith("0x"):
                values.append(parts[1])
            elif parts:
                raise RuntimeAPIError(f"show acl: {response.strip()[:200]}")
        return values
