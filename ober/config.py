#!/usr/bin/env python3
"""Configuration management for Ober."""

import contextlib
import ipaddress
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin

import click
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
    health_rise: int = 3  # good probes in a row before routes are announced
    health_fall: int = 2  # failed probes in a row before routes are withdrawn
    health_holddown: int = 5  # seconds after health start before the first announce


@dataclass
class VIPConfig:
    """Virtual IP configuration."""

    address: str = ""
    interface: str = "lo-vip"
    exposure: str = "internal"  # "internal" (unchanged behavior) or "internet" (hardened)
    owner: str = ""  # keepalived: node IP that holds this VIP; "" = md5 of the VIP picks one


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
    # Sources allowed on internal VIPs; [] = no restriction (pre-0.4 behavior)
    internal_networks: list[str] = field(default_factory=list)
    deny_admin_api: bool = True  # block RGW /admin on internet VIPs
    deny_anonymous: bool = False  # block unsigned requests on internet VIPs
    autoban_enabled: bool = True
    autoban_auth_failures: int = 50  # signed 401/403 (not HEAD/OPTIONS) per window
    autoban_unsigned_failures: int = 500  # unsigned denies per window (shared NATs)
    autoban_window_s: int = 60
    autoban_ban_s: int = 3600
    conn_limit_per_ip: int = 2000  # concurrent connections per IP, 0 = off
    internet_maxconn: int = 20000  # cap so Internet clients cannot starve internal VIPs
    timeout_client_s: int = 300
    timeout_server_s: int = 600  # long enough for CompleteMultipartUpload on huge objects
    log_target: str = ""  # "host:port" UDP syslog of ober watch; "" = log to stdout


@dataclass
class WatchConfig:
    """ober watch (analysis host) and blocklist sync (proxy nodes)."""

    syslog_listen: str = "0.0.0.0:5514"  # UDP, receives HAProxy JSON logs
    api_listen: str = "0.0.0.0:8405"  # HTTP, serves the blocklist to proxy nodes
    url: str = ""  # proxy nodes: where to pull the blocklist, e.g. http://watch01:8405
    # Proxy node IPs/CIDRs allowed to send logs; [] = accept any (warned at startup)
    syslog_allowed_sources: list[str] = field(default_factory=list)
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
    expiry_days: int = 1  # keep close to the block TTL
    max_active_indicators: int = 1000  # the 15,000 tenant quota is shared with the SOC


