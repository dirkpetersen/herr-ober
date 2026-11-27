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
       [ Upstream Router (ECMP)       ]
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

1. **`ober-http` (HAProxy 3.3, OpenSSL):** SSL offloading, S3 headers, ACLs, proxies to Ceph RGW backends.
2. **HA Component (Mode-Dependent):**
    - **BGP Mode:** `ober-bgp` (ExaBGP) - Announces VIP(s) to router via BGP
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
**File:** `/etc/systemd/system.conf.d/90-herr-ober-watchdog.conf` (with `/dev/watchdog` present, bootstrap runs `systemctl daemon-reexec`, so it is active at once)

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
  dummy-devices:
    lo-vip:
      addresses:
        - 10.0.0.100/32  # Primary VIP
        # Additional VIPs can be added here
```

**RHEL (NetworkManager):** Configured via `nmcli` by `ober config`. Without NetworkManager (Amazon Linux), `ober config` writes systemd-networkd files `/etc/systemd/network/60-lo-vip.{netdev,network}`.

### D. SELinux (RHEL only)

SELinux stays enforcing. `ober bootstrap` (and `ober upgrade`) set `haproxy_connect_any`, build and load the
`herr_ober` policy module (`SELINUX_MODULE` in `bootstrap.py`, built with `checkmodule`) and label
`<install>/var/run` as `ober_state_t`. The module lets `keepalived_t` run the ober check and notify
scripts (stats port, state dir, `ping`, `ip`, the maintenance `track_file`). When the module cannot
be built, bootstrap warns and makes `keepalived_t` permissive instead.

---

## 4. Software Layer: The "Ober" Stack

For installation instructions, see [README.md](https://github.com/dirkpetersen/herr-ober/blob/main/README.md#quick-start).

### Component 1: The Controller (`ober`)

**Role:** Single CLI for all operations.

**Commands:**

- `ober bootstrap [path]` - Automated installation (detects OS, installs HAProxy/ExaBGP, applies tuning)
- `ober config [--dry-run] [--non-interactive]` - Configuration wizard (prompts prefilled from ober.yaml), or validate and render ober.yaml without prompts
- `ober sync` - Deprecated, changes nothing; the lists are fields of ober.yaml
- `ober status` - Show current state (supports `--json`)
- `ober start/stop/restart` - Service management (stop performs graceful shutdown with BGP withdraw)
- `ober health` - Long-running health check process for all VIPs (spawned by ExaBGP, text encoder)
- `ober logs [-f] [-n N] [--since T] [--service http|bgp|ha|blocklist|watch|all]` - View journald logs
- `ober doctor` - Diagnostic checks (works before/after bootstrap)
- `ober test` - Test BGP connectivity and config validity without starting services
- `ober upgrade` - Check and install HAProxy/ExaBGP updates
- `ober uninstall` - Clean removal
- Day-2 and cluster commands (`apply`, `rollback`, `push`, `cluster status`, `backend`, `cert install`, `metrics`, `maintenance`): see the [README usage section](https://github.com/dirkpetersen/herr-ober/blob/main/README.md#usage)

### Component 2: The Engine (`ober-http`)

**Role:** HAProxy 3.3 for SSL offloading. PPA, Debian and RHEL builds use OpenSSL; Amazon Linux 2027's `haproxy-awslc` uses AWS-LC FIPS (`ober doctor` shows the TLS library).
**Service File:** `/etc/systemd/system/ober-http.service`

Generated by `ober bootstrap` with paths dynamically configured.

**Config generated by `ober config`:** `<install-path>/etc/haproxy/haproxy.cfg`

Key features:

- Stats, metrics and health endpoint on port 8404, bound to 127.0.0.1 only. Add a management address for Prometheus with `stats_bind: [<ip>]` in `ober.yaml`.
- `/health` returns 503 when no backend server is up (`monitor fail if { nbsrv(...) lt 1 }`).
- Health endpoint at `/health` (returns 200 OK)
- Graceful reload for config changes (zero-downtime)
- Backend health checks: HTTP to Ceph RGW
- Load balancing: least-connections
- Timeouts: aggressive defaults for high-performance networks
- New certificates go in with `ober cert install` (runtime API swap, no reload)

### Component 3: The Announcer (`ober-bgp`)

**Role:** ExaBGP announces the VIPs. ExaBGP has no BFD, so link failure is detected by the BGP hold-time (3s default). With `bgp.bfd_enabled`, FRR `bfdd` runs alone in `ober-bfd.service` (`BindsTo=ober-bgp`) with one BFD peer per neighbor (300 ms x 3 = 0.9 s).
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

**VIP Assignment:** Each VIP has one owner node: `vips[].owner` when set, else md5 of the VIP over the sorted node IPs. Owner nodes get priority 100, backup nodes get priority 90. When a node fails, its VIP fails over to a backup node. When the node recovers, it reclaims its VIP after 30 s (`vrrp_startup_delay` after a start, `rise 15` of the health check after a health failure).

---

## 5. Configuration

### Main Config: `<install-path>/etc/ober.yaml`

Generated by `ober config` interactive wizard. Sections:

1. HA Mode Selection (BGP or Keepalived)
2. HA Configuration (mode-dependent):
    - **BGP Mode:** AS numbers, BGP neighbors, router-id, hold-time
    - **Keepalived Mode:** Peer node IPs, network interface, unicast/multicast, advertisement interval
3. VIP Settings (supports multiple VIPs)
    - BGP mode: All nodes share the same VIP(s)
    - Keepalived mode: One VIP per node recommended for even load distribution
4. Backend Settings (one backend group; all VIPs use it)
5. Certificate Settings
6. Logging Settings (retention time)

### Secrets: `~/.ober/login`

Secrets such as the `ober watch` tokens and the Entra ID client secrets for Sentinel and Defender for Endpoint. Permissions: 600.

---

## 6. Failure Scenarios & Recovery Logic

### BGP/ECMP Mode

| Event | Mechanism | Recovery Time |
| :--- | :--- | :--- |
| **HAProxy Process Crash** | `ober-http` stops. Systemd `BindsTo` stops `ober-bgp`. TCP Reset sent to Router. | **Instant (< 10ms)** |
| **HAProxy Stall/Freeze** | `ober health` gets timeout on `/health`. Sends `withdraw route`. | **~1-2 Seconds** |
| **ExaBGP Process Crash** | OS closes TCP Port 179. Router detects BGP Session Drop. | **Instant (< 10ms)** |
| **OS/Kernel Freeze** | Proxmox `i6300esb` Watchdog sees no kick for 10s. Hypervisor Hard-Resets VM. | **3s (BGP Hold) / 10s (Reboot)** |
| **Network Cable Cut** | BGP keepalives stop. The hold timer expires and the router removes the route. | **3s (BGP Hold)** |

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
- **Access logging** - one JSON line per request over UDP syslog to `security.log_target` (for `ober watch`), when it is set
- **Rate limiting** - only on Internet VIPs (per-IP connection limits and auto-ban); internal VIPs have none

### Certificate Management

- One issuer (lego, DNS-01) on one node or an admin host renews the certificate. Its deploy hook
  (`deploy/lego/ober-lego-hook`) runs `ober cert install --all-nodes`, which checks the PEM and
  swaps it into HAProxy over the runtime API. Nothing watches the files on disk.

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

1. Maintenance on: withdraw BGP routes or drop the VRRP priority, and wait until the node is out of rotation
2. Wait up to `drain_timeout` seconds (default 30) for open connections to close
3. Stop HAProxy (`BindsTo=` stops the HA unit with it)

### Signal Handling

- `ober health` handles SIGTERM/SIGINT gracefully

### Validation

- `ober config`, `ober start` and `ober test` validate ober.yaml and report every problem at once
- `ober test` checks BGP neighbor reachability (or keepalived peers), RGW backend connectivity and the certificate

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

- **PyPI name:** `herr-ober` (CLI command `ober`)
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
- `requests` - Health checks
- `pyyaml` - Config file handling

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

1. **Proxmox:** Set up the host and VM per [docs/proxmox-deployment.md](https://github.com/dirkpetersen/herr-ober/blob/main/docs/proxmox-deployment.md), including the `Intel 6300ESB` watchdog device.
2. **Network:** Verify both SR-IOV VFs are visible in the guest (`lspci`, `ethtool`), or that `virtio` multiqueue is active on the fallback path.
3. **Install:** See [README.md](https://github.com/dirkpetersen/herr-ober/blob/main/README.md#quick-start) for installation instructions.
4. **Bootstrap:** `sudo ober bootstrap` (installs HAProxy, ExaBGP, applies tuning)
5. **Configure:** `sudo ober config` (interactive wizard)
6. **Test:** `sudo ober test` (validate BGP connectivity without starting services)
7. **Verify:** `sudo ober doctor` and `sudo ober status`
8. **Router:** Configure ECMP and BGP Neighbors.
