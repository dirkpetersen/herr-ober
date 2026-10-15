#!/usr/bin/env python3
"""systemd unit templates for ober watch and blocklist sync."""

import sys
from pathlib import Path

from ober.config import OberConfig


def render_watch_service(config: OberConfig, ober_bin: str, protect_home: str = "true") -> str:
    """Return the text of ``ober-watch.service`` (analysis host)."""
    data_dir = config.watch_data_dir
    return f"""[Unit]
Description=Ober Watch (S3 log analysis)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={ober_bin} watch run
Restart=on-failure
RestartSec=5
Nice=10
# The analysis pipeline must never starve the data path on a shared host
CPUSchedulingPolicy=batch
LimitNOFILE=65536

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome={protect_home}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
ReadWritePaths={data_dir}

[Install]
WantedBy=multi-user.target
"""


def render_blocklist_service(config: OberConfig, ober_bin: str, protect_home: str = "true") -> str:
    """Return the text of ``ober-blocklist.service`` (oneshot, proxy nodes)."""
    acl_dir = config.blocklist_path.parent
    sock_dir = config.admin_socket_path.parent
    return f"""[Unit]
Description=Ober blocklist sync
After=network-online.target

[Service]
Type=oneshot
ExecStart={ober_bin} blocklist sync
Nice=10

# Hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome={protect_home}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
ReadWritePaths={acl_dir} {sock_dir}
"""


def render_blocklist_timer(config: OberConfig) -> str:
    """Return the text of ``ober-blocklist.timer``."""
    interval = max(1, config.watch.sync_interval_s)
    return f"""[Unit]
Description=Ober blocklist sync timer

[Timer]
OnBootSec=15s
OnUnitActiveSec={interval}s
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def write_units(
    config: OberConfig,
    directory: Path,
    role: str = "all",
    ober_bin: str | None = None,
    protect_home: str = "true",
) -> list[Path]:
    """Write unit files into ``directory`` and return the written paths.

    Args:
        config: Loaded configuration (paths and sync interval).
        directory: Target directory, e.g. ``/etc/systemd/system``.
        role: ``"watch"`` (analysis host), ``"proxy"`` (blocklist sync) or ``"all"``.
        ober_bin: Absolute path of the ``ober`` executable; defaults to the one next
            to the running interpreter.
        protect_home: systemd ``ProtectHome=`` value; bootstrap passes ``read-only``
            when ober is installed under /root or /home.
    """
    if role not in ("watch", "proxy", "all"):
        raise ValueError(f"unknown role {role!r}")
    binary = ober_bin or str(Path(sys.prefix) / "bin" / "ober")
    directory.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if role in ("watch", "all"):
        files["ober-watch.service"] = render_watch_service(config, binary, protect_home)
    if role in ("proxy", "all"):
        files["ober-blocklist.service"] = render_blocklist_service(config, binary, protect_home)
        files["ober-blocklist.timer"] = render_blocklist_timer(config)
    written = []
    for name, text in files.items():
        path = directory / name
        path.write_text(text)
        written.append(path)
    return written
