"""Tests for generated configuration files."""

import re
import sys

from ober.commands.config import _generate_exabgp_config, _generate_haproxy_config
from ober.config import BackendConfig, OberConfig


def _refs_and_sections(text: str) -> tuple[set[str], set[str]]:
    refs = set(re.findall(r"^\s*default_backend\s+(\S+)", text, re.MULTILINE))
    sections = set(re.findall(r"^backend\s+(\S+)", text, re.MULTILINE))
    return refs, sections


def test_default_backend_exists(sample_config: OberConfig) -> None:
    _generate_haproxy_config(sample_config)
    text = sample_config.haproxy_config_path.read_text()
    refs, sections = _refs_and_sections(text)
    assert refs == {"s3_backend"}
    assert refs <= sections


def test_default_backend_uses_first_backend(sample_config: OberConfig) -> None:
    sample_config.backends = [
        BackendConfig(name="s3_backend_1", servers=["rgw1:7480"]),
        BackendConfig(name="s3_backend_2", servers=["rgw2:7480"]),
    ]
    _generate_haproxy_config(sample_config)
    refs, sections = _refs_and_sections(sample_config.haproxy_config_path.read_text())
    assert refs == {"s3_backend_1"}
    assert refs <= sections


def test_no_backends_omits_frontend(sample_config: OberConfig) -> None:
    sample_config.backends = []
    _generate_haproxy_config(sample_config)
    text = sample_config.haproxy_config_path.read_text()
    refs, sections = _refs_and_sections(text)
    assert "frontend s3_front" not in text
    assert refs == set()
    assert "default_backend" not in text
    assert sections == set()


def test_exabgp_health_uses_running_interpreter(sample_config: OberConfig) -> None:
    sample_config.bgp.neighbors = ["10.0.0.1"]
    _generate_exabgp_config(sample_config)
    text = sample_config.bgp_config_path.read_text()
    assert f"run {sys.executable} -m ober.commands.health;" in text
    assert str(sample_config.venv_path / "bin" / "python") not in text
