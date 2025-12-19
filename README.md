# Herr Ober

[![PyPI version](https://badge.fury.io/py/herr-ober.svg)](https://badge.fury.io/py/herr-ober)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/dirkpetersen/ober/actions/workflows/test.yml/badge.svg)](https://github.com/dirkpetersen/ober/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/dirkpetersen/ober/branch/main/graph/badge.svg)](https://codecov.io/gh/dirkpetersen/ober)

**High-Performance S3 Ingress Controller (BGP/ECMP)**

Herr Ober ("Head Waiter") is a lightweight, high-throughput (50GB/s+) ingress controller designed for Ceph RGW clusters. It uses **HAProxy 3.3 (AWS-LC)** for SSL offloading and **ExaBGP** for Layer 3 High Availability via ECMP.

**Supported:** Ubuntu, Debian, RHEL 10+ on Proxmox VMs (KVM)

---

## Documentation

For deep internals, kernel tuning, and failure recovery logic, see **[architecture.md](architecture.md)**.

---

## Quick Start

### 1. Proxmox VM Prerequisites

Before installing, ensure the VM is configured for 50GB/s throughput:

- **CPU:** Type `host` (AES-NI passthrough)
- **Network:** `VirtIO` with Multiqueue enabled (Queues = vCPUs)
- **Hardware Watchdog:** Add device `Intel 6300ESB` → Action: `Reset`

### 2. Install

```bash
# Install via pipx (recommended) - installs directly to /usr/local
sudo apt install -y pipx
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install herr-ober

# Or via pip in a venv
sudo python3 -m venv /opt/ober/venv
sudo /opt/ober/venv/bin/pip install herr-ober
sudo ln -s /opt/ober/venv/bin/ober /usr/local/bin/ober
```

### 3. Bootstrap

Run as root. Automatically installs HAProxy, ExaBGP, applies kernel tuning, and generates all configs.

```bash
sudo ober bootstrap
```

### 4. Configure

Interactive wizard to set up BGP, VIPs, backends, and certificates.

```bash
sudo ober config
```

### 5. Verify

```bash
# Check prerequisites and configuration
ober doctor

# View service status
ober status
```

---

## Usage

### CLI Commands

```bash
ober bootstrap [path]     # Install and set up everything
ober config [--dry-run]   # Interactive configuration wizard
ober sync                 # Update external system whitelists
ober status               # Show current state (--json for scripting)
ober start|stop|restart   # Service management (stop gracefully withdraws BGP)
ober logs [-f] [-n N]     # View logs (--service http|bgp to filter)
ober doctor               # Diagnostic checks
ober test                 # Test BGP connectivity without starting services
ober upgrade              # Check and install updates
ober uninstall            # Clean removal
```

### Updating Whitelists

Update external system whitelists with Slurm hostlists or IP addresses:

```bash
# Update all whitelists (interactive prompts)
ober sync

# Update specific whitelist
ober sync --routers "switch[01-04]"
ober sync --frontend-http "weka[001-100]"
ober sync --backend-http "rgw[01-08].internal"
```

### Checking Health

```bash
# Full status with systemd service info
ober status

# JSON output for monitoring integration
ober status --json

# Direct health endpoint
curl http://127.0.0.1:8404/health
```

---

## Failure & Recovery

| Event | Recovery |
|-------|----------|
| **Node Crash** | Traffic fails over via ECMP (instant) |
| **OS Freeze** | Proxmox Watchdog hard-resets VM (10s) |
| **HAProxy Crash** | BGP withdraws immediately (`BindsTo=`) |
| **Network Cut** | BFD detects and tears down route (~150ms) |

See [architecture.md](architecture.md) for detailed failure scenarios.

---

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/dirkpetersen/ober.git
cd ober
pip install -e ".[dev]"

# Run tests
pytest

# Lint and format
ruff check .
ruff format .

# Type check
mypy ober/
```

---

## License

MIT
