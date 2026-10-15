#!/usr/bin/env python3
"""Shared data types for ober watch.

These are the contracts between the HAProxy log format, the analysis
pipeline (rules, summaries, blocklist) and the Microsoft integrations.

HAProxy log contract: each request is one syslog message whose payload is a
single JSON object with exactly the keys in ``LOG_FIELDS``. The syslog header
(RFC 3164 or RFC 5424) precedes the JSON; parsers locate the first ``{``.
Redaction happens in HAProxy, before the line leaves the proxy node: no SigV4
signature, no query string values and no object keys are ever logged.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

# Keys of the JSON object HAProxy emits per request (see design doc, "Logging").
LOG_FIELDS: tuple[str, ...] = (
    "ts",  # request start, epoch seconds (float or int)
    "node",  # proxy hostname
    "frontend",  # "s3_internal" or "s3_internet"
    "client_ip",  # real client IP (connection source)
    "method",  # HTTP method
    "host",  # Host header without port
    "bucket",  # bucket name ("" for service-level requests such as ListBuckets)
    "access_key",  # SigV4 access key ID from Authorization or X-Amz-Credential, "" if unsigned
    "presigned",  # true if the query carried X-Amz-Signature (value never logged)
    "status",  # HTTP status returned to the client
    "bytes_in",  # request bytes received from the client
    "bytes_out",  # response bytes sent to the client
    "duration_ms",  # total request time
    "user_agent",  # truncated to 256 bytes; attacker-controlled
    "tls_version",  # e.g. "TLSv1.3", "" for plain HTTP
)

SEVERITIES: tuple[str, ...] = ("low", "medium", "high")

# KeySummary.access_key of the per-interval heartbeat row ober watch always sends,
# so Sentinel can tell "no traffic" from "ober watch or ingestion is down".
HEARTBEAT_KEY = "(heartbeat)"


def severity_rank(severity: str) -> int:
    """Return the order of a severity (low=0 < medium=1 < high=2), -1 if unknown."""
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


@dataclass(frozen=True)
class LogRecord:
    """One parsed HAProxy request log line."""

    ts: float
    node: str
    frontend: str
    client_ip: str
    method: str
    host: str
    bucket: str
    access_key: str
    presigned: bool
    status: int
    bytes_in: int
    bytes_out: int
    duration_ms: int
    user_agent: str
    tls_version: str

    @property
    def is_internet(self) -> bool:
        """True if the request arrived on an internet-exposed VIP."""
        return self.frontend == "s3_internet"

    @property
    def is_auth_failure(self) -> bool:
        """True for responses that indicate failed authentication/authorization."""
        return self.status in (401, 403)


@dataclass
class Finding:
    """A detection result from an L1 rule (or, later, L2/L3)."""

    id: str  # stable unique id, e.g. uuid4 hex
    ts: float  # epoch seconds when the finding was raised
    rule: str  # e.g. "auth_failure_burst"
    severity: str  # one of SEVERITIES
    client_ip: str = ""
    access_key: str = ""
    bucket: str = ""
    node: str = ""
    count: int = 0  # events that triggered the finding within the window
    window_s: int = 0
    detail: str = ""  # short human-readable explanation (no attacker-controlled text)
    action: str = "none"  # "none" or "block"
    layer: str = "L1"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONL storage and Sentinel ingestion."""
        return asdict(self)


@dataclass
class KeySummary:
    """Per access key activity over one summary interval (sent to Sentinel)."""

    window_start: float
    window_s: int
    access_key: str  # "" aggregates unsigned requests
    requests: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    auth_failures: int = 0
    errors_5xx: int = 0
    distinct_ips: int = 0
    distinct_buckets: int = 0
    internet_requests: int = 0
    nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONL storage and Sentinel ingestion."""
        return asdict(self)


@dataclass
class BlockEntry:
    """One blocked IP or CIDR with its expiry."""

    target: str  # IP or CIDR
    reason: str
    expires_at: float  # epoch seconds; 0 = never expires (manual blocks only)
    source: str = "auto"  # "auto" or "manual"
    finding_id: str = ""
    offenses: int = 1  # repeat count, drives TTL doubling

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence and the blocklist API."""
        return asdict(self)
