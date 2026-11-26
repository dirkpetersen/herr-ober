"""Tests for deploy/lego/ober-lego-hook with a fake `ober` on PATH."""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "deploy" / "lego" / "ober-lego-hook"


def _run(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "bin" / "ober"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/args\ncat > {tmp_path}/stdin\n')
    fake.chmod(0o755)
    (tmp_path / "c.crt").write_text("CERT\n")
    (tmp_path / "c.key").write_text("KEY\n")
    full = {"PATH": f"{fake.parent}:{os.environ['PATH']}", **env}
    return subprocess.run(["sh", str(HOOK)], env=full, capture_output=True, text=True)


@pytest.mark.parametrize("prefix", ["LEGO_HOOK_", "LEGO_"])  # lego v5, lego v4
def test_hook_pipes_cert_and_key(tmp_path: Path, prefix: str) -> None:
    env = {f"{prefix}CERT_PATH": f"{tmp_path}/c.crt", f"{prefix}CERT_KEY_PATH": f"{tmp_path}/c.key"}
    result = _run(tmp_path, {**env, "OBER_CERT_NAME": "s3.pem"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "args").read_text() == "cert install --all-nodes --name s3.pem -\n"
    assert (tmp_path / "stdin").read_text() == "CERT\nKEY\n"


def test_hook_without_name(tmp_path: Path) -> None:
    env = {
        "LEGO_HOOK_CERT_PATH": f"{tmp_path}/c.crt",
        "LEGO_HOOK_CERT_KEY_PATH": f"{tmp_path}/c.key",
    }
    assert _run(tmp_path, env).returncode == 0
    assert (tmp_path / "args").read_text() == "cert install --all-nodes -\n"


def test_hook_fails_without_lego_env(tmp_path: Path) -> None:
    result = _run(tmp_path, {})
    assert result.returncode != 0
    assert not (tmp_path / "args").exists()
