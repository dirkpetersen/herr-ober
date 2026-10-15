#!/usr/bin/env python3
"""Configuration management for Ober."""

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class BGPConfig:
    """BGP configuration settings."""

    local_as: int = 65001
    peer_as: int = 65000
    neighbors: list[str] = field(default_factory=list)
    router_id: str = ""
    local_address: str = ""
    hold_time: int = 3
    bfd_enabled: bool = True


@dataclass
class VIPConfig:
    """Virtual IP configuration."""

    address: str = ""
    interface: str = "lo-vip"
    exposure: str = "internal"  # "internal" (unchanged behavior) or "internet" (hardened)


@dataclass
class SecurityConfig:
    """Hardening for VIPs with exposure "internet" (HAProxy data path).

    Every control here must be native HAProxy with per-request cost only (no
    per-byte cost, no network round trip per request).
    """

    s3_hostnames: list[str] = field(default_factory=list)  # e.g. ["s3.example.org"]
    allowlist_enabled: bool = True  # phase 1: internet VIPs are default-deny
    allowlist: list[str] = field(default_factory=list)  # institution CIDRs
    trusted_networks: list[str] = field(default_factory=list)  # never limited or banned
    deny_admin_api: bool = True  # block RGW /admin on internet VIPs
    deny_anonymous: bool = False  # block unsigned requests on internet VIPs
    autoban_enabled: bool = True
    autoban_auth_failures: int = 50  # 401/403 responses per window before a ban
    autoban_window_s: int = 60
    autoban_ban_s: int = 3600
    conn_limit_per_ip: int = 0  # concurrent connections per IP, 0 = off (opt-in)
    timeout_client_s: int = 300
    timeout_server_s: int = 600  # long enough for CompleteMultipartUpload on huge objects
    log_target: str = ""  # "host:port" UDP syslog of ober watch; "" = log to stdout


@dataclass
class WatchConfig:
    """ober watch (analysis host) and blocklist sync (proxy nodes)."""

    syslog_listen: str = "0.0.0.0:5514"  # UDP, receives HAProxy JSON logs
    api_listen: str = "0.0.0.0:8405"  # HTTP, serves the blocklist to proxy nodes
    url: str = ""  # proxy nodes: where to pull the blocklist, e.g. http://watch01:8405
    data_dir: str = ""  # "" = <install_path>/var/watch
    sync_interval_s: int = 30
    summary_interval_s: int = 300
    raw_log_retention_days: int = 30
    block_ttl_s: int = 3600  # first auto-block; doubles for repeat offenders
    block_ttl_max_s: int = 604800


@dataclass
class SentinelConfig:
    """Microsoft Sentinel via the Azure Monitor Logs Ingestion API.

    The client secret is stored in ~/.ober/login as SENTINEL_CLIENT_SECRET.
    """

    enabled: bool = False
    tenant_id: str = ""
    client_id: str = ""
    dce_endpoint: str = ""  # https://<dce>.<region>.ingest.monitor.azure.com
    dcr_immutable_id: str = ""  # dcr-...
    findings_stream: str = "Custom-OberFindings_CL"
    summary_stream: str = "Custom-OberKeySummary_CL"


@dataclass
class MDEConfig:
    """Microsoft Defender for Endpoint indicators (off by default).

    The client secret is stored in ~/.ober/login as MDE_CLIENT_SECRET.
    """

    enabled: bool = False
    tenant_id: str = ""
    client_id: str = ""
    api_base: str = "https://api.securitycenter.microsoft.com"
    min_severity: str = "high"  # findings at or above this become indicators
    action: str = "Block"  # "Block" or "Audit"
    expiry_days: int = 7


def _section_from_dict(cls: type[Any], data: dict[str, Any] | None) -> Any:
    """Build a config dataclass from a dict, ignoring unknown keys."""
    from dataclasses import fields

    data = data or {}
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class BackendConfig:
    """Backend server configuration."""

    name: str = ""
    servers: list[str] = field(default_factory=list)
    health_check_path: str = "/"
    health_check_interval: int = 1000  # milliseconds


@dataclass
class CertConfig:
    """Certificate configuration."""

    path: str = ""
    acme_enabled: bool = False
    acme_email: str = ""
    acme_domain: str = ""
    # Route53 DNS-01 challenge settings
    route53_enabled: bool = False
    route53_profile: str = "default"
    route53_hosted_zone_id: str = ""


@dataclass
class KeepalivedConfig:
    """Keepalived/VRRP configuration."""

    peers: list[str] = field(default_factory=list)  # Other node IPs/hostnames
    interface: str = ""  # Network interface for VIP (auto-detected if empty)
    use_multicast: bool = False  # Default: unicast
    advert_int: int = 1  # VRRP advertisement interval (seconds)


INSTALL_POINTER_PATH = Path("/etc/ober/install_path")


def _read_install_pointer() -> Path | None:
    """Read the install path recorded by ``ober bootstrap``, if any."""
    try:
        text = INSTALL_POINTER_PATH.read_text().rstrip("\r\n")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    path = Path(text).expanduser()
    # Ignore relative pointers and pointers to directories that no longer exist
    if not path.is_absolute() or not path.is_dir():
        return None
    return path


