# Security monitoring

Herr Ober can run on internet-exposed S3 endpoints. Protection is split in two so the data
path stays fast:

* **Data path (proxy nodes).** HAProxy does only per-request work: allowlist, early rejects,
  per-IP counters and auto-ban, plus the blocklist file. It writes one JSON log line per
  request over UDP syslog. Nothing here waits on analysis or on the network.
* **Analysis path (one analysis host).** `ober watch` receives those logs, runs rules,
  keeps the blocklist, and ships findings to Microsoft Sentinel (and optionally Defender for
  Endpoint) from a worker thread behind a bounded queue.

```
clients --> HAProxy (proxy nodes) --UDP syslog JSON--> ober watch (analysis host)
               ^                                          |-- raw logs: local disk only
               |-- ober blocklist sync (pull) <-----------|-- blocklist API (:8405)
                                                          |-- findings + key summaries --> Sentinel
                                                          '-- confirmed block findings --> MDE (opt.)
```

Raw logs never leave the analysis host. Only findings and per-access-key summaries go to
Sentinel, which keeps ingestion cost small.

## 1. Expose a VIP to the internet

Exposure is set per VIP; `internal` (default) keeps the unchanged behavior.

```yaml
vips:
  - address: 203.0.113.10
    interface: lo-vip
    exposure: internet        # "internal" or "internet"

security:
  s3_hostnames: [s3.example.org]
  allowlist_enabled: true     # internet VIPs are default-deny
  allowlist: [198.51.100.0/24]
  trusted_networks: [10.0.0.0/8]   # exempt from all automatic blocking and limits
  internal_networks: [10.0.0.0/8]  # sources allowed on internal VIPs; [] = no restriction
  deny_admin_api: true        # block RGW /admin
  deny_anonymous: false       # true = block unsigned requests
  autoban_enabled: true
  autoban_auth_failures: 50   # signed 401/403 (not HEAD/OPTIONS) per window before a ban
  autoban_unsigned_failures: 500  # separate, higher threshold for unsigned failures
  autoban_window_s: 60
  autoban_ban_s: 3600
  conn_limit_per_ip: 2000     # concurrent connections per IP, 0 = off
  internet_maxconn: 20000     # cap so Internet clients cannot starve internal VIPs
  timeout_client_s: 300
  timeout_server_s: 600
  log_target: analysis01:5514 # UDP syslog of ober watch; "" = stdout

watch:
  syslog_listen: 0.0.0.0:5514
  api_listen: 0.0.0.0:8405     # bind a management address in production
  syslog_allowed_sources: [10.0.0.21, 10.0.0.22]   # the proxy nodes
  url: http://analysis01:8405  # proxy nodes: where to pull the blocklist
  data_dir: ""                 # "" = <install_path>/var/watch
  sync_interval_s: 30
  summary_interval_s: 300
  raw_log_retention_days: 30
  block_ttl_s: 3600            # first auto-block; doubles for repeat offenders
  block_ttl_max_s: 604800
```

### Semantics

* **Allowlist vs. trusted networks.** The allowlist only grants permission to connect. In
  phase 1 every Internet client is allowlisted, so it does not restrict anyone yet.
  `trusted_networks` are exempt from all automatic blocking and limits (auto-ban,
  connection limits, blocklist). If a shared campus NAT address must never be banned, put
  its range in both `allowlist` and `trusted_networks`.
* **Auto-ban.** Counts only signed 401/403 responses that are not HEAD or OPTIONS. Unsigned
  failures are counted separately against the higher `autoban_unsigned_failures` (default
  500), because many users can share one NAT address.
* **What can be blocked automatically.** Automatic blocks (auto-ban and `ober watch`
  blocklist entries) come only from traffic on Internet VIPs, only for single IP
  addresses, and never for trusted or internal ranges.
* **Internal VIPs.** `internal_networks` restricts who may reach internal VIPs; an empty
  list means no restriction.
