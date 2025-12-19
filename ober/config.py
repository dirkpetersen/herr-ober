#!/usr/bin/env python3
"""Configuration management for Ober."""

import os
from dataclasses import dataclass, field
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


def _get_default_install_path() -> Path:
    """Get default install path - current venv if in one, otherwise temp dir."""
    import sys
    import tempfile

    # If running in a venv (e.g., pipx), use the venv path
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix)

    # Otherwise, use a temp directory (for tests/fallback only)
    return Path(tempfile.gettempdir()) / "ober"


@dataclass
class OberConfig:
    """Main Ober configuration."""

    install_path: Path = field(default_factory=_get_default_install_path)
    bgp: BGPConfig = field(default_factory=BGPConfig)
    vips: list[VIPConfig] = field(default_factory=list)
    backends: list[BackendConfig] = field(default_factory=list)
    certs: CertConfig = field(default_factory=CertConfig)
    log_retention_days: int = 7
    stats_port: int = 8404
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

        if "vips" in data:
            self.vips = [
                VIPConfig(
                    address=v.get("address", ""),
                    interface=v.get("interface", "lo-vip"),
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
            "bgp": {
                "local_as": self.bgp.local_as,
                "peer_as": self.bgp.peer_as,
                "neighbors": self.bgp.neighbors,
                "router_id": self.bgp.router_id,
                "local_address": self.bgp.local_address,
                "hold_time": self.bgp.hold_time,
                "bfd_enabled": self.bgp.bfd_enabled,
            },
            "vips": [{"address": v.address, "interface": v.interface} for v in self.vips],
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
