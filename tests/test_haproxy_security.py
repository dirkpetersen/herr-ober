"""Structure and `haproxy -c` validation of the generated (hardened) haproxy.cfg."""

import itertools
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from ober.commands.config import (
    _apply_configuration,
    _generate_haproxy_config,
    _haproxy_log_format,
    _write_security_lists,
)
from ober.config import BackendConfig, CertConfig, OberConfig, SecurityConfig, VIPConfig
from ober.watch.models import LOG_FIELDS

HAPROXY = shutil.which("haproxy") or (
    "/usr/sbin/haproxy" if Path("/usr/sbin/haproxy").exists() else ""
)
needs_haproxy = pytest.mark.skipif(not HAPROXY, reason="haproxy not installed")


@pytest.fixture(scope="module")
def cert_pem(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Self-signed ECDSA cert + key in one PEM (needed so `haproxy -c` finds the file)."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"),
            "-days", "2", "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    pem = d / "s.pem"
    pem.write_text((d / "c.pem").read_text() + (d / "k.pem").read_text())
    return pem


@pytest.fixture(scope="module")
def short_sock() -> Iterator[Path]:
    """Admin socket path; AF_UNIX paths are limited to ~100 chars so avoid pytest's tmp dirs."""
    d = Path(tempfile.mkdtemp(prefix="ob"))
    yield d / "h.sock"
    shutil.rmtree(d, ignore_errors=True)


def make_config(
    root: Path,
    cert: Path,
    shape: str = "mixed",
    **security: object,
) -> OberConfig:
    """Config with VIPs of the given shape: "internal", "internet" or "mixed"."""
    config = OberConfig(install_path=root)
    config.ensure_directories()
    exposures = {
        "internal": ["internal"],
        "internet": ["internet"],
        "mixed": ["internal", "internet"],
    }[shape]
    config.vips = [
        VIPConfig(address=f"10.0.100.{i + 1}/32", exposure=e) for i, e in enumerate(exposures)
    ]
    config.backends = [BackendConfig(name="s3_backend", servers=["10.1.0.1:7480"])]
    config.certs = CertConfig(path=str(cert))
    config.security = SecurityConfig(**security)  # type: ignore[arg-type]
    return config


def generate(config: OberConfig, sock: Path) -> str:
    _write_security_lists(config)
    _generate_haproxy_config(config, socket_path=sock)
    return config.haproxy_config_path.read_text()


def section(text: str, name: str) -> str:
    """Text of one `frontend <name>` section."""
    m = re.search(rf"^frontend {name}\n(.*?)(?=^\S)", text + "\nx", re.MULTILINE | re.DOTALL)
    assert m, f"frontend {name} missing"
    return m.group(1)


