#!/usr/bin/env python3
"""Blocklist synchronisation on proxy nodes (runs from a systemd timer)."""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import requests

from ober.config import OberConfig
from ober.watch.blocklist import normalize_target
from ober.watch.runtime import HAProxyRuntime, RuntimeAPIError, SocketUnavailableError

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 10


@dataclass
class SyncResult:
    """Outcome of one sync run."""

    reachable: bool = True
    socket_ok: bool = True
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def read_target_file(path: Path) -> set[str]:
    """Read one target per line (comments and blanks ignored)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    out = set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def write_target_file(path: Path, targets: set[str]) -> None:
    """Atomically rewrite the blocklist file (tmp in the same directory + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(f"{t}\n" for t in sorted(targets)))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch_targets(base_url: str, token: str) -> set[str]:
    """GET the active blocklist from ober watch and return normalised targets.

    Raises:
        requests.RequestException: if the service is unreachable or answers an error.
    """
    resp = requests.get(
        base_url.rstrip("/") + "/v1/blocklist",
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    targets: set[str] = set()
    for entry in resp.json().get("entries", []):
        try:
            targets.add(normalize_target(str(entry["target"])))
        except (KeyError, ValueError):
            logger.warning("Ignoring invalid blocklist entry from server")
    return targets


def sync_blocklist(config: OberConfig, token: str) -> SyncResult:
    """Pull the desired blocklist and apply it to HAProxy and the on-disk file.

    If the watch service is unreachable nothing is changed. If the runtime socket
    is unavailable (HAProxy down) only the file is updated, so a restart picks it up.
    """
    result = SyncResult()
    base = config.watch.url
    try:
        desired = fetch_targets(base, token)
    except (requests.RequestException, ValueError) as exc:
        result.reachable = False
        result.warnings.append(f"ober watch unreachable, keeping current blocklist: {exc}")
        return result

    acl_file = config.blocklist_path
    current = {normalize_target_safe(t) for t in read_target_file(acl_file)}
    current.discard("")
    to_add = sorted(desired - current)
    to_del = sorted(current - desired)
    new_state = set(desired)

    runtime = HAProxyRuntime(config.admin_socket_path)
    if to_add or to_del:
        try:
            live = set(runtime.show_acl(acl_file))
            for target in to_add:
                try:
                    if target not in live:
                        runtime.add_acl(acl_file, target)
                    result.added.append(target)
                except RuntimeAPIError as exc:
                    result.errors.append(f"add {target}: {exc}")
                    new_state.discard(target)
            for target in to_del:
                try:
                    runtime.del_acl(acl_file, target)
                    result.removed.append(target)
                except RuntimeAPIError as exc:
                    result.errors.append(f"del {target}: {exc}")
                    new_state.add(target)
        except SocketUnavailableError as exc:
            result.socket_ok = False
            result.warnings.append(f"HAProxy runtime socket unavailable, updating file only: {exc}")
            result.added = to_add
            result.removed = to_del
        except RuntimeAPIError as exc:
            result.errors.append(str(exc))
            new_state = set(current)

    if new_state != current or not acl_file.exists():
        write_target_file(acl_file, new_state)
    return result


def normalize_target_safe(target: str) -> str:
    """Normalise a target from the file; unparseable lines are returned unchanged."""
    try:
        return normalize_target(target)
    except ValueError:
        return target
