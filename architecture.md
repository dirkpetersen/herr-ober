# Project: Herr Ober (The Head Waiter)

**High-Performance S3 Ingress Controller Architecture**

**Target Throughput:** 50GB/s+ (Aggregate)
**Supported OS:** Ubuntu, Debian, RHEL 10+
**Infrastructure:** Proxmox VMs (KVM)
**Topology:** Layer 3 BGP ECMP (Active/Active)
**Network:** IPv4 only

---

## 1. High-Level Architecture

**Herr Ober** is a "Shared Nothing" cluster architecture with two High Availability modes:

**BGP/ECMP Mode:** Each node operates independently, announcing shared VIP(s) via BGP. The upstream router uses ECMP to distribute traffic across all healthy nodes.

**Keepalived Mode:** Each node operates independently with one VIP per node. VRRP provides VIP failover between nodes. DNS round-robin distributes client traffic.

```text
       [ Upstream Router (ECMP + BFD) ]
              |        |        |
      +-------+        |        +-------+
      |                |                |
[ Herr Ober 01 ] [ Herr Ober 02 ] [ Herr Ober 03 ]
      |                |                |
      +-------+--------+--------+-------+
              | (Internal Network)
      [ Ceph Object Store (S3 Backend) ]
```

### The Component Stack (Per Node)

1. **`ober-http` (HAProxy 3.3 with AWS-LC):** SSL offloading, S3 headers, ACLs, proxies to Ceph RGW backends.
2. **HA Component (Mode-Dependent):**
   - **BGP Mode:** `ober-bgp` (ExaBGP) - Announces VIP(s) to router via BGP with BFD
   - **Keepalived Mode:** `ober-ha` (Keepalived) - VRRP-based VIP failover between nodes
3. **`ober` CLI (Python 3.12+):** Controller for installation, configuration, and management. Distributed via PyPI.
4. **Proxmox Watchdog:** `i6300esb` hardware watchdog to hard-reset frozen VMs.

---

## 2. Infrastructure Layer (Proxmox Host)

To achieve 50GB/s and true HA, the VM hardware definition is critical.

### VM Hardware Configuration

* **CPU:** Type = **`host`** (Pass-through AES-NI instructions for SSL).
* **Memory:** Static allocation (Disable Ballooning to prevent latency spikes), backed by 1G hugepages.
* **Network:** **SR-IOV VFs** passed through to the VM (front and back on separate VLANs). Bridged virtio-net can't reach the per-node target.
  * **Fallback:** `VirtIO` with multiqueue set to the vCPU count (e.g., 8 queues for 8 vCPUs).
* **Watchdog:**
  * **Model:** `Intel 6300ESB`
  * **Action:** `Reset` (Hard reboot if guest OS freezes).

---

## 3. OS Layer

### A. Kernel Tuning (Applied by `ober bootstrap`)

Standard Linux TCP stacks choke at 50GB/s. The `ober bootstrap` command automatically writes these settings to `/etc/sysctl.d/99-herr-ober.conf`.

```ini
# Maximize Network Backlogs (Prevent drops during micro-bursts)
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 65535

# Huge TCP Buffers (128MB per socket)
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728

# Congestion Control (BBR)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Local Port Range
net.ipv4.ip_local_port_range = 1024 65535

# Panic on OOM (Trigger Watchdog faster)
vm.panic_on_oom = 1
kernel.panic = 10
```

### B. The "Nuclear" Watchdog Configuration

Configures Systemd to kick the virtual Proxmox hardware card.
**File:** `/etc/systemd/system.conf`

```ini
[Manager]
# Kick hardware every 5s. If OS freezes for 10s, Proxmox resets VM.
RuntimeWatchdogSec=10s
ShutdownWatchdogSec=2min
```

### C. The Dummy VIP Interface

Configuration to hold the Virtual IP(s) without ARP conflicts.

**Ubuntu/Debian (netplan):** `/etc/netplan/60-vip.yaml`

```yaml
network:
  version: 2
  tunnels:
    lo-vip:
      mode: dummy
      addresses:
        - 10.0.0.100/32  # Primary VIP
        # Additional VIPs can be added here
```

**RHEL (NetworkManager):** Configured via `nmcli` by `ober bootstrap`.

### D. SELinux (RHEL only)

SELinux should be disabled. `ober bootstrap` will disable it if found enabled.

---

## 4. Software Layer: The "Ober" Stack

