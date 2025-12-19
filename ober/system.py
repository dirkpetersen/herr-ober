#!/usr/bin/env python3
"""System detection and information utilities."""

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class OSFamily(Enum):
    """Supported OS families."""

    DEBIAN = "debian"
    RHEL = "rhel"
    UNKNOWN = "unknown"


@dataclass
class SystemInfo:
    """System information and detection."""

    os_family: OSFamily = OSFamily.UNKNOWN
    os_name: str = ""
    os_version: str = ""
    os_codename: str = ""
    python_version: str = field(default_factory=lambda: platform.python_version())
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)
    hostname: str = field(default_factory=platform.node)
    arch: str = field(default_factory=platform.machine)

    def __post_init__(self) -> None:
        """Detect OS after initialization."""
        self._detect_os()

    def _detect_os(self) -> None:
        """Detect the operating system family and version."""
        if Path("/etc/os-release").exists():
            os_release = self._parse_os_release()
            os_id = os_release.get("ID", "").lower()
            os_id_like = os_release.get("ID_LIKE", "").lower()

            self.os_name = os_release.get("NAME", "Unknown")
            self.os_version = os_release.get("VERSION_ID", "")
            self.os_codename = os_release.get("VERSION_CODENAME", "")

            if os_id in ("ubuntu", "debian") or "debian" in os_id_like:
                self.os_family = OSFamily.DEBIAN
            elif (
                os_id in ("rhel", "centos", "rocky", "almalinux", "fedora") or "rhel" in os_id_like
            ):
                self.os_family = OSFamily.RHEL

    def _parse_os_release(self) -> dict[str, str]:
        """Parse /etc/os-release file."""
        result: dict[str, str] = {}
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, value = line.split("=", 1)
                        result[key] = value.strip('"')
        except OSError:
            pass
        return result

    @property
    def is_supported(self) -> bool:
        """Check if OS is supported."""
        if self.os_family == OSFamily.UNKNOWN:
            return False
        if self.os_family == OSFamily.RHEL:
            try:
                major_version = int(self.os_version.split(".")[0])
                return major_version >= 10
            except (ValueError, IndexError):
                return False
        return True

    @property
    def package_manager(self) -> str:
        """Get the package manager for this OS."""
        if self.os_family == OSFamily.DEBIAN:
            return "apt"
        elif self.os_family == OSFamily.RHEL:
            return "dnf"
        return ""

    def check_python_version(self, min_version: tuple[int, int] = (3, 12)) -> bool:
        """Check if Python version meets minimum requirement."""
        return sys.version_info >= min_version

    def get_local_ip(self) -> str | None:
        """Get the primary local IP address."""
        try:
            result = subprocess.run(
                ["ip", "-4", "route", "get", "1.1.1.1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
                if match:
                    return match.group(1)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None


@dataclass
class ServiceInfo:
    """Information about a systemd service."""

    name: str
    is_active: bool = False
    is_enabled: bool = False
    status: str = "unknown"
    pid: int | None = None

    @classmethod
    def from_service_name(cls, name: str) -> "ServiceInfo":
        """Create ServiceInfo by querying systemd."""
        info = cls(name=name)
        info.refresh()
        return info

    def refresh(self) -> None:
        """Refresh service status from systemd."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.status = result.stdout.strip()
            self.is_active = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self.status = "unknown"
            self.is_active = False

        try:
            result = subprocess.run(
                ["systemctl", "is-enabled", self.name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.is_enabled = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            self.is_enabled = False

        if self.is_active:
            try:
                result = subprocess.run(
                    ["systemctl", "show", self.name, "--property=MainPID", "--value"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    self.pid = int(result.stdout.strip())
            except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
                pass


def check_command_exists(command: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(command) is not None


def get_haproxy_version() -> str | None:
    """Get installed HAProxy version."""
    try:
        result = subprocess.run(
            ["haproxy", "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            match = re.search(r"version\s+(\d+\.\d+\.\d+)", result.stdout)
            if match:
                return match.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_exabgp_version() -> str | None:
    """Get installed ExaBGP version.

    Checks:
    1. Current venv (sys.prefix) - handles pipx installs
    2. System exabgp command
    """
    pip_paths: list[Path] = []

    # Check the current venv (if running in one, e.g., pipx)
    if sys.prefix != sys.base_prefix:
        current_venv = Path(sys.prefix)
        pip_paths.append(current_venv / "bin" / "pip")

    for pip_path in pip_paths:
        # Get the venv's bin directory
        bin_dir = pip_path.parent
        python_path = bin_dir / "python"

        # Try pip binary first, then fall back to python -m pip (for pipx venvs)
        commands = []
        if pip_path.exists():
            commands.append([str(pip_path), "show", "exabgp"])
        if python_path.exists():
            commands.append([str(python_path), "-m", "pip", "show", "exabgp"])

        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if line.startswith("Version:"):
                            return line.split(":")[1].strip()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

    # Fallback: try system exabgp --version (for ExaBGP 4.x)
    try:
        result = subprocess.run(
            ["exabgp", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # ExaBGP outputs version on stderr
        output = result.stdout + result.stderr
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def run_command(
    cmd: list[str],
    check: bool = True,
    capture: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run a command with standard options.

    Args:
        cmd: Command and arguments to run.
        check: Raise exception on non-zero exit.
        capture: Capture stdout/stderr.
        timeout: Command timeout in seconds.

    Returns:
        CompletedProcess with results.

    Raises:
        subprocess.CalledProcessError: If check=True and command fails.
        subprocess.TimeoutExpired: If command times out.
    """
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