@dataclass
class BackendConfig:
    """Backend server configuration."""

    name: str = ""
    servers: list[str] = field(default_factory=list)
    health_check_path: str = "/"
    health_check_interval: int = 1000  # milliseconds
    fall: int = 3  # failed checks before a server is marked down
    rise: int = 2  # good checks before it is marked up again
    slowstart_s: int = 10  # ramp-up time for a recovered server (0 = off)
    maxconn: int = 0  # per-server connection limit, extra requests queue (0 = no limit)
    health_check_expect: str = ""  # e.g. "rstatus ^[234]"; empty = HAProxy default (2xx/3xx)
    tls: bool = False  # HTTPS to the servers (AWS S3, RGW behind TLS)
    tls_verify: bool = True  # verify the server cert (CA chain, and the host name for hostnames)
    tls_ca_file: str = ""  # CA bundle for a private CA; empty = the system CAs (@system-ca)


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
    gateway_check: str = ""  # IP to ping; when unreachable the node goes to FAULT ("" = off)


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
    # This node's IP for keepalived unicast_src_ip and BGP router-id/local-address defaults;
    # "" = the source address of the default route
    local_ip: str = ""
    # All ingress nodes (IP or SSH host name) for `ober cluster status`; keepalived mode
    # falls back to local_ip plus the peers
    nodes: list[str] = field(default_factory=list)
    bgp: BGPConfig = field(default_factory=BGPConfig)
    keepalived: KeepalivedConfig = field(default_factory=KeepalivedConfig)
    vips: list[VIPConfig] = field(default_factory=list)
    backends: list[BackendConfig] = field(default_factory=list)
    certs: CertConfig = field(default_factory=CertConfig)
    log_retention_days: int = 7
    stats_port: int = 8404
    drain_timeout: int = 30  # seconds `ober stop` waits for connections to close
    # Extra addresses for stats/metrics/health; 127.0.0.1 is always bound
    stats_bind: list[str] = field(default_factory=list)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)
    mde: MDEConfig = field(default_factory=MDEConfig)
    _venv_path_override: Path | None = field(default=None, repr=False)
    _load_errors: list[str] = field(default_factory=list, repr=False, compare=False)
    _load_warnings: list[str] = field(default_factory=list, repr=False, compare=False)
    # Raw content of ober.local.yaml: its keys are saved back there, not to ober.yaml
    _local: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

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

    def node_ip(self) -> str:
        """This node's IP: the ``local_ip`` override, else the default-route source address."""
        if self.local_ip:
            return self.local_ip
        from ober.system import SystemInfo

        return SystemInfo().get_local_ip() or "0.0.0.0"

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
    def server_state_path(self) -> Path:
        """HAProxy server-state-file: keeps drain/maint across reloads and restarts."""
        return self.install_path / "var" / "run" / "haproxy-server-state"

    @property
    def maintenance_path(self) -> Path:
        """Maintenance flag file ("1" = on, "0" or missing = off), also a keepalived track_file."""
        # In the state dir: ober-ha can only watch (track_file) a path in its ReadWritePaths
        return self.install_path / "var" / "run" / "maintenance"

    @property
    def health_state_path(self) -> Path:
        """Routes announced by the BGP health process (JSON)."""
        return self.install_path / "var" / "run" / "ober-health.json"

    @property
    def vrrp_state_path(self) -> Path:
        """VRRP state per instance, written by the keepalived notify hook (JSON)."""
        return self.install_path / "var" / "run" / "vrrp-state.json"

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
        """Load configuration from a YAML file.

        Never raises for bad content, so that ``stop``, ``status`` and ``logs`` still work.
        Problems are kept for ``validate()``; bad sections keep their defaults.
        ``ober.local.yaml`` next to the file, when present, overrides its keys (per-node values
        such as ``local_ip`` or ``bgp.router_id``, so ober.yaml can be the same on every node).
        """
        data = self._read_mapping(path)
        local = path.parent / LOCAL_CONFIG_NAME
        if local.exists():
            self._local = self._read_mapping(local) or {}
            data = _merge(data or {}, self._local)
        if not data:
            return

        known = {f.name for f in fields(self) if not f.name.startswith("_")} | {"venv_path"}
        for key in data:
            if key not in known:
                self._load_warnings.append(f"{key}: unknown key (ignored)")

        if "install_path" in data:
            self.install_path = Path(str(data["install_path"])).expanduser()
        for fld in fields(self):
            name, kind = fld.name, fld.type
            if name.startswith("_") or name == "install_path" or name not in data:
                continue
            value = data[name]
            if isinstance(kind, type) and is_dataclass(kind):
                value = self._section(kind, value, name)
            elif get_origin(kind) is list and is_dataclass(get_args(kind)[0]):
                value = self._section_list(get_args(kind)[0], value, name)
            setattr(self, name, value)

        # Load venv_path if specified (for pipx installations)
        if data.get("venv_path"):
            self._venv_path_override = Path(str(data["venv_path"]))

    def _read_mapping(self, path: Path) -> dict[str, Any] | None:
        """Parse one YAML file; record problems and return None when it is not usable."""
        try:
            with open(path) as f:
                data = yaml.load(f, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
        except yaml.MarkedYAMLError as e:
            mark = e.problem_mark
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            self._load_errors.append(f"{path}: YAML syntax error{where}: {e.problem}")
            return None
        except (OSError, yaml.YAMLError) as e:
            self._load_errors.append(f"{path}: cannot read: {e}")
            return None
        if data is not None and not isinstance(data, dict):
            self._load_errors.append(f"{path}: top level must be a mapping")
            return None
        return data

    def _section(self, cls: type[Any], data: Any, where: str) -> Any:
        """Build a config dataclass from a dict; record bad shapes and unknown keys."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            self._load_errors.append(f"{where}: must be a mapping, got {data!r}")
            return cls()
        names = {f.name for f in fields(cls)}
        for key in data:
            if key not in names:
                self._load_warnings.append(f"{where}.{key}: unknown key (ignored)")
        return cls(**{k: v for k, v in data.items() if k in names})

    def _section_list(self, cls: type[Any], data: Any, where: str) -> list[Any]:
        """Build a list of config dataclasses; record items that are not mappings."""
        if data is None:
            return []
        if not isinstance(data, list):
            self._load_errors.append(f"{where}: must be a list, got {data!r}")
            return []
        return [self._section(cls, item, f"{where}[{i}]") for i, item in enumerate(data)]

    def validate(self) -> tuple[list[str], list[str]]:
        """Check types, enums and address formats.

        Returns:
            (errors, warnings). All problems are reported, not only the first.
        """
        errors = list(self._load_errors)
        warnings = list(self._load_warnings)
        _check_types(self, "", errors)
        _check_values(self, errors)
        for i, vip in enumerate(self.vips):
            if not vip.owner or not isinstance(vip.owner, str):
                continue
            key = f"vips[{i}].owner"
            if self.ha_mode == "bgp":
                warnings.append(f"{key}: ignored in BGP mode (all nodes announce each VIP)")
            # Same node identity as get_vip_owner: local_ip (or the detected IP) plus peers
            elif vip.owner not in self.keepalived.peers and vip.owner != self.node_ip():
                errors.append(
                    f"{key}: must be local_ip or one of keepalived.peers, got {vip.owner!r}"
                )
        return errors, warnings

    def save(self, path: Path | None = None) -> None:
        """Save configuration to YAML file.

        Args:
            path: Path to save to. Defaults to config_path.
        """
        if path is None:
            path = self.config_path

        path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {}
        for fld in fields(self):
            if fld.name.startswith("_"):
                continue
            value = getattr(self, fld.name)
            if is_dataclass(value):
                value = asdict(value)  # type: ignore[arg-type]
            elif isinstance(value, list):
                value = [asdict(v) if is_dataclass(v) else v for v in value]  # type: ignore[arg-type]
            elif isinstance(value, Path):
                value = str(value)
            data[fld.name] = value

        # Save venv_path if it differs from default (e.g., pipx venv)
        if self._venv_path_override is not None:
            data["venv_path"] = str(self._venv_path_override)

        if self._local:
            local = _split(data, self._local)
            write_atomic(path.parent / LOCAL_CONFIG_NAME, yaml.dump(local, sort_keys=False))
        write_atomic(path, yaml.dump(data, default_flow_style=False, sort_keys=False))

    def ensure_directories(self) -> None:
        """Create all required directories."""
        dirs = [
            self.install_path / "etc" / "haproxy",
            self.install_path / "etc" / "bgp",
            self.install_path / "etc" / "keepalived",
            self.install_path / "etc" / "certs",
            self.health_state_path.parent,  # must exist: the HA units list it in ReadWritePaths
        ]
        # Only create bin and venv dirs if not using an external venv (e.g., pipx)
        if self._venv_path_override is None:
            dirs.append(self.install_path / "bin")
            dirs.append(self.install_path / "venv")
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


LOCAL_CONFIG_NAME = "ober.local.yaml"
# Values that must differ between nodes: a shared ober.yaml must leave them empty
PER_NODE_KEYS = (("local_ip",), ("bgp", "router_id"), ("bgp", "local_address"))


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep merge: mappings merge key by key, any other value in ``over`` replaces."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _split(data: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    """Move the keys that ``local`` has out of ``data``; return them (current values)."""
    out: dict[str, Any] = {}
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            out[key] = _split(data[key], value)
        else:
            out[key] = data.pop(key, value)
    return out


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Write a file with tmp-plus-rename, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # a crash must not leave an empty file after the rename
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys; PyYAML silently keeps the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = key_node.value
            if isinstance(key_node, yaml.ScalarNode) and key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


_HOSTPORT_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]+:\d{1,5}$")


def _type_problem(value: Any, expected: Any) -> str | None:
    """Return why ``value`` does not match the annotation ``expected``, or None."""
    if get_origin(expected) is list:
        if not isinstance(value, list):
            return "must be a list"
        item = get_args(expected)[0]
        if not is_dataclass(item) and not all(_type_problem(v, item) is None for v in value):
            return f"must be a list of {item.__name__}"
        return None
    if expected is bool:
        ok = isinstance(value, bool)
    elif expected is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected is str:
        ok = isinstance(value, str)
    elif expected is Path:
        ok = isinstance(value, (str, Path))
    else:
        return None
    return None if ok else f"must be {expected.__name__}"


def _check_types(obj: Any, where: str, errors: list[str]) -> None:
    """Check each dataclass field against its annotation, recursing into sections."""
    for f in fields(obj):
        if f.name.startswith("_"):
            continue
        value, key = getattr(obj, f.name), f"{where}{f.name}"
        problem = _type_problem(value, f.type)
        if problem:
            errors.append(f"{key}: {problem}, got {value!r}")
        elif is_dataclass(f.type):
            _check_types(value, f"{key}.", errors)
        elif get_origin(f.type) is list and is_dataclass(get_args(f.type)[0]):
            for i, item in enumerate(value):
                _check_types(item, f"{key}[{i}].", errors)


def _is_ip(value: Any) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_network(value: Any) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except (TypeError, ValueError):
        return False


def _is_single_ipv4(value: Any) -> bool:
    """A VIP: one IPv4 address, bare or /32 (a wider prefix adds a connected route)."""
    try:
        iface = ipaddress.ip_interface(value)
    except (TypeError, ValueError):
        return False
    return iface.version == 4 and iface.network.prefixlen == 32


def server_name(address: str) -> str:
    """Stable HAProxy server name from its address, e.g. ``rgw1:7480`` -> ``rgw1_7480``."""
    return re.sub(r"[^A-Za-z0-9.-]+", "_", address).strip("_")


def _is_hostport(value: Any) -> bool:
    if not isinstance(value, str) or not _HOSTPORT_RE.match(value):
        return False
    return 1 <= int(value.rsplit(":", 1)[1]) <= 65535


def _in_range(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _check_values(cfg: "OberConfig", errors: list[str]) -> None:
    """Check enums, ranges and address formats. Wrong types are reported by _check_types."""

    def need(ok: bool, key: str, what: str, value: Any) -> None:
        if not ok:
            errors.append(f"{key}: {what}, got {value!r}")

    def each(key: str, values: Any, ok: Any, what: str) -> None:
        for i, v in enumerate(values if isinstance(values, list) else []):
            need(ok(v), f"{key}[{i}]", what, v)

    max_port, max_as = 65535, 4294967295
    need(cfg.ha_mode in ("bgp", "keepalived"), "ha_mode", "must be bgp or keepalived", cfg.ha_mode)
    need(
        cfg.local_ip == "" or _is_ip(cfg.local_ip),
        "local_ip",
        "must be an IP or empty",
        cfg.local_ip,
    )
    need(_in_range(cfg.stats_port, 1, max_port), "stats_port", "must be 1-65535", cfg.stats_port)
    each("stats_bind", cfg.stats_bind, _is_ip, "must be an IP address")
    if any(v.exposure == "internet" for v in cfg.vips):
        # stats/metrics have no auth: a wildcard bind answers on every address of the node
        each(
            "stats_bind",
            cfg.stats_bind,
            lambda a: not _is_ip(a) or not ipaddress.ip_address(a).is_unspecified,
            "must not be 0.0.0.0 or :: when a VIP has exposure internet",
        )
    need(
        _in_range(cfg.log_retention_days, 1, 36500),
        "log_retention_days",
        "must be >= 1",
        cfg.log_retention_days,
    )

    bgp = cfg.bgp
    need(_in_range(bgp.local_as, 1, max_as), "bgp.local_as", "must be 1-4294967295", bgp.local_as)
    need(_in_range(bgp.peer_as, 1, max_as), "bgp.peer_as", "must be 1-4294967295", bgp.peer_as)
    each("bgp.neighbors", bgp.neighbors, _is_ip, "must be an IP address")
    for key in ("router_id", "local_address"):
        value = getattr(bgp, key)
        need(value == "" or _is_ip(value), f"bgp.{key}", "must be an IP or empty", value)
    need(
        bgp.hold_time == 0 or _in_range(bgp.hold_time, 3, 65535),
        "bgp.hold_time",
        "must be 0 or 3-65535",
        bgp.hold_time,
    )

    need(_in_range(bgp.health_rise, 1, 1000), "bgp.health_rise", "must be >= 1", bgp.health_rise)
    need(_in_range(bgp.health_fall, 1, 1000), "bgp.health_fall", "must be >= 1", bgp.health_fall)
    need(
        _in_range(bgp.health_holddown, 0, 86400),
        "bgp.health_holddown",
        "must be >= 0",
        bgp.health_holddown,
    )
    need(_in_range(cfg.drain_timeout, 1, 86400), "drain_timeout", "must be > 0", cfg.drain_timeout)

    ka = cfg.keepalived
    each("keepalived.peers", ka.peers, _is_ip, "must be an IP address")
    need(
        ka.gateway_check == "" or _is_ip(ka.gateway_check),
        "keepalived.gateway_check",
        "must be an IP or empty",
        ka.gateway_check,
    )
    need(_in_range(ka.advert_int, 1, 255), "keepalived.advert_int", "must be 1-255", ka.advert_int)

    seen: set[str] = set()
    for i, vip in enumerate(cfg.vips):
        key = f"vips[{i}]"
        need(
            _is_single_ipv4(vip.address),
            f"{key}.address",
            "must be a single address (/32)",
            vip.address,
        )
        need(
            vip.exposure in ("internal", "internet"),
            f"{key}.exposure",
            "must be internal or internet",
            vip.exposure,
        )
        ip = str(vip.address).split("/")[0]
        need(ip not in seen, f"{key}.address", "is a duplicate", vip.address)
        seen.add(ip)

    for i, backend in enumerate(cfg.backends):
        key = f"backends[{i}]"
        need(
            isinstance(backend.name, str) and bool(re.match(r"^[A-Za-z0-9_.:-]+$", backend.name)),
            f"{key}.name",
            "must be a non-empty name of letters, digits, _ . : -",
            backend.name,
        )
        each(f"{key}.servers", backend.servers, _is_hostport, "must be host:port")
        need(
            isinstance(backend.health_check_path, str)
            and backend.health_check_path.startswith("/"),
            f"{key}.health_check_path",
            "must start with /",
            backend.health_check_path,
        )
        need(
            _in_range(backend.health_check_interval, 100, 3600000),
            f"{key}.health_check_interval",
            "must be >= 100 (ms)",
            backend.health_check_interval,
        )
        for name, low in (("fall", 1), ("rise", 1), ("slowstart_s", 0), ("maxconn", 0)):
            value = getattr(backend, name)
            need(_in_range(value, low, 1000000), f"{key}.{name}", f"must be >= {low}", value)
        need(
            isinstance(backend.health_check_expect, str)
            and (
                not backend.health_check_expect
                or bool(re.match(r"^r?status [^\s]+$", backend.health_check_expect))
            ),
            f"{key}.health_check_expect",
            "must be empty, 'status <codes>' or 'rstatus <regex>'",
            backend.health_check_expect,
        )
        need(
            isinstance(backend.tls_ca_file, str)
            and (not backend.tls_ca_file or bool(re.match(r"^/\S+$", backend.tls_ca_file))),
            f"{key}.tls_ca_file",
            "must be empty or an absolute path without spaces",
            backend.tls_ca_file,
        )
        names = [server_name(str(s)) for s in backend.servers]
        need(
            len(names) == len(set(names)),
            f"{key}.servers",
            "has duplicates (after mapping to HAProxy server names)",
            backend.servers,
        )

    sec = cfg.security
    for key, low, high in (
        ("timeout_client_s", 1, 86400),
        ("timeout_server_s", 1, 86400),
        ("autoban_window_s", 1, 2**31 - 1),
        ("autoban_ban_s", 1, 2**31 - 1),
        ("internet_maxconn", 1, 2**31 - 1),
        ("conn_limit_per_ip", 0, 2**31 - 1),
        ("autoban_auth_failures", 0, 2**31 - 1),
        ("autoban_unsigned_failures", 0, 2**31 - 1),
    ):
        value = getattr(sec, key)
        what = f"must be {low}-{high}" if high == 86400 else f"must be >= {low}"
        need(_in_range(value, low, high), f"security.{key}", what, value)
    for key in ("allowlist", "trusted_networks", "internal_networks"):
        each(f"security.{key}", getattr(sec, key), _is_network, "must be an IP or CIDR")
    need(
        sec.log_target == "" or _is_hostport(sec.log_target),
        "security.log_target",
        "must be host:port or empty",
        sec.log_target,
    )

    watch = cfg.watch
    for key in ("syslog_listen", "api_listen"):
        value = getattr(watch, key)
        need(_is_hostport(value), f"watch.{key}", "must be host:port", value)
    each(
        "watch.syslog_allowed_sources",
        watch.syslog_allowed_sources,
        _is_network,
        "must be an IP or CIDR",
    )


def exit_if_invalid(config: OberConfig, ctx: click.Context) -> None:
    """Print every problem of ``config`` and exit 1 if there is an error."""
    errors, warnings = config.validate()
    for warning in warnings:
        click.echo(f"Warning: {config.config_path}: {warning}", err=True)
    if errors:
        click.echo(f"Error: {config.config_path} is not valid:", err=True)
        for error in errors:
            click.echo(f"  - {error}", err=True)
        ctx.exit(1)


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
