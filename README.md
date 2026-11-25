# Herr Ober

[![PyPI version](https://badge.fury.io/py/herr-ober.svg)](https://badge.fury.io/py/herr-ober)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml/badge.svg)](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/dirkpetersen/herr-ober/branch/main/graph/badge.svg)](https://codecov.io/gh/dirkpetersen/herr-ober)

**High-Performance S3 Ingress Controller (BGP/ECMP or VRRP)**

Herr Ober ("Head Waiter") is a lightweight, high-throughput (50GB/s+) ingress controller designed for Ceph RGW clusters. It uses **HAProxy 3.3 (AWS-LC)** for SSL offloading with two HA modes:

- **BGP/ECMP Mode** - Uses **ExaBGP** for Layer 3 HA via BGP (requires BGP-capable router)
- **Keepalived Mode** - Uses **VRRP** for VIP failover (no BGP required)

**Supported:** Ubuntu, Debian, RHEL 10+ on Proxmox VMs (KVM), and Amazon Linux 2023/2027 on EC2

---

## Documentation

For deep internals, kernel tuning, and failure recovery logic, see **[architecture.md](https://github.com/dirkpetersen/herr-ober/blob/main/architecture.md)**.
For Proxmox host and VM setup (SR-IOV, hugepages, CPU priority, BIOS), see **[docs/proxmox-deployment.md](docs/proxmox-deployment.md)**.

---

## Quick Start

### 1. Proxmox VM Prerequisites

Before installing, configure the VM for high throughput:

- **CPU:** type `host` (passes AES-NI/VAES through to HAProxy's TLS code).
- **Network:** SR-IOV VFs passed through to the VM. VirtIO with multiqueue (queues = vCPUs) is a fallback that tops out at tens of Gbit/s.
- **Memory:** fixed size, ballooning off, 1G hugepages.
- **Hardware watchdog:** add device `Intel 6300ESB` with action `Reset`.

For host BIOS and kernel settings, a full VM config and a verification checklist, see **[docs/proxmox-deployment.md](docs/proxmox-deployment.md)**.

### 2. Install

**One-liner (recommended):**
```bash
curl -fsSL https://raw.githubusercontent.com/dirkpetersen/herr-ober/main/install.sh | sudo bash
sudo ober bootstrap
```

**Manual install:**
```bash
sudo su -
apt install -y pipx
# Install globally (outside /root): the systemd units use ProtectHome=true
export PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
pipx install 'herr-ober[bgp]'   # [bgp] pins ExaBGP to the tested 4.2.x series
ober bootstrap
```

The one-liner also writes `/etc/profile.d/herr-ober.sh`, which exports `PIPX_HOME` and
`PIPX_BIN_DIR`, so a later `pipx upgrade herr-ober` in a new root shell finds the global install.

**Amazon Linux 2023/2027:** there is no pipx package, so the one-liner installs pipx into its
own venv at `/opt/pipx-tool` and then works as on the other systems. You can also install into
a plain venv outside `/root` and `/home` (as `lab/aws/deploy.sh` does):
```bash
sudo python3 -m venv /opt/herr-ober
sudo /opt/herr-ober/bin/pip install 'herr-ober[bgp]'
sudo ln -sf /opt/herr-ober/bin/ober /usr/local/bin/ober
sudo ober bootstrap
```
Amazon Linux has no NetworkManager, so in BGP mode `ober config` puts the VIPs on `lo-vip`
with systemd-networkd (`/etc/systemd/network/60-lo-vip.{netdev,network}`). These files match
only `lo-vip`; the ENI configuration of amazon-ec2-net-utils is not changed.

**Upgrading from v0.3.0 or earlier:** re-run the one-liner. The old install lived under
`/root/.local/pipx`; the installer prints how to keep it
(`sudo ober bootstrap /root/.local/pipx/venvs/herr-ober`) or copy its `etc/` to
`/opt/pipx/venvs/herr-ober/etc`. Because the `hostlist` dependency was replaced by
`python-hostlist`, reinstall into a clean venv so that no stale `hostlist` package stays:
```bash
PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx reinstall herr-ober
sudo ober upgrade   # installs ExaBGP again if the reinstall removed it, and rewrites the units
```

### 3. Configure

Interactive wizard to select HA mode (BGP or Keepalived) and configure VIPs, backends, and certificates.

```bash
sudo ober config
```

**Note:** Choose BGP mode if you have a BGP-capable router for ECMP load balancing. Choose Keepalived mode for VRRP-based failover without BGP.

### 4. Verify

```bash
# Check prerequisites and configuration
ober doctor

# View service status
ober status
```

### Example: Keepalived Mode Setup

For a 3-node keepalived cluster without BGP:

**Node 1:**
```bash
sudo ober bootstrap
sudo ober config
# Select: Keepalived/VRRP mode
# Peers: node2,node3 (or 10.0.0.2,10.0.0.3)
# VIPs: 192.168.1.100,192.168.1.101,192.168.1.102 (one per node)
sudo ober start
```

**Node 2 & 3:** Repeat the same steps with appropriate peer IPs.

**DNS Round-Robin:**
```
s3.example.com  IN  A  192.168.1.100
s3.example.com  IN  A  192.168.1.101
s3.example.com  IN  A  192.168.1.102
```

---

## Usage

### CLI Commands

```bash
ober bootstrap [path]     # Install and set up everything (--firewall: enable ufw/firewalld)
ober config [--dry-run]   # Interactive configuration wizard (choose BGP or Keepalived mode)
ober config --non-interactive  # Validate ober.yaml and render all configs, no prompts
ober status               # Show current state (--json for scripting)
ober start|stop|restart   # Service management (stop gracefully withdraws routes/releases VIPs)
                          # restart = validated zero-downtime reload; restart --hard = full restart
ober logs [-f] [-n N]     # View logs (--service http|bgp|ha|all to filter)
ober doctor               # Diagnostic checks
ober test                 # Test BGP connectivity without starting services
ober upgrade              # Check and install updates
ober uninstall            # Clean removal
```

### Editing ober.yaml directly

Every wizard prompt is prefilled from `ober.yaml`, so pressing Enter keeps a value. You can also
edit the file and apply it without prompts:

```bash
sudo ober config --dry-run          # validate and show a summary (works without a TTY)
sudo ober config --non-interactive  # validate and render haproxy.cfg and the HA config
```

`ober config`, `ober start` and `ober test` refuse an invalid `ober.yaml` and list every
problem. `ober stop`, `ober status` and `ober logs` still work. `local_ip` sets this node's IP
for keepalived `unicast_src_ip` and the BGP router-id/local-address when the default route
uses another NIC.

`ober sync` is deprecated: the files it wrote were never read. Use `bgp.neighbors` or
`keepalived.peers`, `security.internal_networks` or `security.allowlist`, and
`backends[].servers` in `ober.yaml`.

### Host firewall

`ober bootstrap` changes no firewall unless you add `--firewall`. With it, bootstrap allows the
SSH port(s) from `sshd -T` first, then 443/tcp and VRRP, and then enables ufw (Debian/Ubuntu) or
firewalld (RHEL). All rules are persistent. Port 80, BGP and the stats port are not opened.
Bootstrap also raises `nf_conntrack_max` to 1048576, because a stateful firewall tracks every
client and RGW connection.

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

### BGP/ECMP Mode
| Event | Recovery |
|-------|----------|
| **Node Crash** | Traffic fails over via ECMP (instant) |
| **OS Freeze** | Proxmox Watchdog hard-resets VM (10s) |
| **HAProxy Crash** | BGP withdraws immediately (`BindsTo=`) |
| **Network Cut** | BGP hold timer expires and the router removes the route (3s default hold-time) |

### Keepalived Mode
| Event | Recovery |
|-------|----------|
| **Node Crash** | VIP fails over to backup node (instant) |
| **OS Freeze** | Proxmox Watchdog resets VM, VRRP timeout triggers failover (3-10s) |
| **HAProxy Crash** | Health check fails, priority drops, VIP fails over (4-6s) |
| **Keepalived Crash** | VRRP advertisements stop, VIP fails over (3s) |
| **Network Partition** | Risk of split-brain (both nodes may claim VIP) |

See [architecture.md](https://github.com/dirkpetersen/herr-ober/blob/main/architecture.md) for detailed failure scenarios.

---

## Security Monitoring

For internet-exposed S3 endpoints, `ober watch` analyzes HAProxy logs off the data path, can
push proxy-side blocklists, and forwards findings (never raw logs) to Microsoft Sentinel and,
optionally, Defender for Endpoint.

Key points: `trusted_networks` are exempt from all automatic blocking and limits (put a
shared campus NAT range in both `allowlist` and `trusted_networks`); auto-ban counts only
signed 401/403 responses, unsigned failures use a separate higher threshold
(`autoban_unsigned_failures`); automatic blocks apply only to single IPs seen on Internet
VIPs. List the proxy nodes in `watch.syslog_allowed_sources`, bind `ober watch` to a
management network and firewall 5514/udp and 8405/tcp (UDP syslog is unauthenticated).
`OBER_WATCH_TOKEN` grants read access, `OBER_WATCH_ADMIN_TOKEN` is needed for blocklist
add/remove. Upgrading from earlier versions: the frontend `s3_front` became `s3_internal` /
`s3_internet` (update dashboards), and `ober config` installs a systemd drop-in for
`/run/ober`. Known limits: stick tables are per node (with ECMP the effective threshold is
N times the limit) and rejected connections are not logged. See **[docs/security-monitoring.md](docs/security-monitoring.md)**
and **[deploy/sentinel/README.md](deploy/sentinel/README.md)**.

---

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/dirkpetersen/herr-ober.git
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
