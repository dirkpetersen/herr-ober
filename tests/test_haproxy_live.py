"""Run a real HAProxy with the generated config and check the data path end to end.

Skipped when haproxy/openssl are missing. Everything binds to 127.0.0.0/8 high ports; the
client picks different loopback *source* addresses so ban/blocklist scenarios cannot affect
each other.
"""

import http.client
import http.server
import json
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ober.commands.config import _generate_haproxy_config, _write_security_lists
from ober.config import BackendConfig, CertConfig, OberConfig, SecurityConfig, VIPConfig
from ober.watch.models import LOG_FIELDS

HAPROXY = shutil.which("haproxy") or (
    "/usr/sbin/haproxy" if Path("/usr/sbin/haproxy").exists() else ""
)
pytestmark = pytest.mark.skipif(
    not HAPROXY or not shutil.which("openssl"), reason="haproxy/openssl not installed"
)

INTERNET_IP = "127.0.0.1"
INTERNAL_IP = "127.0.0.2"
HOST = "s3.example.org"

# Secrets that must never appear in any syslog datagram.
SIGNATURE = "5d1f0c0ffee0badc0de5d1f0c0ffee0badc0de5d1f0c0ffee0badc0de1234abcd"
SIGV2_SIG = "Zm9vYmFyU2lnbmF0dXJlMTIzNDU2Nzg5MA="
OBJECT_KEY = "secret-object-key-9137.dat"
QUERY_SECRET = "SECRETUPLOADID77"
SECRETS = (SIGNATURE, SIGV2_SIG, OBJECT_KEY, QUERY_SECRET, "20240101T000000Z")

SIGV4 = (
    "AWS4-HMAC-SHA256 Credential=AKIDSIGV4EXAMPLE/20240101/us-east-1/s3/aws4_request, "
    f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={SIGNATURE}"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class Backend(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if "X-Forwarded-Proto" in self.headers:  # skip haproxy health checks
                    outer.seen.append(
                        {
                            "path": self.path,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                        }
                    )
                status = 403 if self.path.startswith("/forbidden") else 200
                body = b"B" * 3000 if self.path.startswith("/big") else b"ok"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = do_OPTIONS = _serve

            def log_message(self, *args: Any) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)