def _get_default_install_path() -> Path:
    """Resolve the install path.

    Order: ``OBER_INSTALL_PATH`` env var, the pointer file written by bootstrap
    (``INSTALL_POINTER_PATH``), ``sys.prefix`` when in a venv, then a temp dir.
    """
    import os
    import sys
    import tempfile

    env = os.environ.get("OBER_INSTALL_PATH", "").strip()
    if env:
        return Path(env)

    pointer = _read_install_pointer()
    if pointer is not None:
        return pointer

    # If running in a venv (e.g., pipx), use the venv path
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix)

    # Otherwise, use a temp directory (for tests/fallback only)
    return Path(tempfile.gettempdir()) / "ober"


@dataclass
class OberConfig:
    """Main Ober configuration."""

    install_path: Path = field(default_factory=_get_default_install_path)
    ha_mode: str = "bgp"  # "bgp" or "keepalived"
    bgp: BGPConfig = field(default_factory=BGPConfig)
    keepalived: KeepalivedConfig = field(default_factory=KeepalivedConfig)
    vips: list[VIPConfig] = field(default_factory=list)
    backends: list[BackendConfig] = field(default_factory=list)
    certs: CertConfig = field(default_factory=CertConfig)
    log_retention_days: int = 7
    stats_port: int = 8404
    security: SecurityConfig = field(default_factory=SecurityConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)
    mde: MDEConfig = field(default_factory=MDEConfig)
    _venv_path_override: Path | None = field(default=None, repr=False)

    @property
    def config_path(self) -> Path:
        """Path to main config file."""
        return self.install_path / "etc" / "ober.yaml"

    @property
    def haproxy_config_path(self) -> Path:
        """Path to HAProxy config file."""
        return self.install_path / "etc" / "haproxy" / "haproxy.cfg"

    @property
    def bgp_config_path(self) -> Path:
        """Path to ExaBGP config file."""
        return self.install_path / "etc" / "bgp" / "config.ini"

    @property
    def keepalived_config_path(self) -> Path:
        """Path to keepalived config file."""
        return self.install_path / "etc" / "keepalived" / "keepalived.conf"

    @property
    def certs_path(self) -> Path:
        """Path to certificates directory."""
        return self.install_path / "etc" / "certs"

    @property
    def venv_path(self) -> Path:
        """Path to Python venv (may be pipx venv or /opt/ober/venv)."""
        if self._venv_path_override is not None:
            return self._venv_path_override
        return self.install_path / "venv"

    @property
    def whitelist_path(self) -> Path:
        """Path to HTTP whitelist file."""
        return self.install_path / "etc" / "haproxy" / "whitelist.lst"

    @property
    def allowlist_path(self) -> Path:
        """Institution CIDRs allowed on internet VIPs (one per line)."""
        return self.install_path / "etc" / "haproxy" / "allowlist.lst"

    @property
    def trusted_path(self) -> Path:
        """Trusted networks, never rate limited or auto-banned (one per line)."""
        return self.install_path / "etc" / "haproxy" / "trusted.lst"

    @property
    def blocklist_path(self) -> Path:
        """Blocked IPs/CIDRs; also the ACL id used with the HAProxy runtime API."""
        return self.install_path / "etc" / "haproxy" / "blocklist.lst"

    @property
    def admin_socket_path(self) -> Path:
        """HAProxy admin socket (local only, root only)."""
        return Path("/run/ober/haproxy.sock")

    @property
    def watch_data_dir(self) -> Path:
        """Data directory of ober watch (raw logs, findings, blocklist state)."""
        if self.watch.data_dir:
            return Path(self.watch.data_dir).expanduser()
        return self.install_path / "var" / "watch"

    @classmethod
    def load(cls, path: Path | None = None) -> "OberConfig":
        """Load configuration from YAML file.

        Args:
            path: Path to config file. If None, uses default install path.

        Returns:
            OberConfig instance.
        """
        config = cls()

        if path is None:
            # Use the default config path from the instance
            path = config.config_path

        if path and path.exists():
            config._load_from_file(path)

        return config

    def _load_from_file(self, path: Path) -> None:
        """Load configuration from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        if "install_path" in data:
            self.install_path = Path(data["install_path"]).expanduser()

        if "ha_mode" in data:
            self.ha_mode = data["ha_mode"]

        if "bgp" in data:
            bgp_data = data["bgp"]
            self.bgp = BGPConfig(
                local_as=bgp_data.get("local_as", 65001),
                peer_as=bgp_data.get("peer_as", 65000),
                neighbors=bgp_data.get("neighbors", []),
                router_id=bgp_data.get("router_id", ""),
                local_address=bgp_data.get("local_address", ""),
                hold_time=bgp_data.get("hold_time", 3),
                bfd_enabled=bgp_data.get("bfd_enabled", True),
            )

        if "keepalived" in data:
            ka_data = data["keepalived"]
            self.keepalived = KeepalivedConfig(
                peers=ka_data.get("peers", []),
                interface=ka_data.get("interface", ""),
                use_multicast=ka_data.get("use_multicast", False),
                advert_int=ka_data.get("advert_int", 1),
            )

        if "vips" in data:
            self.vips = [
                VIPConfig(
                    address=v.get("address", ""),
                    interface=v.get("interface", "lo-vip"),
                    exposure=v.get("exposure", "internal"),
                )
                for v in data["vips"]
            ]

        if "backends" in data:
            self.backends = [
                BackendConfig(
                    name=b.get("name", ""),
                    servers=b.get("servers", []),
                    health_check_path=b.get("health_check_path", "/"),
                    health_check_interval=b.get("health_check_interval", 1000),
                )
                for b in data["backends"]
            ]

        if "certs" in data:
            cert_data = data["certs"]
            self.certs = CertConfig(
                path=cert_data.get("path", ""),
                acme_enabled=cert_data.get("acme_enabled", False),
                acme_email=cert_data.get("acme_email", ""),
                acme_domain=cert_data.get("acme_domain", ""),
                route53_enabled=cert_data.get("route53_enabled", False),
                route53_profile=cert_data.get("route53_profile", "default"),
                route53_hosted_zone_id=cert_data.get("route53_hosted_zone_id", ""),
            )

        self.log_retention_days = data.get("log_retention_days", 7)
        self.stats_port = data.get("stats_port", 8404)

        self.security = _section_from_dict(SecurityConfig, data.get("security"))
        self.watch = _section_from_dict(WatchConfig, data.get("watch"))
        self.sentinel = _section_from_dict(SentinelConfig, data.get("sentinel"))
        self.mde = _section_from_dict(MDEConfig, data.get("mde"))

        # Load venv_path if specified (for pipx installations)
        if "venv_path" in data:
            self._venv_path_override = Path(data["venv_path"])

    def save(self, path: Path | None = None) -> None:
        """Save configuration to YAML file.

        Args:
            path: Path to save to. Defaults to config_path.
        """
        if path is None:
            path = self.config_path

        path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "install_path": str(self.install_path),
            "ha_mode": self.ha_mode,
            "bgp": {
                "local_as": self.bgp.local_as,
                "peer_as": self.bgp.peer_as,
                "neighbors": self.bgp.neighbors,
                "router_id": self.bgp.router_id,
                "local_address": self.bgp.local_address,
                "hold_time": self.bgp.hold_time,
                "bfd_enabled": self.bgp.bfd_enabled,
            },
            "keepalived": {
                "peers": self.keepalived.peers,
                "interface": self.keepalived.interface,
                "use_multicast": self.keepalived.use_multicast,
                "advert_int": self.keepalived.advert_int,
            },
            "vips": [
                {"address": v.address, "interface": v.interface, "exposure": v.exposure}
                for v in self.vips
            ],
            "backends": [
                {
                    "name": b.name,
                    "servers": b.servers,
                    "health_check_path": b.health_check_path,
                    "health_check_interval": b.health_check_interval,
                }
                for b in self.backends
            ],
            "certs": {
                "path": self.certs.path,
                "acme_enabled": self.certs.acme_enabled,
                "acme_email": self.certs.acme_email,
                "acme_domain": self.certs.acme_domain,
                "route53_enabled": self.certs.route53_enabled,
                "route53_profile": self.certs.route53_profile,
                "route53_hosted_zone_id": self.certs.route53_hosted_zone_id,
            },
            "log_retention_days": self.log_retention_days,
            "stats_port": self.stats_port,
            "security": asdict(self.security),
            "watch": asdict(self.watch),
            "sentinel": asdict(self.sentinel),
            "mde": asdict(self.mde),
        }

        # Save venv_path if it differs from default (e.g., pipx venv)
        if self._venv_path_override is not None:
            data["venv_path"] = str(self._venv_path_override)

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def ensure_directories(self) -> None:
        """Create all required directories."""
        dirs = [
            self.install_path / "etc" / "haproxy",
            self.install_path / "etc" / "bgp",
            self.install_path / "etc" / "keepalived",
            self.install_path / "etc" / "certs",
        ]
        # Only create bin and venv dirs if not using an external venv (e.g., pipx)
        if self._venv_path_override is None:
            dirs.append(self.install_path / "bin")
            dirs.append(self.install_path / "venv")
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


def get_secrets_path() -> Path:
    """Get path to secrets file."""
    return Path.home() / ".ober" / "login"


def load_secrets() -> dict[str, str]:
    """Load secrets from ~/.ober/login."""
    secrets_path = get_secrets_path()
    if not secrets_path.exists():
        return {}

    secrets: dict[str, str] = {}
    with open(secrets_path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                secrets[key.strip()] = value.strip()
    return secrets


def save_secrets(secrets: dict[str, str]) -> None:
    """Save secrets to ~/.ober/login with secure permissions."""
    secrets_path = get_secrets_path()
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    with open(secrets_path, "w") as f:
        for key, value in secrets.items():
            f.write(f"{key}={value}\n")

    # Set permissions to 600
    os.chmod(secrets_path, 0o600)
