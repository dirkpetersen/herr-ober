# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance S3 ingress controller for Ceph RGW clusters. It uses HAProxy 3.3 (AWS-LC) for SSL offloading and ExaBGP for Layer 3 HA via BGP/ECMP.

- **PyPI package name:** `ober`
- **GitHub:** https://github.com/dirkpetersen/ober
- **Supported OS:** Ubuntu, Debian, RHEL 10+
- **Target environment:** Proxmox VMs
- **PyPI classifiers:** Development Status :: 4 - Beta, Framework :: HAProxy

## Architecture

"Shared Nothing" cluster - each node operates independently. No central management. Nodes announce a shared VIP via BGP; upstream router uses ECMP to distribute traffic. When a node fails, it withdraws the route and traffic shifts to remaining nodes.

Per-node components:
- **HAProxy 3.3 (AWS-LC)** - SSL termination, ACLs, proxies to Ceph RGW backends
- **ExaBGP** - Announces VIP(s) to upstream router via BGP
- **ober CLI** - Python controller that manages everything

## CLI Commands

Distribution via PyPI (pip/pipx). Single `ober` command with subcommands.

**Global flags:**
- `--version` - Show ober version plus installed HAProxy/ExaBGP versions
- `--json` - JSON output for scripting
- `-q` / `--quiet` - Minimal output
- `-v` / `--verbose` - Detailed output

**Output:** Colored when terminal supports it, plain otherwise. Uses `rich` library.

**Versioning:** Semantic versioning (semver).

**Error messages:** Should be helpful and suggest fixes (e.g., "BGP neighbor unreachable. Check firewall rules on port 179").

### `ober bootstrap [path]`
Fully automated installation:
- Auto-detects OS and uses appropriate package manager (apt for Ubuntu/Debian, dnf for RHEL)
- VIP interface: netplan on Ubuntu/Debian, NetworkManager (nmcli) on RHEL
- Detects HAProxy version (default: ha33 AWS-LC build), offers prompt for different version
- Installs HAProxy, creates Python venv, applies kernel tuning
- Generates all configs: HAProxy, ExaBGP, systemd units, netplan VIP
- Shows progress bars/spinners during long operations
- Installation path logic:
  - If running inside a venv: install there
  - Otherwise: prompt with `/opt/ober/` as default
  - Or explicit: `ober bootstrap /custom/path`
- After completion, prompts user to run `ober config`