class SyslogSink:
    """UDP listener collecting raw datagrams."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port: int = self.sock.getsockname()[1]
        self.payloads: list[bytes] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            self.payloads.append(data)

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self.sock.close()

    def records(self, client_ip: str) -> list[dict[str, Any]]:
        out = []
        for raw in list(self.payloads):
            text = raw.decode("utf-8", "replace")
            if "{" not in text:  # haproxy process events (start, server UP/DOWN)
                continue
            record = json.loads(text[text.index("{") :])
            if record["client_ip"] == client_ip:
                out.append(record)
        return out

    def wait_record(
        self, client_ip: str, count: int = 1, timeout: float = 5.0
    ) -> list[dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            records = self.records(client_ip)
            if len(records) >= count:
                return records
            time.sleep(0.05)
        raise AssertionError(
            f"expected {count} log lines for {client_ip}, got {self.records(client_ip)}"
        )


class Proxy:
    def __init__(self, config: OberConfig, port: int, sock: Path, sink: SyslogSink) -> None:
        self.config = config
        self.port = port
        self.sock = sock
        self.sink = sink
        self.backend: Backend

    def request(
        self,
        method: str = "GET",
        path: str = "/",
        headers: dict[str, str] | None = None,
        src: str = "127.0.0.50",
        host: str = HOST,
        dest: str = INTERNET_IP,
        body: bytes | None = None,
        conn: http.client.HTTPSConnection | None = None,
    ) -> tuple[int, http.client.HTTPSConnection]:
        if conn is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                dest, self.port, context=ctx, timeout=5, source_address=(src, 0)
            )
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        resp.read()
        return resp.status, conn

    def status(self, *args: Any, **kwargs: Any) -> int:
        status, conn = self.request(*args, **kwargs)
        conn.close()
        return status

    def admin(self, command: str) -> str:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.sock))
            s.sendall(command.encode() + b"\n")
            chunks = []
            while chunk := s.recv(4096):
                chunks.append(chunk)
        return b"".join(chunks).decode()


def can_connect(proxy: Proxy, src: str, dest: str = INTERNET_IP) -> bool:
    """True if a full TLS+HTTP exchange works from this source address."""
    try:
        proxy.status("GET", "/probe", {"Authorization": SIGV4}, src=src, dest=dest)
        return True
    except (OSError, http.client.HTTPException):
        return False


@pytest.fixture(scope="module")
def proxy() -> Iterator[Proxy]:
    root = Path(tempfile.mkdtemp(prefix="obl"))
    backend = Backend()
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    sink = SyslogSink()
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                "-nodes", "-keyout", str(root / "k.pem"), "-out", str(root / "c.pem"),
                "-days", "2", "-subj", "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        pem = root / "s.pem"
        pem.write_text((root / "c.pem").read_text() + (root / "k.pem").read_text())

        config = OberConfig(install_path=root / "inst")
        config.ensure_directories()
        config.vips = [
            VIPConfig(address=f"{INTERNET_IP}/32", exposure="internet"),
            VIPConfig(address=f"{INTERNAL_IP}/32"),
        ]
        config.backends = [
            BackendConfig(name="s3_backend", servers=[f"127.0.0.1:{backend.server_port}"])
        ]
        config.certs = CertConfig(path=str(pem))
        config.stats_port = free_port()
        config.security = SecurityConfig(
            s3_hostnames=[HOST],
            allowlist=["127.0.0.0/24"],  # 127.0.1.x is outside: default deny
            trusted_networks=["127.0.2.0/24"],  # outside the allowlist, never banned
            deny_anonymous=True,
            autoban_auth_failures=3,
            autoban_window_s=2,
            autoban_ban_s=60,
            log_target=f"127.0.0.1:{sink.port}",
        )
        port = free_port()
        sock = root / "h.sock"
        _write_security_lists(config)
        _generate_haproxy_config(config, socket_path=sock, https_port=port)

        # Probe with a trusted source so the main scenarios start clean
        proc = subprocess.Popen(
            [HAPROXY, "-db", "-q", "-f", str(config.haproxy_config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        p = Proxy(config, port, sock, sink)
        p.backend = backend
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if proc.poll() is not None:
                    out = proc.stdout.read().decode() if proc.stdout else ""
                    raise RuntimeError(f"haproxy exited: {out}")
                try:
                    if (
                        p.status("GET", "/ready", {"Authorization": SIGV4}, src="127.0.2.250")
                        == 200
                    ):
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("haproxy backend never became healthy")
            yield p
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
    finally:
        backend.shutdown()
        backend.server_close()
        sink.close()
        shutil.rmtree(root, ignore_errors=True)


def check_record(record: dict[str, Any]) -> None:
    assert tuple(record) == LOG_FIELDS
    for key in ("node", "frontend", "client_ip", "method", "host", "bucket"):
        assert isinstance(record[key], str), key
    for key in ("access_key", "user_agent", "tls_version"):
        assert isinstance(record[key], str), key
    assert isinstance(record["presigned"], bool)
    assert isinstance(record["ts"], int | float) and record["ts"] > 1_700_000_000
    for key in ("status", "bytes_in", "bytes_out", "duration_ms"):
        assert isinstance(record[key], int) and not isinstance(record[key], bool), key


def one_log(proxy: Proxy, src: str, **kwargs: Any) -> dict[str, Any]:
    status, conn = proxy.request(src=src, **kwargs)
    conn.close()
    record = proxy.sink.wait_record(src)[-1]
    check_record(record)
    record["_status"] = status
    return record


# -- log contract ----------------------------------------------------------------------------


def test_sigv4_header(proxy: Proxy) -> None:
    rec = one_log(
        proxy,
        "127.0.0.11",
        method="PUT",
        path=f"/mybucket/{OBJECT_KEY}?uploadId={QUERY_SECRET}&partNumber=1",
        headers={
            "Authorization": SIGV4,
            "X-Amz-Date": "20240101T000000Z",
            "User-Agent": "aws-sdk/1.0",
        },
        body=b"x" * 1000,
    )
    assert rec["_status"] == 200
    assert rec["frontend"] == "s3_internet"
    assert rec["access_key"] == "AKIDSIGV4EXAMPLE"
    assert rec["presigned"] is False
    assert rec["bucket"] == "mybucket"
    assert rec["method"] == "PUT" and rec["host"] == HOST
    assert rec["status"] == 200 and rec["bytes_in"] >= 1000 and 0 < rec["bytes_out"] < 500
    assert rec["user_agent"] == "aws-sdk/1.0"
    assert rec["tls_version"] == "TLSv1.3"
    assert rec["client_ip"] == "127.0.0.11"
    assert rec["node"] == socket.gethostname()


def test_signed_request_reaches_backend_unmodified(proxy: Proxy) -> None:
    proxy.backend.seen.clear()
    path = f"/b/{OBJECT_KEY}?x=1&y=%2Fz"
    headers = {"Authorization": SIGV4, "X-Amz-Date": "20240101T000000Z"}
    assert proxy.status("GET", path, headers, src="127.0.0.12") == 200
    seen = proxy.backend.seen[-1]
    assert seen["path"] == path
    assert seen["headers"]["host"] == HOST
    assert seen["headers"]["authorization"] == SIGV4
    assert seen["headers"]["x-amz-date"] == "20240101T000000Z"
    assert seen["headers"]["x-forwarded-proto"] == "https"
    assert seen["headers"]["x-forwarded-for"] == "127.0.0.12"


def test_sigv2_header(proxy: Proxy) -> None:
    rec = one_log(
        proxy,
        "127.0.0.13",
        path=f"/legacy/{OBJECT_KEY}",
        headers={"Authorization": f"AWS AKIDV2EXAMPLE:{SIGV2_SIG}"},
    )
    assert rec["access_key"] == "AKIDV2EXAMPLE"
    assert rec["bucket"] == "legacy"
    assert rec["presigned"] is False


def test_presigned_url(proxy: Proxy) -> None:
    query = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIDPRESIGN%2F20240101%2Fus-east-1"
        f"%2Fs3%2Faws4_request&X-Amz-Date=20240101T000000Z&X-Amz-Signature={SIGNATURE}"
    )
    rec = one_log(proxy, "127.0.0.14", path=f"/pre/{OBJECT_KEY}?{query}")
    assert rec["_status"] == 200  # a signature in the query is not anonymous
    assert rec["access_key"] == "AKIDPRESIGN"
    assert rec["presigned"] is True
    assert rec["bucket"] == "pre"


def test_presigned_v2_url(proxy: Proxy) -> None:
    rec = one_log(
        proxy, "127.0.0.15", path=f"/pre2/{OBJECT_KEY}?AWSAccessKeyId=AK&Signature={SIGV2_SIG}"
    )
    assert rec["presigned"] is True and rec["_status"] == 200


def test_vhost_style_bucket(proxy: Proxy) -> None:
    rec = one_log(
        proxy,
        "127.0.0.16",
        path=f"/{OBJECT_KEY}",
        host=f"my.vhost-bucket.{HOST}:8443",
        headers={"Authorization": SIGV4},
    )
    assert rec["bucket"] == "my.vhost-bucket"
    assert rec["host"] == f"my.vhost-bucket.{HOST}"  # port stripped
    assert rec["_status"] == 200


def test_service_level_request_has_empty_bucket(proxy: Proxy) -> None:
    rec = one_log(proxy, "127.0.0.17", path="/", headers={"Authorization": SIGV4})
    assert rec["bucket"] == ""


def test_unsigned_request_denied(proxy: Proxy) -> None:
    rec = one_log(proxy, "127.0.0.18", path=f"/pub/{OBJECT_KEY}")
    assert rec["_status"] == 403 and rec["status"] == 403
    assert rec["access_key"] == "" and rec["presigned"] is False
    assert rec["bucket"] == "pub"


def test_unsigned_allowed_on_internal_frontend(proxy: Proxy) -> None:
    rec = one_log(proxy, "127.0.0.19", dest=INTERNAL_IP, host="whatever.internal", path="/pub/k")
    assert rec["_status"] == 200
    assert rec["frontend"] == "s3_internal"
    assert rec["bucket"] == "pub"  # host is not a configured s3 hostname -> path style


def test_internal_frontend_vhost_and_keys(proxy: Proxy) -> None:
    rec = one_log(
        proxy,
        "127.0.0.20",
        dest=INTERNAL_IP,
        host=f"bkt.{HOST}",
        path=f"/{OBJECT_KEY}",
        headers={"Authorization": SIGV4},
    )
    assert rec["frontend"] == "s3_internal"
    assert rec["bucket"] == "bkt" and rec["access_key"] == "AKIDSIGV4EXAMPLE"


def test_admin_api_denied_on_internet_only(proxy: Proxy) -> None:
    proxy.backend.seen.clear()
    rec = one_log(proxy, "127.0.0.21", path="/admin/user?uid=bob", headers={"Authorization": SIGV4})
    assert rec["_status"] == 403 and rec["status"] == 403
    assert rec["bucket"] == "admin"
    assert not proxy.backend.seen  # never reached RGW
    # percent-encoded spelling is caught too
    assert proxy.status("GET", "/%61dmin/usage", {"Authorization": SIGV4}, src="127.0.0.21") == 403
    # a bucket merely starting with "admin" is fine
    assert (
        proxy.status("GET", "/administrators/k", {"Authorization": SIGV4}, src="127.0.0.21") == 200
    )
    # internal VIP is not restricted
    assert (
        proxy.status(
            "GET", "/admin/user", {"Authorization": SIGV4}, src="127.0.0.21", dest=INTERNAL_IP
        )
        == 200
    )


def test_unknown_host_and_method_denied(proxy: Proxy) -> None:
    auth = {"Authorization": SIGV4}
    assert proxy.status("GET", "/b/k", auth, src="127.0.0.22", host="evil.example.com") == 403
    assert proxy.status("GET", "/b/k", auth, src="127.0.0.22", host="evilas3.example.org") == 403
    assert proxy.status("PATCH", "/b/k", auth, src="127.0.0.22") == 403
    assert proxy.status("OPTIONS", "/b/k", src="127.0.0.22") == 200  # CORS preflight is unsigned


def test_hostile_user_agent_is_valid_json_and_truncated(proxy: Proxy) -> None:
    nasty = 'evil"\\agent,\t{"a":1}'
    rec = one_log(proxy, "127.0.0.23", headers={"Authorization": SIGV4, "User-Agent": nasty})
    # quotes, backslashes and commas survive; control characters become "?"
    assert rec["user_agent"] == nasty.replace("\t", "?")
    rec = one_log(proxy, "127.0.0.24", headers={"Authorization": SIGV4, "User-Agent": "A" * 1000})
    assert rec["user_agent"] == "A" * 256


def test_non_utf8_user_agent_keeps_line_valid(proxy: Proxy) -> None:
    status, conn = proxy.request(
        src="127.0.0.25", headers={"Authorization": SIGV4, "User-Agent": "caf\u00e9-client"}
    )
    conn.close()
    rec = proxy.sink.wait_record("127.0.0.25")[-1]
    check_record(rec)
    assert rec["user_agent"] == "caf?-client"  # non-ASCII bytes can't break the JSON line


def test_keepalive_does_not_leak_fields_between_requests(proxy: Proxy) -> None:
    src = "127.0.0.26"
    status, conn = proxy.request(
        path="/first/k", headers={"Authorization": SIGV4, "User-Agent": "ua1"}, src=src
    )
    assert status == 200
    status, conn = proxy.request(path="/second/k?X-Amz-Signature=abc", src=src, conn=conn)
    conn.close()
    first, second = proxy.sink.wait_record(src, 2)
    assert first["access_key"] == "AKIDSIGV4EXAMPLE" and first["bucket"] == "first"
    assert second["access_key"] == "" and second["bucket"] == "second"
    assert (
        second["user_agent"] == "" and second["presigned"] is True and first["presigned"] is False
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_tls_policy(proxy: Proxy) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    with (
        pytest.raises((ssl.SSLError, OSError)),
        socket.create_connection(
            (INTERNET_IP, proxy.port), source_address=("127.0.0.27", 0)
        ) as raw,
    ):
        ctx.wrap_socket(raw)
    ctx12 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx12.check_hostname = False
    ctx12.verify_mode = ssl.CERT_NONE
    ctx12.maximum_version = ssl.TLSVersion.TLSv1_3
    with (
        socket.create_connection(
            (INTERNET_IP, proxy.port), source_address=("127.0.0.27", 0)
        ) as raw,
        ctx12.wrap_socket(raw) as tls,
    ):
        cipher = tls.cipher()
        assert cipher is not None and cipher[0] == "TLS_AES_128_GCM_SHA256"
        assert tls.selected_alpn_protocol() in (None, "http/1.1")


# -- connection-level controls --------------------------------------------------------------------


def test_allowlist_default_deny_and_trusted_exempt(proxy: Proxy) -> None:
    assert can_connect(proxy, "127.0.0.30")  # in allowlist 127.0.0.0/24
    assert not can_connect(proxy, "127.0.1.30")  # outside: reset before TLS
    assert can_connect(proxy, "127.0.2.30")  # trusted
    # internal VIP has no allowlist
    assert can_connect(proxy, "127.0.1.30", dest=INTERNAL_IP)


def test_blocklist_runtime_update(proxy: Proxy) -> None:
    src = "127.0.0.31"
    assert can_connect(proxy, src) and can_connect(proxy, src, INTERNAL_IP)
    out = proxy.admin(f"add acl {proxy.config.blocklist_path} {src}")
    assert "Done" in out or out.strip() == "", out
    assert not can_connect(proxy, src)
    assert not can_connect(proxy, src, INTERNAL_IP)  # blocklist covers both frontends
    assert can_connect(proxy, "127.0.0.32")  # others unaffected
    proxy.admin(f"del acl {proxy.config.blocklist_path} {src}")
    assert can_connect(proxy, src)


def test_blocklist_applies_to_trusted(proxy: Proxy) -> None:
    proxy.admin(f"add acl {proxy.config.blocklist_path} 127.0.2.33")
    try:
        assert not can_connect(proxy, "127.0.2.33")
    finally:
        proxy.admin(f"del acl {proxy.config.blocklist_path} 127.0.2.33")


def test_autoban_after_backend_403s(proxy: Proxy) -> None:
    src = "127.0.0.40"
    auth = {"Authorization": SIGV4}
    # below the threshold (3 per 2s): no ban
    for _ in range(2):
        assert proxy.status("GET", "/forbidden/x", auth, src=src) == 403
    assert can_connect(proxy, src)
    time.sleep(2.5)  # rate decays

    # keep-alive connection: the attacker keeps hammering over one connection
    conn = None
    statuses = []
    cut = False
    for _ in range(12):
        try:
            status, conn = proxy.request("GET", "/forbidden/x", auth, src=src, conn=conn)
            statuses.append(status)
        except (OSError, http.client.HTTPException):
            cut = True
            break
    assert cut, f"keep-alive connection was never cut: {statuses}"
    assert 3 <= len(statuses) <= 6

    # new connections are rejected before TLS
    assert not can_connect(proxy, src)
    # the ban persists after the failure rate has decayed (window 2s)
    time.sleep(3)
    assert not can_connect(proxy, src)
    assert can_connect(proxy, src, INTERNAL_IP)
    # other clients are unaffected
    assert can_connect(proxy, "127.0.0.41")
    # the ban is an expiry timestamp in gpt0, kept after the rate decayed
    table = proxy.admin("show table s3_internet")
    line = next(ln for ln in table.splitlines() if f"key={src} " in ln)
    expiry = int(re.search(r"gpt0=(\d+)", line).group(1))  # type: ignore[union-attr]
    assert time.time() < expiry <= time.time() + proxy.config.security.autoban_ban_s


def test_policy_denies_count_towards_ban(proxy: Proxy) -> None:
    src = "127.0.0.42"
    for _ in range(6):
        try:
            proxy.status("GET", "/admin/user", {"Authorization": SIGV4}, src=src)
        except (OSError, http.client.HTTPException):
            break
    assert not can_connect(proxy, src)


def test_trusted_never_banned(proxy: Proxy) -> None:
    src = "127.0.2.43"
    for _ in range(10):
        assert proxy.status("GET", "/forbidden/x", {"Authorization": SIGV4}, src=src) == 403
    assert can_connect(proxy, src)


def test_successes_do_not_ban(proxy: Proxy) -> None:
    src = "127.0.0.44"
    for _ in range(30):
        assert proxy.status("GET", "/b/k", {"Authorization": SIGV4}, src=src) == 200
    assert can_connect(proxy, src)


# -- redaction (runs last: inspects every datagram received) --------------------------------


def test_zz_every_datagram_is_clean(proxy: Proxy) -> None:
    time.sleep(0.5)
    assert len(proxy.sink.payloads) > 20
    from ober.watch.parser import parse_datagram

    parsed = 0
    for raw in proxy.sink.payloads:
        text = raw.decode("utf-8")  # every datagram must be valid UTF-8
        for secret in SECRETS:
            assert secret not in text, f"{secret!r} leaked in {text}"
        assert "uploadId" not in text and "X-Amz" not in text
        assert "Signature" not in text and "Credential" not in text
        if "{" not in text:
            continue  # process event, not a request line
        record = json.loads(text[text.index("{") :])
        check_record(record)
        assert "/" not in record["bucket"]
        assert OBJECT_KEY not in json.dumps(record)
        # the consumer's parser accepts what HAProxy really emits
        # (aborted requests, e.g. a banned keep-alive connection, log status -1)
        # aborted requests log status -1; the parser keeps them as status 0
        assert parse_datagram(raw) is not None
        parsed += 1
    assert parsed > 20