* **Client IP on RGW.** HAProxy strips any client-sent `X-Forwarded-For` on Internet VIPs
  and sets its own. Set `rgw_remote_addr_param = http_x_forwarded_for` on RGW so it sees
  the real client address.
* **Stats port.** The HAProxy stats port 8404 is not reachable via Internet VIPs.

### Network exposure of `ober watch`

`watch.syslog_allowed_sources` must list the proxy nodes; with an empty list any sender is
accepted (a warning is logged at startup). UDP syslog is unauthenticated and can be
spoofed, so bind `syslog_listen` and `api_listen` to a management network address and
firewall 5514/udp and 8405/tcp so that only the proxy nodes can reach them.

## 2. Run `ober watch` on the analysis host

```bash
ober watch run        # foreground; run under systemd in production
ober watch status     # counters of the running daemon
```

A systemd unit template lives in `ober/watch/units.py`. The daemon needs `watch` settings
(above) and, if you want integrations, the `sentinel:` / `mde:` sections and secrets (below).
Keep it on a host that is not on the data path, or at least at low priority (the template
uses `Nice=10` and batch scheduling).

## 3. Blocklist sync on proxy nodes

Proxy nodes pull the blocklist from the analysis host and apply it to the local HAProxy
(no per-request network call; HAProxy reads a local file).

```bash
ober blocklist sync               # pull and apply (run from a timer, every sync_interval_s)
ober blocklist list
ober blocklist add 203.0.113.99 --reason "manual" --ttl 3600
ober blocklist remove 203.0.113.99
```

Reading the blocklist needs `OBER_WATCH_TOKEN`; `add` and `remove` write and need
`OBER_WATCH_ADMIN_TOKEN` (see Secrets). If the analysis host is down the sync does not
fail the timer; HAProxy keeps the last applied list and its own local auto-ban.

## 4. Microsoft Sentinel

Create tables, DCR, app registration and analytics rules with the exact commands in
[deploy/sentinel/README.md](../deploy/sentinel/README.md), then enable it:

```yaml
sentinel:
  enabled: true
  tenant_id: <tenant>
  client_id: <app id>
  dce_endpoint: https://<name>.<region>.ingest.monitor.azure.com
  dcr_immutable_id: dcr-...
  findings_stream: Custom-OberFindings_CL
  summary_stream: Custom-OberKeySummary_CL
```

Delivery behavior:

* Findings go to `OberFindings_CL`, per-key summaries (every `summary_interval_s`) to
  `OberKeySummary_CL`. Columns are the `Finding` / `KeySummary` field names plus
  `TimeGenerated`; `id` is sent as `finding_id` (reserved name in Azure Monitor).
* Bodies are gzip-compressed and split below 1 MB. HTTP 429 and 5xx are retried with
  backoff, honoring `Retry-After`.
* If delivery keeps failing, rows are spooled to
  `<watch data dir>/spool/sentinel/<stream>/*.jsonl` (capped at 1 GB; the oldest files are
  dropped first and counted) and re-sent after the next successful call.
* A sink never raises into the daemon; misconfigured sinks are skipped with a warning at
  startup (check the log for `Sentinel sink disabled`).

## 5. Microsoft Defender for Endpoint (optional)

Off by default. Findings with `action == "block"` and severity at or above `min_severity`
become IP indicators for the attacker's address.

```yaml
mde:
  enabled: true
  tenant_id: <tenant>
  client_id: <app id>
  api_base: https://api.securitycenter.microsoft.com
  min_severity: high     # low | medium | high
  action: Block          # Block or Audit
  expiry_days: 1         # keep close to the block TTL
  max_active_indicators: 1000   # unexpired indicators ober may hold; the rest are skipped
```

* Only globally routable single IP addresses are submitted. Private, loopback, reserved,
  and CGNAT addresses are skipped (NAT64 `64:ff9b::/96` and IPv4-compatible `::a.b.c.d`
  addresses are judged by their embedded IPv4 address), and CIDR ranges are not sent because the indicators API
  does not support them.
