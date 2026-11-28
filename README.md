# Herr Ober

[![PyPI version](https://badge.fury.io/py/herr-ober.svg)](https://badge.fury.io/py/herr-ober)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml/badge.svg)](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/dirkpetersen/herr-ober/branch/main/graph/badge.svg)](https://codecov.io/gh/dirkpetersen/herr-ober)

**High-Performance S3 Ingress Controller (BGP/ECMP or VRRP)**

Herr Ober ("Head Waiter") is a lightweight, high-throughput (50GB/s+) ingress controller designed for Ceph RGW clusters. It uses **HAProxy 3.4** (the Ubuntu PPA and distro builds use OpenSSL; `ober doctor` shows the TLS library) for SSL offloading with two HA modes:

- **BGP/ECMP Mode** - Uses **ExaBGP** for Layer 3 HA via BGP (requires BGP-capable router)
- **Keepalived Mode** - Uses **VRRP** for VIP failover (no BGP required)

**Supported:** Ubuntu, Debian, RHEL 10+ on Proxmox VMs (KVM), and Amazon Linux 2027 on EC2

---

## Documentation

The full documentation (getting started, configuration reference, CLI reference, deployment
and security guides) is at **<https://dirkpetersen.github.io/herr-ober/>**.

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

**One-liner (recommended):** use a release tag (see
[releases](https://github.com/dirkpetersen/herr-ober/releases)), not `main`, so you run a
reviewed script. The script installs the newest herr-ober from PyPI.
```bash
OBER_TAG=v0.4.0
curl -fsSL "https://raw.githubusercontent.com/dirkpetersen/herr-ober/$OBER_TAG/install.sh" | sudo bash
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

**Amazon Linux 2027:** there is no pipx package, so the one-liner installs pipx into its
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

**HAProxy 3.4 on RHEL 10:** RHEL, AlmaLinux and Rocky 10 ship only HAProxy 3.0. Before
`ober bootstrap`, build the 3.4 RPM from the Fedora source RPM on one EL10 host of the same CPU
architecture, and install it on each node (`dnf` then keeps it, because it is newer than 3.0):
```bash
sudo deploy/el10-haproxy/build-rpm.sh      # prints /var/tmp/haproxy-rpm.*/RPMS/<arch>/haproxy-3.4*.rpm
sudo dnf install -y /var/tmp/haproxy-rpm.*/RPMS/*/haproxy-3.4*.rpm
```
On Ubuntu, bootstrap uses `ppa:vbernat/haproxy-3.4`. That PPA builds only for 26.04 and later, so
24.04 gets the 3.3 PPA.

**Upgrading from v0.3.0 or earlier:** re-run the one-liner. The old install lived under
`/root/.local/pipx`; the installer prints how to keep it
(`sudo ober bootstrap /root/.local/pipx/venvs/herr-ober`) or copy its `etc/` to
`/opt/pipx/venvs/herr-ober/etc`. Herr Ober no longer depends on a hostlist package, so reinstall
into a clean venv so that no stale `hostlist` package stays:
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
# Peers: 10.0.0.2,10.0.0.3 (IP addresses, not hostnames)
# VIPs: 192.168.1.100,192.168.1.101,192.168.1.102 (one per node)
sudo ober start
```

**Node 2 & 3:** Repeat the same steps with appropriate peer IPs.

By default the md5 of each VIP picks its owner node (priority 100, the others 90), so the
split can be uneven. To place each VIP on a known node, set `owner` (an IP from `local_ip` and
`keepalived.peers`) in `ober.yaml`:

```yaml
vips:
  - {address: 192.168.1.100, owner: 10.0.0.1}
  - {address: 192.168.1.101, owner: 10.0.0.2}
  - {address: 192.168.1.102, owner: 10.0.0.3}
```

An owner that is not a cluster node is an error. In BGP mode `owner` is ignored (a warning).
A failing node loses its VIPs in a few seconds. A started owner waits 30 s
(`vrrp_startup_delay`), and an owner with recovered health 30 s (`rise`), before it takes its VIP back.

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
ober metrics [--output F] # Node state for Prometheus (deploy/prometheus/alerts.yml)
ober cluster status       # All nodes over SSH: VIP placement, drift (set nodes: in ober.yaml)
ober cluster upgrade      # Rolling upgrade of all nodes, this node last (see Rolling upgrade)
ober apply [-f FILE|-]    # Apply ober.yaml: diff, check, reload, health gate, auto roll back
ober push                 # ober apply on the other nodes, one at a time; stops at a failure
ober rollback [--list]    # Go back to the state before the last apply
ober cert install PEM [--all-nodes]  # Check cert+key, swap into HAProxy without reload
ober backend list|drain|maint|ready <server> [--all-nodes]  # RGW servers (this node or all)
ober start|stop|restart   # Service management (stop gracefully withdraws routes/releases VIPs)
                          # restart = validated zero-downtime reload; restart --hard = full restart
ober logs [-f] [-n N]     # View logs (--service http|bgp|bfd|ha|all to filter)
ober doctor               # Diagnostic checks
ober test                 # Test BGP connectivity without starting services
ober upgrade              # Re-render units, install HAProxy/ExaBGP updates (see Rolling upgrade)
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

<!-- --8<-- [start:operations] -->
### One ober.yaml for the whole cluster

Values that must differ between nodes go in `ober.local.yaml` next to `ober.yaml` on each
node. Its keys override `ober.yaml`, and `ober config` saves them back there:

```yaml
# etc/ober.local.yaml on node ober2
local_ip: 10.0.0.2
bgp:
  router_id: 10.0.0.2
```

Then `ober.yaml` is the same on every node. List the nodes once (`nodes: [ober1, ober2, ober3]`),
apply on one node, and push to the others:

```bash
sudo ober apply              # this node first
sudo ober push               # ssh NODE sudo -n ober apply -f - on each other node, one at a time
sudo ober cluster status     # VIP placement and config drift over all nodes
```

`ober push` refuses an `ober.yaml` that sets `local_ip`, `bgp.router_id` or
`bgp.local_address`, and stops at the first node that fails its check or health gate. That node
puts its old files back. Certificates are not copied. The SSH user needs a key without a prompt
and `sudo -n ober` on each node. SSH runs with `BatchMode=yes`, so the host keys must already be
in root's `known_hosts` (or set `StrictHostKeyChecking accept-new` for the node addresses).

### Metrics and alerts

HAProxy serves request and backend metrics on `:8404/metrics`. `ober metrics` adds what HAProxy
does not know: VIP announced or VRRP MASTER, VIP present, maintenance, backend servers UP,
certificate expiry, the config hash and the versions. Write it for the node_exporter textfile
collector every minute, for example from root's crontab:

```
* * * * * /usr/local/bin/ober metrics --output /var/lib/node_exporter/textfile/ober.prom
```

`deploy/prometheus/alerts.yml` has the alert rules: a VIP on no node or MASTER on two nodes,
a node healthy but not announcing, backends down, certificate under 21 days, and config or
version drift between nodes. Test them with `promtool test rules deploy/prometheus/alerts_test.yml`.

`ober sync` is deprecated: the files it wrote were never read. Use `bgp.neighbors` or
`keepalived.peers`, `security.internal_networks` or `security.allowlist`, and
`backends[].servers` in `ober.yaml`.

### Certificates

Use one issuer for the whole cluster, not one per node: behind ECMP or DNS round-robin a
per-node HTTP-01 challenge can land on the wrong node. The files in `deploy/lego/` run
[lego](https://go-acme.github.io/lego/) with the DNS-01 challenge on one node (or an admin
host that can SSH to all nodes):

- DNS-01 proves the domain with a DNS TXT record, so no node opens port 80.
- The certificate covers `s3.example.com` and `*.s3.example.com`. The wildcard is needed for
  virtual-host buckets (`bucket.s3.example.com`). `ober cert install` warns without it.
- After each new certificate, lego runs `ober-lego-hook`. The hook joins the certificate and
  key into one PEM and pipes it to `ober cert install --all-nodes [--name NAME] -`, which
  checks it and swaps it into HAProxy on every node without a reload.

```bash
sudo install -m 755 deploy/lego/ober-lego-hook /usr/local/bin/
sudo install -m 600 deploy/lego/ober-lego.env /etc/ober/lego.env    # then edit: DNS token
sudo install -m 644 deploy/lego/ober-lego.service deploy/lego/ober-lego.timer /etc/systemd/system/
sudo systemctl start ober-lego.service       # first certificate, check the journal
sudo systemctl enable --now ober-lego.timer  # checks twice a day, renews when due
```

The DNS API token stays in `/etc/ober/lego.env` (mode 600), never in `ober.yaml`. The unit is
written for lego v5 (`lego run --deploy-hook`); lego v4 uses `renew --renew-hook`, and the hook
reads the environment variables of both. The unit runs as root, so root needs an SSH key for
the other nodes and `sudo -n ober` there (as for `ober push`).

If the hook fails, lego keeps the new certificate in `/var/lib/lego/certificates/` and does
not run the hook again until the next renewal. Run the hook by hand or use `ober cert install`.
Expiry is visible early: `ober status` warns under 21 days and is critical under 7, and
`ober metrics` exports `ober_cert_remaining_seconds`, which fires the `OberCertExpiresSoon`
alert in `deploy/prometheus/alerts.yml` under 21 days.

### Rolling upgrade

Upgrade one node at a time. `sudo ober cluster upgrade` runs the steps below on each node over
SSH (this node last) and stops at the first failure, which leaves that node in maintenance.
`--command` sets the package upgrade command (default: the pipx command in step 3), `--nodes`
limits the nodes, `--timeout` (300 s) limits each wait, and `--dry-run` shows the steps. The
steps by hand, on each node:

1. `sudo ober cluster status` must exit 0 (OK) before you start.
2. `sudo ober maintenance on`. Wait until `sudo ober maintenance status` shows no announced
   routes (BGP) or no MASTER state (keepalived), so the traffic has moved to the other nodes.
3. As root: `PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade herr-ober` (or
   `pip install -U herr-ober` in the venv).
4. `sudo ober upgrade` (`-y` installs without asking). It re-renders the systemd units and offers HAProxy and ExaBGP updates.
   If it says `ober-http.service` changed, or it installed a new ExaBGP, run
   `sudo ober restart --hard` now, while the node has no traffic.
5. `sudo ober doctor` must show no failures.
6. `sudo ober maintenance off`.
7. Wait until `sudo ober cluster status` exits 0 again, then go to the next node.

BGP mode: the health process that ExaBGP started keeps running the old code until ExaBGP
restarts. A health process from an ober version without SIGHUP support dies on the SIGHUP that
`ober apply`, `ober rollback` and `ober push` send, and ExaBGP starts it again with the new
code. To avoid this while the node has traffic, run `sudo ober restart --hard` in step 4 also
in BGP mode.

### Host firewall

`ober bootstrap` changes no firewall unless you add `--firewall`. With it, bootstrap allows the
SSH port(s) from `sshd -T` first, then 443/tcp, VRRP and BFD (3784/udp), and then enables ufw
(Debian/Ubuntu) or firewalld (RHEL). When the firewall is already active, bootstrap adds only
443/tcp, VRRP and 3784/udp and keeps your SSH rules. If `sshd -T` fails, bootstrap stops before it changes the firewall. All
rules are persistent. Port 80, BGP and the stats port are not opened.
Bootstrap also raises `nf_conntrack_max` to 1048576, because a stateful firewall tracks every
client and RGW connection.

### AWS S3 or TLS backends

A backend can use HTTPS, so Herr Ober can front AWS S3 or an RGW behind TLS. Example for
AWS S3 in us-west-2:

```yaml
security:
  s3_hostnames: [s3.us-west-2.amazonaws.com]   # cert and SAN checks use these names
certs:
  path: /etc/ober/certs/s3.pem                  # a cert for s3.us-west-2.amazonaws.com
backends:
  - name: aws_s3
    servers: ["s3.us-west-2.amazonaws.com:443"]
    tls: true                     # HTTPS to the servers
    tls_verify: true              # default: verify with the distro CA bundle
    tls_ca_file: ""               # or a CA bundle for an RGW with a private CA
    health_check_expect: "rstatus ^[234]"   # optional: S3 answers GET / with 307 or 403
```

- SigV4 signs the Host header. Herr Ober passes Host unchanged, so clients must sign for the
  AWS name, and that name must resolve to the VIP (hosts file or private DNS).
- Clients then expect a cert for the AWS name from Herr Ober. Use a private CA that the clients
  trust, or for a test a self-signed cert and `aws --no-verify-ssl`.
- The SNI to the backend is the server's host name, which the AWS cert covers (also
  `*.s3.us-west-2.amazonaws.com`), so path-style and virtual-host style requests both work.
- TLS servers given by host name are resolved again through DNS (`resolvers sys`), because S3
  IP addresses change. Plain HTTP backends resolve names only at start. An IP server gets no
  SNI, so only the CA chain is verified.

<!-- --8<-- [end:operations] -->

### Checking Health

```bash
# Full status with systemd service info
ober status

# JSON output for monitoring integration
ober --json status

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
| **Network Cut** | BGP hold timer expires and the router removes the route (3s default hold-time); with BFD 0.9 s |

### BFD (BGP mode, optional)

ExaBGP has no BFD. With `bgp.bfd_enabled: true` each node also runs FRR `bfdd` alone (unit
`ober-bfd`, no FRR bgpd or zebra, so FRR never changes routes) with one single-hop BFD session
per `bgp.neighbors` entry, from `bgp.local_address` (or the node IP). The timers are
`bgp.bfd_interval_ms` (300) and `bgp.bfd_multiplier` (3): a dead node or link is detected in
0.9 s. `ober-bfd` has `BindsTo=ober-bgp`, so it stops with ExaBGP and HAProxy.

- Bootstrap installs the `frr` package and keeps the distro `frr.service` off. BFD stays off
  until FRR is installed, also when `bfd_enabled` is true (default `false`). Amazon Linux 2027
  has no `frr` package, so there is no BFD there (BGP hold timer only).
- The config is `/etc/frr/bfdd.conf` (bfdd reads no other path). `ober config` and `ober apply`
  write it and restart `ober-bfd` when it changes, which takes the BGP sessions down for a moment.
- The router must tie BFD to the BGP session, for example bird2: `protocol bfd { }` and
  `bfd on;` in each `protocol bgp`. It must accept UDP 3784 from the node addresses.
- `ober status` and `ober doctor` show the peer state (`vtysh -d bfdd -c 'show bfd peers'`);
  a peer that is down is WARN. `ober metrics` exports `ober_bfd_peer_up{peer}`.

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
cd herr-ober
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