def haproxy_check(config: OberConfig) -> str:
    result = subprocess.run(
        [HAPROXY, "-c", "-f", str(config.haproxy_config_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stderr + result.stdout, result.stdout + result.stderr
    return result.stdout + result.stderr


# -- structure ---------------------------------------------------------------------------


def test_global_and_defaults(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem, timeout_client_s=111, timeout_server_s=222)
    text = generate(config, short_sock)
    assert f"stats socket {short_sock} mode 600 level admin" in text
    assert "ssl-default-bind-options ssl-min-ver TLSv1.2" in text
    assert "ssl-default-bind-ciphersuites TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384" in text
    assert "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256" in text
    assert "tune.ssl.cachesize" in text
    assert "no-tls-tickets" not in text
    assert "default-dh-param" not in text
    assert "log stdout format raw local0" in text
    assert "timeout client 111s" in text
    assert "timeout server 222s" in text
    assert "no log" in section(text, "stats")


def test_default_admin_socket_path(tmp_path: Path, cert_pem: Path) -> None:
    config = make_config(tmp_path, cert_pem)
    _generate_haproxy_config(config)
    assert "stats socket /run/ober/haproxy.sock mode 600 level admin" in (
        config.haproxy_config_path.read_text()
    )


def test_log_target_is_udp_rfc5424(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem, log_target="10.9.9.9:5514")
    text = generate(config, short_sock)
    assert "log 10.9.9.9:5514 len 8192 format rfc5424 local0" in text
    assert "log stdout" not in text


@pytest.mark.parametrize("target", ["nohost", "a b:5514", "x:5514\nglobal"])
def test_bad_log_target_rejected(tmp_path: Path, cert_pem: Path, target: str) -> None:
    config = make_config(tmp_path, cert_pem, log_target=target)
    with pytest.raises(ValueError):
        _generate_haproxy_config(config)


def test_bad_hostname_rejected(tmp_path: Path, cert_pem: Path) -> None:
    config = make_config(tmp_path, cert_pem, s3_hostnames=["a b\nglobal"])
    with pytest.raises(ValueError):
        _generate_haproxy_config(config)


def test_internal_only(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    text = generate(make_config(tmp_path, cert_pem, "internal"), short_sock)
    assert "frontend s3_internal" in text
    assert "frontend s3_internet" not in text
    internal = section(text, "s3_internal")
    assert "bind 10.0.100.1:443 ssl crt" in internal
    assert "alpn http/1.1" in internal
    assert "option forwardfor" in internal
    assert "X-Forwarded-Proto https" in internal
    assert "tcp-request connection reject if { src -f" in internal
    assert "blocklist.lst" in internal
    assert "stick-table" not in internal
    assert "http-request deny" not in internal


def test_default_exposure_is_internal(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem, "internal")
    assert VIPConfig(address="10.0.0.1/32").exposure == "internal"
    text = generate(config, short_sock)
    assert "frontend s3_front" not in text


def test_internet_only(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    text = generate(make_config(tmp_path, cert_pem, "internet"), short_sock)
    assert "frontend s3_internal" not in text
    assert "frontend s3_internet" in text
    assert "bind 10.0.100.1:443 ssl crt" in section(text, "s3_internet")


def test_mixed_binds_split_by_exposure(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    text = generate(make_config(tmp_path, cert_pem, "mixed"), short_sock)
    assert "bind 10.0.100.1:443" in section(text, "s3_internal")
    assert "bind 10.0.100.2:443" in section(text, "s3_internet")
    assert text.count("default_backend s3_backend") == 2
    assert text.count("\nbackend s3_backend") == 1


def test_internet_rule_order(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(
        tmp_path,
        cert_pem,
        "internet",
        s3_hostnames=["s3.example.org"],
        allowlist=["192.0.2.0/24"],
        deny_anonymous=True,
        conn_limit_per_ip=7,
        autoban_auth_failures=11,
        autoban_window_s=22,
        autoban_ban_s=333,
    )
    text = section(generate(config, short_sock), "s3_internet")

    def pos(needle: str) -> int:
        assert needle in text, needle
        return text.index(needle)

    order = [
        "maxconn 20000",
        "stick-table type ipv6 size 1m expire 355s store conn_cur,gpc(2),gpc_rate(2,22s),gpt(1)",
        f"reject if {{ src -f {config.blocklist_path} }}",
        f"reject unless {{ src -f {config.allowlist_path} }} or {{ src -f {config.trusted_path} }}",
        f"track-sc0 src unless {{ src -f {config.trusted_path} }}",
        "tcp-request connection reject if { sc_get_gpt(0,0),sub(sess.now) gt 0 }",
        "tcp-request connection reject if { sc_conn_cur(0) gt 7 }",
        "http-request reject if { sc_get_gpt(0,0),sub(txn.now) gt 0 }",
        "http-request del-header X-Forwarded-For",
        "option forwardfor",
        "acl s3_host var(sess.o_host) -m end .s3.example.org",
        "method GET HEAD PUT POST DELETE OPTIONS",
        "admin(/|$)",
        "req.hdr_cnt(authorization) eq 0",
        "req.fhdr_cnt(authorization) gt 1",
        "http-request sc-inc-gpc(0,0)",
        "http-request sc-inc-gpc(1,0)",
        "http-request sc-set-gpt(0,0) date(333)",
        "http-request deny deny_status 403",
        "http-response sc-inc-gpc(0,0) if { status 401 403 } { var(sess.o_akey),length gt 0 } "
        "!{ method HEAD OPTIONS }",
        "http-response sc-inc-gpc(1,0) if { status 401 403 } !{ var(sess.o_akey),length gt 0 }",
        "http-response sc-set-gpt(0,0) date(333) if { status 401 403 }",
        "log-format",
    ]
    positions = [pos(o) for o in order]
    assert positions == sorted(positions)
    # pre-TLS rejects are all `tcp-request connection`, never `tcp-request content`
    assert "tcp-request content" not in text
    # SigV4: nothing rewrites the Host header, the path or the Authorization header
    for forbidden in ("set-header host", "set-path", "set-uri", "replace-", "set-query"):
        assert forbidden not in text.lower()
    # only the client-controlled forwarding headers are removed
    assert re.findall(r"del-header (\S+)", text) == ["X-Forwarded-For", "Forwarded", "X-Real-IP"]
    assert text.index("del-header X-Real-IP") < text.index("option forwardfor")
    assert "sc_gpc_rate(0,0) gt 11" in text and "sc_gpc_rate(1,0) gt 500" in text


def test_internet_flags_toggle_sections(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(
        tmp_path,
        cert_pem,
        "internet",
        allowlist_enabled=False,
        deny_admin_api=False,
        autoban_enabled=False,
        conn_limit_per_ip=0,
    )
    text = section(generate(config, short_sock), "s3_internet")
    assert "allowlist.lst" not in text
    assert "stick-table" not in text and "track-sc0" not in text
    assert "admin(/|$)" not in text
    assert "maxconn 20000" in text
    assert "sc-inc-gpc" not in text
    assert "authorization) eq 0" not in text  # deny_anonymous default off
    assert "acl s3_host" not in text  # no hostnames configured
    assert "blocklist.lst" in text  # blocklist always applies


def test_conn_limit_only_table(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem, "internet", autoban_enabled=False, conn_limit_per_ip=9)
    text = section(generate(config, short_sock), "s3_internet")
    assert "store conn_cur" in text and "gpc(" not in text
    assert "sc_conn_cur(0) gt 9" in text


def test_json_log_format_matches_contract() -> None:
    for frontend in ("s3_internal", "s3_internet"):
        fmt = _haproxy_log_format(frontend)
        assert fmt.startswith("%{+json}o ")
        names = re.findall(r"%\((\w+)(?::(\w+))?\)", fmt)
        assert tuple(n for n, _ in names) == LOG_FIELDS
        types = {n: t for n, t in names if t}
        assert types == {"presigned": "bool"}
        assert f"[str({frontend})]" in fmt


def test_log_format_never_logs_request_target_or_signature(
    tmp_path: Path, cert_pem: Path, short_sock: Path
) -> None:
    text = generate(make_config(tmp_path, cert_pem, "internet"), short_sock)
    fmt = _haproxy_log_format("s3_internet")
    for forbidden in ("%r", "%hr", "%HU", "%HP", "%HQ", "url", "query", "path", "authorization"):
        assert forbidden not in fmt, forbidden
    # Authorization is read as a whole header (it contains commas), only behind a regex
    # guard, and cut at '/' (SigV4) or ':' (SigV2) before anything is stored.
    for line in text.splitlines():
        if "set-var(sess.o_akey) req.fhdr(authorization)" in line:
            assert "-m reg" in line
            assert "field(1,/)" in line or "field(1,:)" in line
    assert "req.hdr(authorization)" not in text  # comma-splitting accessor would mangle it


# -- _apply_configuration ----------------------------------------------------------------


def test_apply_writes_lists_and_keeps_blocklist(tmp_path: Path, cert_pem: Path) -> None:
    config = make_config(
        tmp_path,
        cert_pem,
        allowlist=["192.0.2.0/24", "198.51.100.7"],
        trusted_networks=["10.0.0.0/8"],
    )
    _apply_configuration_for_test(config)
    assert config.allowlist_path.read_text() == "192.0.2.0/24\n198.51.100.7\n"
    assert config.trusted_path.read_text() == "10.0.0.0/8\n"
    assert config.blocklist_path.read_text() == ""

    # ober watch owns the blocklist at runtime: never overwritten
    config.blocklist_path.write_text("203.0.113.9\n")
    config.security.allowlist = []
    _apply_configuration_for_test(config)
    assert config.blocklist_path.read_text() == "203.0.113.9\n"
    assert config.allowlist_path.read_text() == ""  # always written, even when empty
    assert config.trusted_path.exists()


def _apply_configuration_for_test(config: OberConfig, root: bool = False) -> list[list[str]]:
    """Run the real _apply_configuration; its other generators need BGP mode only.

    Never touches /etc or /run: systemd and run dirs live under the install path.
    """
    from unittest.mock import patch

    calls: list[list[str]] = []
    with (
        patch("ober.commands.config._generate_exabgp_config"),
        patch("ober.commands.config._configure_vip_interface"),
        patch("ober.system.run_command", side_effect=lambda cmd, **_: calls.append(cmd)),
    ):
        _apply_configuration(
            config,
            systemd_dir=config.install_path / "systemd",
            run_dir=config.install_path / "run" / "ober",
            is_root=root,
        )
    return calls


def test_apply_prepares_runtime_dir_as_root(tmp_path: Path, cert_pem: Path) -> None:
    config = make_config(tmp_path, cert_pem)
    calls = _apply_configuration_for_test(config, root=True)
    dropin = tmp_path / "systemd" / "ober-http.service.d" / "10-ober-runtime.conf"
    assert dropin.read_text().splitlines() == [
        "[Service]",
        "RuntimeDirectory=ober",
        "RuntimeDirectoryMode=0700",
        "RuntimeDirectoryPreserve=yes",
    ]
    run_dir = tmp_path / "run" / "ober"
    assert run_dir.is_dir() and run_dir.stat().st_mode & 0o777 == 0o700
    assert ["systemctl", "daemon-reload"] in calls
    # watch / blocklist-sync units are regenerated next to the drop-in
    assert any(f.suffix == ".service" for f in (tmp_path / "systemd").iterdir())


def test_apply_does_not_touch_system_when_not_root(tmp_path: Path, cert_pem: Path) -> None:
    config = make_config(tmp_path, cert_pem)
    calls = _apply_configuration_for_test(config, root=False)
    assert calls == []
    assert not (tmp_path / "systemd").exists() and not (tmp_path / "run").exists()


def test_bootstrap_unit_preserves_runtime_dir() -> None:
    from ober.commands.bootstrap import HAPROXY_SERVICE

    assert "RuntimeDirectory=ober" in HAPROXY_SERVICE
    assert "RuntimeDirectoryPreserve=yes" in HAPROXY_SERVICE


def test_invalid_network_entries_are_skipped(
    tmp_path: Path, cert_pem: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(
        tmp_path,
        cert_pem,
        allowlist=["192.0.2.0/24", "192.0.2.0/33", "not-an-ip", " 198.51.100.7 ", "2001:db8::/32"],
        trusted_networks=["10.0.0.0/8", "10.0.0.0/8x"],
        internal_networks=["172.16.0.0/12", "300.1.1.1", "10.1.2.3/8"],  # host bits are fine
    )
    _write_security_lists(config)
    assert config.allowlist_path.read_text() == "192.0.2.0/24\n198.51.100.7\n2001:db8::/32\n"
    assert config.trusted_path.read_text() == "10.0.0.0/8\n"
    internal = config.install_path / "etc" / "haproxy" / "internal.lst"
    assert internal.read_text() == "172.16.0.0/12\n10.1.2.3/8\n"
    out = capsys.readouterr().out
    for bad in ("192.0.2.0/33", "not-an-ip", "10.0.0.0/8x", "300.1.1.1"):
        assert bad in out
    if HAPROXY:
        generate(config, config.install_path / "h.sock")
        haproxy_check(config)


def test_internal_networks_restrict_internal_frontend(
    tmp_path: Path, cert_pem: Path, short_sock: Path
) -> None:
    config = make_config(tmp_path, cert_pem, "mixed", internal_networks=["10.0.0.0/8"])
    text = generate(config, short_sock)
    internal = section(text, "s3_internal")
    lst = config.install_path / "etc" / "haproxy" / "internal.lst"
    assert (
        f"tcp-request connection reject unless {{ src -f {lst} }} or "
        f"{{ src -f {config.trusted_path} }}"
    ) in internal
    assert internal.index("blocklist.lst") < internal.index("internal.lst")
    assert "internal.lst" not in section(text, "s3_internet")
    assert "del-header" not in internal  # internal VIP keeps append behaviour
    # empty = no restriction
    config.security.internal_networks = []
    assert "internal.lst" not in section(generate(config, short_sock), "s3_internal")


def test_stats_frontend_rejects_internet_vip_destinations(
    tmp_path: Path, cert_pem: Path, short_sock: Path
) -> None:
    config = make_config(tmp_path, cert_pem, "mixed")
    text = generate(config, short_sock)
    assert "tcp-request connection reject if { dst 10.0.100.2 }" in section(text, "stats")
    config.vips = [v for v in config.vips if v.exposure != "internet"]
    assert "dst" not in section(generate(config, short_sock), "stats")


def test_bucket_only_for_known_hosts(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem, "internet", s3_hostnames=["s3.example.org"])
    text = section(generate(config, short_sock), "s3_internet")
    assert "if { var(sess.o_host) -m str 10.0.100.1 s3.example.org }" in text
    assert "path,field(2,/),bytes(0,128),regsub([^[:print:]],?,g) if" in text  # always guarded
    config.security.s3_hostnames = []
    text = section(generate(config, short_sock), "s3_internet")
    assert "-m str 10.0.100.1 }" in text


def test_no_backend_no_frontends(tmp_path: Path, cert_pem: Path, short_sock: Path) -> None:
    config = make_config(tmp_path, cert_pem)
    config.backends = []
    text = generate(config, short_sock)
    assert "frontend s3_" not in text


# -- haproxy -c ----------------------------------------------------------------------------


@needs_haproxy
@pytest.mark.parametrize("shape", ["internal", "internet", "mixed"])
def test_haproxy_accepts_default_shapes(
    tmp_path: Path, cert_pem: Path, short_sock: Path, shape: str
) -> None:
    config = make_config(tmp_path, cert_pem, shape, s3_hostnames=["s3.example.org"])
    generate(config, short_sock)
    haproxy_check(config)


@needs_haproxy
def test_haproxy_accepts_every_flag_combination(
    tmp_path: Path, cert_pem: Path, short_sock: Path
) -> None:
    flags = ("allowlist_enabled", "autoban_enabled", "deny_admin_api", "deny_anonymous")
    checked = 0
    for shape in ("internal", "internet", "mixed"):
        for values in itertools.product([False, True], repeat=len(flags)):
            for limit, hosts, target in (
                (0, [], ""),
                (5, ["s3.example.org", "eu.s3.example.org"], "127.0.0.1:5514"),
            ):
                config = make_config(
                    tmp_path / f"c{checked}",
                    cert_pem,
                    shape,
                    s3_hostnames=hosts,
                    conn_limit_per_ip=limit,
                    internal_networks=["10.0.0.0/8"] if limit else [],
                    log_target=target,
                    allowlist=["192.0.2.0/24"],
                    **dict(zip(flags, values, strict=True)),
                )
                generate(config, short_sock)
                haproxy_check(config)
                checked += 1
    assert checked == 3 * 16 * 2


@needs_haproxy
def test_haproxy_accepts_empty_allowlist_and_ipv6(
    tmp_path: Path, cert_pem: Path, short_sock: Path
) -> None:
    config = make_config(tmp_path, cert_pem, "internet", allowlist=[], trusted_networks=["::1/128"])
    generate(config, short_sock)
    haproxy_check(config)


# -- wizard ------------------------------------------------------------------------------------


def test_wizard_marks_internet_vips() -> None:
    from unittest.mock import patch

    from ober.commands.config import _configure_security

    vips = [VIPConfig(address="10.0.100.1/32"), VIPConfig(address="10.0.100.2/32")]
    answers = [
        {"internet": ["10.0.100.2/32"]},
        {
            "hostnames": "s3.example.org, ",
            "allowlist": "192.0.2.0/24, bogus 198.51.100.0/24",
            "trusted": "10.0.0.0/8",
            "log_target": " watch01:5514 ",
        },
    ]
    with patch("ober.commands.config.inquirer.prompt", side_effect=answers):
        out_vips, sec = _configure_security(SecurityConfig(), vips)
    assert [v.exposure for v in out_vips] == ["internal", "internet"]
    assert sec.s3_hostnames == ["s3.example.org"]
    assert sec.allowlist == ["192.0.2.0/24", "198.51.100.0/24"]  # invalid entry dropped
    assert sec.trusted_networks == ["10.0.0.0/8"]
    assert sec.log_target == "watch01:5514"


def test_wizard_all_internal_asks_nothing_else() -> None:
    from unittest.mock import patch

    from ober.commands.config import _configure_security

    vips = [VIPConfig(address="10.0.100.1/32", exposure="internet")]
    with patch("ober.commands.config.inquirer.prompt", side_effect=[{"internet": []}]) as prompt:
        out_vips, _ = _configure_security(SecurityConfig(), vips)
    assert out_vips[0].exposure == "internal"
    assert prompt.call_count == 1


def test_summary_warns_on_empty_allowlist(tmp_path: Path, cert_pem: Path) -> None:
    from ober.commands import config as cmd

    config = make_config(tmp_path, cert_pem, "internet")
    with cmd.console.capture() as capture:
        cmd._print_config_summary(config)
    out = capture.get()
    assert "(internet)" in out and "denies all Internet traffic" in out
    config.security.allowlist = ["192.0.2.0/24"]
    with cmd.console.capture() as capture:
        cmd._print_config_summary(config)
    assert "denies all" not in capture.get()


def test_reconfigure_keeps_vip_exposure() -> None:
    from unittest.mock import patch

    from ober.commands.config import _configure_vips

    current = [VIPConfig(address="10.0.100.1/32", exposure="internet")]
    with patch("ober.commands.config.inquirer.prompt", return_value={"vips": "10.0.100.1"}):
        vips = _configure_vips(current)
    assert vips[0].exposure == "internet"
