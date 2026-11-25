"""Tests for ober cert install (PEM checks, runtime hot swap, reload fallback)."""

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from ober.cli import main
from ober.commands.cert import check_pem
from ober.config import CertConfig, OberConfig

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")


def make_pem(tmp: Path, name: str, san: str = "DNS:s3.example.com,DNS:*.s3.example.com") -> str:
    """Self-signed EC certificate plus key as one PEM text."""
    cert, key = tmp / f"{name}.crt", tmp / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "30",
            "-subj", f"/CN={name}", "-addext", f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert.read_text() + key.read_text()


def test_check_pem(temp_dir: Path) -> None:
    good = make_pem(temp_dir, "a")
    assert check_pem(good, ["s3.example.com"]) == ([], [])
    errors, _ = check_pem(good, ["other.example.com"])
    assert errors and "does not cover other.example.com" in errors[0]
    _, warnings = check_pem(make_pem(temp_dir, "b", "DNS:s3.example.com"), ["s3.example.com"])
    assert "virtual-host" in warnings[0]
    other_key = make_pem(temp_dir, "c").split("-----END CERTIFICATE-----\n")[1]
    mixed = good.split("-----END CERTIFICATE-----\n")[0] + "-----END CERTIFICATE-----\n"
    errors, _ = check_pem(mixed + other_key, [])
    assert errors and "do not match" in errors[0]


def _install(temp_dir: Path, pem: str, runtime: MagicMock) -> tuple[Any, Path, MagicMock]:
    target = temp_dir / "certs" / "s3.pem"
    config = OberConfig(install_path=temp_dir, certs=CertConfig(path=str(target)))
    config.security.s3_hostnames = ["s3.example.com"]
    run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with (
        patch("ober.cli.SystemInfo", return_value=MagicMock(is_root=True)),
        patch("ober.commands.cert.OberConfig.load", return_value=config),
        patch("ober.commands.cert.ServiceInfo") as svc,
        patch("ober.commands.cert.HAProxyRuntime", return_value=runtime),
        patch("ober.commands.cert.run_command", run),
    ):
        svc.from_service_name.return_value = MagicMock(is_active=True)
        result = CliRunner().invoke(main, ["cert", "install", "-"], input=pem)
    return result, target, run


def test_install_hot_swaps_and_keeps_key_private(temp_dir: Path) -> None:
    runtime = MagicMock()
    runtime.send.side_effect = ["Transaction created for certificate x!\n", "Success!\n"]
    pem = make_pem(temp_dir, "a")
    result, target, run = _install(temp_dir, pem, runtime)
    assert result.exit_code == 0, result.output
    assert target.read_text() == pem
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert runtime.send.call_args_list[0].args == (f"set ssl cert {target}",)
    assert runtime.send.call_args_list[0].kwargs == {"payload": pem}
    run.assert_not_called()  # no reload


def test_install_falls_back_to_reload_and_refuses_bad_pem(temp_dir: Path) -> None:
    runtime = MagicMock()
    runtime.send.return_value = "unknown certificate name\n"
    result, _, run = _install(temp_dir, make_pem(temp_dir, "a"), runtime)
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(["systemctl", "reload", "ober-http"], check=False)
    result, target, _ = _install(temp_dir, make_pem(temp_dir, "x", "DNS:x.test"), MagicMock())
    assert result.exit_code == 1 and "does not cover s3.example.com" in result.output
    assert target.read_text() != ""  # the earlier file stays


def test_directory_target_and_all_nodes_name(temp_dir: Path) -> None:
    from ober.commands.cert import _target

    certs = temp_dir / "certs"
    certs.mkdir()
    config = OberConfig(install_path=temp_dir, certs=CertConfig(path=str(certs)))
    assert _target(config, "/x/s3.pem", None) == certs / "s3.pem"
    assert _target(config, "-", "s3.pem") == certs / "s3.pem"  # what --all-nodes sends
    for source, name in (("-", None), ("-", "../evil.pem")):
        with pytest.raises(click.UsageError):
            _target(config, source, name)