### `ober config [--dry-run]`
Interactive configuration using [python-inquirer](https://python-inquirer.readthedocs.io/en/latest/). Idempotent (can re-run to change settings).

Use `--dry-run` to validate and preview changes without applying.

Auto-detects where possible (local IP, etc.) and pre-fills defaults.

Configures:
- BGP parameters: local-as, peer-as, neighbor(s) (router IPs), router-id, local-address
  - Default AS numbers: local-as=65001, peer-as=65000 (private AS range)
  - Default hold-time: 3 seconds (fast failover)
  - Supports multiple BGP neighbors (redundant routers)
- BFD (Bidirectional Forwarding Detection) - asks if router supports it, defaults to yes
- VIP(s) - supports multiple, defaults to one
- Ceph RGW backends - supports multiple backend groups (different Ceph clusters for different VIPs)
- Certs: `--cert <path>` or HAProxy's built-in ACME support
- Health check interval (default: 1 second)
- Log retention time (systemd journal)
- HAProxy stats endpoint (exposed on all interfaces for Prometheus scraping)

Validates during config:
- BGP neighbor reachability
- RGW backend connectivity
- Certificate validity

### `ober sync [OPTIONS]`
Quick updates for external system whitelists. Accepts Slurm hostlists or IP addresses.

Options:
- `--routers <hostlist>` - Switches/routers
- `--frontend-http <hostlist>` - Frontend systems (Weka, etc.) allowed HTTP
- `--backend-http <hostlist>` - Backend systems (S3/Ceph)

If no option specified, prompts for all three. If one option used, only updates that category.

Validates that hostnames/IPs resolve before updating whitelists.

### `ober status`
Shows current state:
- BGP session status (up/down)
- HAProxy health
- Announced routes
- Includes `systemctl status` output for all daemons
- Supports `--json` for machine-readable output

### `ober start` / `ober stop` / `ober restart`
Service management wrappers for systemd services.

`ober stop` performs graceful shutdown: withdraws BGP routes first, waits for connections to drain, then stops HAProxy.

### `ober health <vip>`
Long-running process spawned by ExaBGP. Checks HAProxy health endpoint, writes `announce`/`withdraw` commands to stdout for ExaBGP to process. Uses text encoder for simplicity.

### `ober uninstall`
Reverses bootstrap: removes systemd units, configs, cleans up installation.

Note: If `ober bootstrap` fails midway, run `ober uninstall` before retrying.

### `ober upgrade`
Checks for HAProxy/ExaBGP updates. Shows available versions and what would be updated, requires confirmation before installing.

### `ober logs [-f] [-n LINES] [--service SERVICE]`
Convenience command to tail/view journald logs for ober services.
- `-f` / `--follow` - Tail logs in real-time
- `-n` / `--lines` - Number of lines to show
- `--service` - Filter by service (`http`, `bgp`, or all if omitted)

### `ober doctor`
Diagnostic command that checks everything is correctly installed and configured, reports any issues.

Can run before bootstrap to check prerequisites (Python version, root access, supported OS).

### `ober test`
Test BGP connectivity and config validity without starting services. Useful for validating configuration before going live.

## Configuration

- Format: YAML
- Location: `<install-path>/etc/ober.yaml`
- Nodes and routers specified as IP lists or Slurm hostlists
- **Secrets** (BGP passwords, etc.): stored separately in `~/.ober/login` (permissions: 600), not in main config

## HAProxy Stats & Health

- Stats port: 8404
- Exposed on all interfaces for Prometheus scraping
- Health endpoint (`/health`): returns HTTP 200 OK when healthy
- Backend health checks: HTTP to Ceph RGW (standard method)
- Load balancing: least-connections
- Connection limits: dynamic (HAProxy managed)
- Timeouts: aggressive defaults for high-performance networks
- Certificate reload: auto-detect changes and reload
- HTTP/1.1 only (no HTTP/2)
- No request size limits (unlimited for large S3 uploads)
- No access logging (rely on Ceph RGW)
- No rate limiting (handled by Ceph)

**Open question:** S3 header handling (Host, Authorization, x-amz-*) - may need future work.

## Project Structure

```
ober/
├── ober/              # Main package (flat layout)
│   ├── __init__.py
│   ├── cli.py         # Click commands
│   └── ...
├── tests/
├── pyproject.toml     # Build config, dependencies
└── ...
```

## Testing & CI

- Framework: pytest (local with mocked system calls)
- CI: GitHub Actions
- Auto-publish to PyPI on GitHub release tags
- Linting/Formatting: ruff (run by Claude Code, no pre-commit hooks)
- Type checking: mypy in CI
- Full type annotations required
- Minimum test coverage: 50%
- Dev dependencies separate from runtime
- Maintain CHANGELOG.md
- Support `pip`, `uv`, and `pipx` for dependency management
- Initial version: 0.1.0
- Docstrings: Google style

### Integration Test Infrastructure

End-to-end testing with mock S3 backends:

```
                    ┌─────────────┐
                    │   boto3     │
                    │   client    │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │  HAProxy    │
                    │  (test cfg) │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
       ┌─────────────┐          ┌─────────────┐
       │   moto S3   │          │   moto S3   │
       │  (port 5001)│          │  (port 5002)│
       └─────────────┘          └─────────────┘
```

**Components:**
- **2x moto S3 servers** - Mock S3 backends running as daemon processes (ports 5001, 5002)
- **HAProxy** - Test configuration proxying to both moto instances (least-connections)
- **boto3 client** - Uploads auto-generated test files through HAProxy

**Test scenarios:**
- Upload/download files of various sizes through HAProxy
- Verify least-connections load balancing across backends
- Test HAProxy health checks detect moto backend failures
- Test graceful reload of HAProxy config
- Test certificate handling (self-signed for tests)

**What we CAN'T test on single host:**
- BGP announcement/withdrawal (requires router or network namespace trickery)
- ECMP failover (requires multiple nodes)
- BFD (requires router support)

BGP-related code should be unit tested with mocked ExaBGP interactions.

**pytest fixture example:**
```python
import pytest
import subprocess
import time
import boto3
from moto.server import create_backend_app
import threading

@pytest.fixture(scope="session")
def moto_servers():
    """Start two moto S3 servers as backends."""
    servers = []
    for port in [5001, 5002]:
        app = create_backend_app("s3")
        thread = threading.Thread(
            target=lambda p=port: app.run(host="127.0.0.1", port=p, threaded=True),
            daemon=True
        )
        thread.start()
        servers.append(f"http://127.0.0.1:{port}")
    time.sleep(1)  # Wait for servers to start
    yield servers

@pytest.fixture(scope="session")
def haproxy_test(moto_servers, tmp_path_factory):
    """Start HAProxy with test config pointing to moto backends."""
    cfg_dir = tmp_path_factory.mktemp("haproxy")
    cfg_file = cfg_dir / "haproxy.cfg"
    cfg_file.write_text(f'''
global
    daemon

defaults
    mode http
    timeout connect 5s
    timeout client 30s
    timeout server 30s

frontend s3_front
    bind *:8080
    default_backend s3_back

backend s3_back
    balance leastconn
    option httpchk GET /
    server moto1 127.0.0.1:5001 check
    server moto2 127.0.0.1:5002 check
''')
    proc = subprocess.Popen(["haproxy", "-f", str(cfg_file)])
    time.sleep(1)
    yield "http://127.0.0.1:8080"
    proc.terminate()

@pytest.fixture
def s3_client(haproxy_test):
    """boto3 client configured to use HAProxy endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=haproxy_test,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
```

## Package Usage

- CLI: `ober <command>`
- Importable for scripting: `from ober import ...`

## Code Style

- Shebang: `#!/usr/bin/env python3`
- Docstrings: Google style
- Type annotations: Required throughout
- Linting: ruff
- Formatting: ruff format

## CLI Behavior

- Exit codes: 0 for success, 1 for error
- Destructive operations (`ober uninstall`): require interactive confirmation
- Unsupported OS: reject immediately with clear error message
- Signal handling: `ober health` handles SIGTERM/SIGINT gracefully
- Network timeouts: aggressive values for high-performance networks (<1ms latency), not user-configurable
- DNS resolution: hostnames resolved at runtime, not cached
- Download failures: fail immediately (no retry logic)
- Assumes internet access (no offline/proxy support)
- `ober config` wizard: grouped sections (BGP → VIP → Backends → Certs → etc.), allows skipping already-configured sections
- IPv4 only (no IPv6 support)
- Services run as root
- SELinux: assume disabled on RHEL (bootstrap should disable if enabled)

## Dependencies

Python 3.12+ required. Key packages:
- `click` - CLI framework
- `python-inquirer` - Interactive prompts
- `rich` - Colored output, tables, progress bars
- `hostlist` - Slurm hostlist expansion
- `requests` - Health checks
- `pyyaml` - Config file handling
- `watchdog` - inotify for certificate file watching

Dev dependencies (optional `[dev]` extra):
- `pytest`, `pytest-cov` - Testing
- `moto[server]` - Mock S3 backend servers for integration tests
- `boto3` - S3 client for integration tests
- `ruff` - Linting/formatting
- `mypy` - Type checking

## Systemd Services

- `ober-http.service` - HAProxy
- `ober-bgp.service` - ExaBGP (bound to ober-http via `BindsTo=`)

Critical: `ober-bgp` is bound to `ober-http`. If HAProxy dies, BGP withdraws immediately.

HAProxy config changes: prefer graceful reload (zero-downtime) over restart.

## Logging

- Systemd journal only
- Log retention time configured during `ober config`
- Log messages use `[ober]` prefix

## Key Paths (default installation)

- `/opt/ober/bin/ober` - CLI entry point
- `/opt/ober/venv/` - Python venv with ExaBGP
- `/opt/ober/etc/ober.yaml` - Main configuration
- `/opt/ober/etc/haproxy/haproxy.cfg` - HAProxy config
- `/opt/ober/etc/haproxy/whitelist.lst` - HTTP bypass whitelist
- `/opt/ober/etc/bgp/config.ini` - ExaBGP config
- `/opt/ober/etc/certs/` - SSL certificates