* Batches of up to 500 use `POST /api/indicators/import`, rate limited client-side below the
  documented 30 calls/minute (`Retry-After` is honored up to 300 s). Pending IPs are
  retried on every summary interval and on shutdown. An IP that was submitted and has not expired is not
  resubmitted (state in `<watch data dir>/mde_state.json`).
* Indicator description holds only the rule name, count and window; never attacker-controlled
  strings. MDE allows 15,000 active indicators per tenant, shared with your SOC, so ober stops adding
  at `max_active_indicators` and counts the skipped IPs.
* App permission `Ti.ReadWrite.All` with admin consent is required (see the deploy README).

## 6. Secrets

Secrets are read from `~/.ober/login` (`KEY=value` per line, mode 600) of the user running
the command, never from `ober.yaml`:

| Key | Used by |
|-----|---------|
| `SENTINEL_CLIENT_SECRET` | `ober watch`, Sentinel sink |
| `MDE_CLIENT_SECRET` | `ober watch`, MDE sink |
| `OBER_WATCH_TOKEN` | read access: `ober watch` (API server), `ober blocklist sync` / `list` on proxy nodes; the same value on every node |
| `OBER_WATCH_ADMIN_TOKEN` | write access: `ober blocklist add` / `remove`; keep it off the proxy nodes where possible |

## 7. What is logged, and what never is

Each request produces one JSON line with exactly these fields: `ts`, `node`, `frontend`,
`client_ip`, `method`, `host`, `bucket`, `access_key`, `presigned`, `status`, `bytes_in`,
`bytes_out`, `duration_ms`, `user_agent` (truncated to 256 bytes), `tls_version`.

Never logged, because HAProxy redacts before the line leaves the proxy node:

* SigV4 signatures (`presigned` is a boolean; the `X-Amz-Signature` value is not recorded)
* Query string values
* Object keys (only the bucket name is logged)
* Request or response bodies, and any other headers

The access key ID is logged (it identifies the caller, it is not a secret). `user_agent`
is attacker-controlled and is JSON-escaped; findings and MDE indicators never include it.
Raw logs stay on the analysis host for `raw_log_retention_days`.

## 8. Performance rules

* Every HAProxy control is per-request only: no per-byte work, no lookups over the network,
  no Lua on the hot path. The blocklist is a local file, updated by pull.
* The log line is built from variables already computed in HAProxy; syslog is UDP, so a slow
  or dead analysis host cannot back-pressure the proxy.
* In `ober watch`, the receive path only parses, evaluates rules and enqueues. Disk and
  network sinks run on worker threads behind bounded queues; when a queue is full, work is
  dropped and counted rather than blocking.
* Sentinel and MDE calls may block but are off the hot path, have timeouts, retry with
  backoff, and spool instead of failing.
* Do not place the analysis host on a proxy node unless you accept it sharing CPU with the
  data path.

## 9. Upgrade notes

* The HAProxy frontend `s3_front` was renamed to `s3_internal` and `s3_internet`. Update
  Prometheus queries and Grafana dashboards that reference it.
* `ober config` installs a systemd drop-in so HAProxy can create `/run/ober`.
* The TLS minimum of 1.2 now applies to internal clients too.
* Run `haproxy -c -f <haproxy.cfg>` once with the production AWS-LC build before rolling
  out, to validate the generated configuration.

## 10. Known limitations

* Stick tables are per node. With ECMP spreading one client over N nodes, the effective
  threshold is up to N times the configured limit.
* Connections rejected by the allowlist, the blocklist or an active ban are not logged, so
  they do not appear in `ober watch` or Sentinel.
* `deny_anonymous` also blocks SigV2 presigned URLs and POST-policy (browser form) uploads,
  because they carry no `Authorization` header. Leave it off if you use them.
* Raw logs need disk at high request rates. At approximately 400 bytes per request
  uncompressed (gzip approximately 10:1), 1,000 requests/s is about 35 GB/day raw, or about
  3.5 GB/day compressed; with 30 days retention plan for roughly 100 GB compressed.