For installation instructions, see [README.md](README.md#quick-start).

### Component 1: The Controller (`ober`)

**Role:** Single CLI for all operations.

**Commands:**
- `ober bootstrap [path]` - Automated installation (detects OS, installs HAProxy/ExaBGP, applies tuning)
- `ober config [--dry-run]` - Interactive configuration wizard (grouped sections, idempotent, auto-detects local IP)
- `ober sync` - Update external system whitelists:
  - `--routers <hostlist>` - Switches/routers
  - `--frontend-http <hostlist>` - Frontend systems (Weka) allowed HTTP
  - `--backend-http <hostlist>` - Backend systems (S3/Ceph)
  - No options: prompts for all; one option: updates only that category
- `ober status` - Show current state (supports `--json`)
- `ober start/stop/restart` - Service management (stop performs graceful shutdown with BGP withdraw)
- `ober health <vip>` - Long-running health check process (spawned by ExaBGP, text encoder)
- `ober logs [-f] [-n N] [--service http|bgp]` - View journald logs
- `ober doctor` - Diagnostic checks (works before/after bootstrap)
- `ober test` - Test BGP connectivity and config validity without starting services
- `ober upgrade` - Check and install HAProxy/ExaBGP updates
- `ober uninstall` - Clean removal

### Component 2: The Engine (`ober-http`)

**Role:** HAProxy 3.3 with AWS-LC for enterprise-class SSL performance.
**Service File:** `/etc/systemd/system/ober-http.service`

Generated by `ober bootstrap` with paths dynamically configured.

**Config generated by `ober config`:** `<install-path>/etc/haproxy/haproxy.cfg`

Key features:
- Stats endpoint on port 8404 (all interfaces, for Prometheus)
- Health endpoint at `/health` (returns 200 OK)
- Graceful reload for config changes (zero-downtime)
- Backend health checks: HTTP to Ceph RGW
- Load balancing: least-connections
- Timeouts: aggressive defaults for high-performance networks
- Auto-reloads when certificates change on disk

### Component 3: The Announcer (`ober-bgp`)

**Role:** ExaBGP with BFD support (enabled by default).
**Service File:** `/etc/systemd/system/ober-bgp.service`

Generated by `ober bootstrap` with paths dynamically configured.

**Config generated by `ober config`:** `<install-path>/etc/bgp/config.ini`

### Component 3 (Alternative): The Failover Manager (`ober-ha`)

**Role:** Keepalived with VRRP for VIP failover (alternative to BGP mode).
**Service File:** `/etc/systemd/system/ober-ha.service`

Generated by `ober bootstrap` with paths dynamically configured.

**Config generated by `ober config`:** `<install-path>/etc/keepalived/keepalived.conf`

**Key features:**
- Multiple VIPs (one per node) with hash-based ownership assignment
- Unicast mode default (multicast optional)
- Health tracking via `track_script` monitoring HAProxy `/health` endpoint
- Preempt enabled - nodes reclaim their VIPs when recovered
- No authentication (internal network assumed trusted)

**VIP Assignment:** Uses consistent hashing to deterministically assign each VIP to a node owner. Owner nodes get priority 100, backup nodes get priority 90. When a node fails, its VIP fails over to a backup node. When the node recovers, it reclaims its VIP automatically (preempt).

---

## 5. Configuration

### Main Config: `<install-path>/etc/ober.yaml`

Generated by `ober config` interactive wizard. Sections:
1. HA Mode Selection (BGP or Keepalived)
2. HA Configuration (mode-dependent):
   - **BGP Mode:** AS numbers, BGP neighbors, router-id, hold-time, BFD settings
   - **Keepalived Mode:** Peer node IPs, network interface, unicast/multicast, advertisement interval
3. VIP Settings (supports multiple VIPs)
   - BGP mode: All nodes share the same VIP(s)
   - Keepalived mode: One VIP per node recommended for even load distribution
4. Backend Settings (supports multiple backend groups - different Ceph clusters per VIP)
5. Certificate Settings
6. Logging Settings (retention time)

### Secrets: `~/.ober/login`

BGP passwords and other sensitive data. Permissions: 600.

---

## 6. Failure Scenarios & Recovery Logic

### BGP/ECMP Mode

| Event | Mechanism | Recovery Time |
| :--- | :--- | :--- |
| **HAProxy Process Crash** | `ober-http` stops. Systemd `BindsTo` stops `ober-bgp`. TCP Reset sent to Router. | **Instant (< 10ms)** |
| **HAProxy Stall/Freeze** | `ober health` gets timeout on `/health`. Sends `withdraw route`. | **~1-2 Seconds** |
| **ExaBGP Process Crash** | OS closes TCP Port 179. Router detects BGP Session Drop. | **Instant (< 10ms)** |
| **OS/Kernel Freeze** | Proxmox `i6300esb` Watchdog sees no kick for 10s. Hypervisor Hard-Resets VM. | **3s (BGP Hold) / 10s (Reboot)** |
| **Network Cable Cut** | BFD packets stop. Router tears down route. | **~150ms** |

### Keepalived Mode

| Event | Mechanism | Recovery Time |
| :--- | :--- | :--- |
| **HAProxy Process Crash** | `ober-http` stops. Systemd `BindsTo` stops `ober-ha`. VIP fails over to backup node. | **Instant** |
| **HAProxy Stall/Freeze** | `track_script` fails health check. Priority drops by 50. VIP fails over to backup. | **~4-6 Seconds** |
| **Keepalived Process Crash** | VRRP advertisements stop. Peer nodes detect timeout and take over VIPs. | **~3 Seconds** |
| **OS/Kernel Freeze** | Proxmox `i6300esb` Watchdog resets VM. VRRP timeout triggers failover. | **~3-10 Seconds** |
| **Network Partition** | VRRP packets stop. Risk of split-brain (both nodes claim VIP). | **Varies** |

**Note:** Keepalived's split-brain risk in network partition scenarios can be mitigated by ensuring reliable network between nodes. For critical deployments requiring fencing, use BGP mode.

---

## 7. HAProxy Implementation Details

### Protocol & Limits
- **HTTP/1.1 only** (no HTTP/2 support)
- **No request size limits** - unlimited for large S3 uploads
- **No access logging** - rely on Ceph RGW logs
- **No rate limiting** - handled by Ceph

### Certificate Management
- Certs provided via `--cert <path>` or HAProxy's built-in ACME support
- **Auto-reload**: Uses inotify (watchdog library) to detect cert changes and trigger graceful reload

### Open Questions
- S3 header handling (Host, Authorization, x-amz-*) may need future work

---

## 8. CLI Implementation Details

### Global Flags
- `--version` - Show ober version plus installed HAProxy/ExaBGP versions
- `--json` - JSON output for scripting
- `-q` / `--quiet` - Minimal output
- `-v` / `--verbose` - Detailed output

### Output & Errors
- Colored output when terminal supports it (using `rich` library)
- Error messages should be helpful with fix suggestions (e.g., "BGP neighbor unreachable. Check firewall rules on port 179")
- Exit codes: 0 for success, 1 for error

### Graceful Shutdown (`ober stop`)
1. Withdraw BGP routes
2. Wait for connections to drain
3. Stop HAProxy

### Signal Handling
- `ober health` handles SIGTERM/SIGINT gracefully

### Validation
- `ober sync` validates that hostnames/IPs resolve before updating whitelists
- `ober config` validates BGP neighbor reachability, RGW backend connectivity, certificate validity

### Config Wizard (`ober config`)
- Uses [python-inquirer](https://python-inquirer.readthedocs.io/en/latest/) for interactive prompts
- Grouped sections: BGP → VIP → Backends → Certs → Logging
- Allows skipping already-configured sections
- Auto-detects local IP and pre-fills defaults
- `--dry-run` to preview changes without applying

### Destructive Operations
- `ober uninstall` requires interactive confirmation
- If `ober bootstrap` fails midway, run `ober uninstall` before retrying

---

## 9. Network & Runtime Behavior

- **IPv4 only** - no IPv6 support
- **DNS resolution at runtime** - hostnames resolved when needed, not cached
- **Network timeouts** - aggressive values for high-performance networks (<1ms latency), not user-configurable
- **Download failures** - fail immediately (no retry logic)
- **No offline/proxy support** - assumes internet access
- **Services run as root**

---

## 10. Development & Packaging

### Package Info
- **PyPI name:** `ober`
- **GitHub:** https://github.com/dirkpetersen/herr-ober
- **Initial version:** 0.1.0
- **Versioning:** Semantic versioning (semver)
- **License:** MIT
- **PyPI classifiers:** Development Status :: 4 - Beta, Framework :: HAProxy

### Python Requirements
- **Python 3.12+** required
- **Shebang:** `#!/usr/bin/env python3`

### Dependencies (Runtime)
- `click` - CLI framework
- `python-inquirer` - Interactive prompts
- `rich` - Colored output, tables, progress bars
- `hostlist` - Slurm hostlist expansion
- `requests` - Health checks
- `pyyaml` - Config file handling
- `watchdog` - inotify for certificate file watching

### Dev Dependencies (`[dev]` extra)
- `pytest`, `pytest-cov` - Testing
- `ruff` - Linting and formatting
- `mypy` - Type checking

### Code Style
- **Docstrings:** Google style
- **Type annotations:** Required throughout
- **Linting/Formatting:** ruff

### Testing & CI
- **Framework:** pytest with mocked system calls
- **CI:** GitHub Actions
- **Auto-publish:** To PyPI on GitHub release tags
- **Type checking:** mypy in CI
- **Minimum coverage:** 50%

### Package Usage
- CLI: `ober <command>`
- Importable for scripting: `from ober import ...`

---

## 11. Deployment Checklist

1. **Proxmox:** Set up the host and VM per [docs/proxmox-deployment.md](docs/proxmox-deployment.md), including the `Intel 6300ESB` watchdog device.
2. **Network:** Verify both SR-IOV VFs are visible in the guest (`lspci`, `ethtool`), or that `virtio` multiqueue is active on the fallback path.
3. **Install:** See [README.md](README.md#quick-start) for installation instructions.
4. **Bootstrap:** `sudo ober bootstrap` (installs HAProxy, ExaBGP, applies tuning)
5. **Configure:** `sudo ober config` (interactive wizard)
6. **Test:** `sudo ober test` (validate BGP connectivity without starting services)
7. **Verify:** `sudo ober doctor` and `sudo ober status`
8. **Router:** Configure ECMP and BGP Neighbors (enable BFD).
