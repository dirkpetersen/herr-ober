# Configuration

All settings live in one YAML file, `<install_path>/etc/ober.yaml`. Values that differ between
nodes can go in `ober.local.yaml` next to it (see [Per-node values](#per-node-values)). The
interactive wizard `sudo ober config` writes `ober.yaml` and generates the HAProxy, ExaBGP or
keepalived files from it. The wizard can be run again at any time: every prompt is prefilled
from the existing file.

## The configuration wizard

`sudo ober config` asks its questions in this order:

| Section | Questions | Written to |
|---------|-----------|------------|
| 1. Node and HA mode | This node's IP (empty = auto-detect), then BGP/ECMP or Keepalived/VRRP | `local_ip`, `ha_mode` |
| 2. BGP or Keepalived | BGP: local AS, peer AS, neighbor IP(s), router ID, local address, hold time. Keepalived: peer IPs (Slurm hostlist supported, for example `10.0.0.[2-3]`), interface, multicast or unicast, advertisement interval | `bgp` or `keepalived` |
| 3. VIPs | VIP address(es), then which VIPs are Internet-facing. For Internet VIPs also: S3 hostnames, allowed source CIDRs, trusted networks, `ober watch` syslog target | `vips`, `security` |
| 4. Backends | The backend group: name, servers (`host:port`), health check path and interval | `backends` |
| 5. Certificates | Certificate file path. Without one, no S3 frontend can be generated and apply fails | `certs.path` |
| 6. Additional | Log retention (days), HAProxy stats port | `log_retention_days`, `stats_port` |

The keepalived peer prompt is the only place that accepts Slurm hostlists. BGP neighbors, VIPs
and backend servers are split on commas.

The wizard then shows a summary and, after confirmation:

1. saves `ober.yaml` (keys that `ober.local.yaml` has are saved there);
2. writes the security lists and `haproxy.cfg`;
3. in BGP mode, writes the ExaBGP config and the `lo-vip` dummy interface that holds the VIPs
   (netplan `/etc/netplan/60-vip.yaml` on Ubuntu, `nmcli` on RHEL, systemd-networkd
   files `/etc/systemd/network/60-lo-vip.*` when neither exists, for example on Amazon Linux);
   in keepalived mode, writes `keepalived.conf` and removes a `lo-vip` left from BGP mode
   (keepalived manages the VIPs itself);
4. as root, installs a systemd drop-in so HAProxy can create `/run/ober`, and rewrites the
   `ober watch` and blocklist sync units.

Generated files are only rewritten when their text changes. `ober config` does not reload
anything; use `sudo ober apply` or `sudo ober restart` afterwards.

| Option | Description |
|--------|-------------|
| `--dry-run` | Validate and show a summary without applying (no root needed; without a TTY it uses `ober.yaml` as is) |
| `--non-interactive` | No prompts: validate the existing `ober.yaml` and render all configs from it |

`ober config`, `ober start` and `ober test` refuse an invalid `ober.yaml` and list every
problem. `ober stop`, `ober status` and `ober logs` still work with an invalid file.

## HA modes

The two modes are mutually exclusive. `ha_mode` decides which configuration `ober config`
generates and which service `ober start` starts next to `ober-http`. `ober start` also disables
and stops the unit of the other mode.

=== "BGP/ECMP (ha_mode: bgp)"

    ExaBGP announces the shared VIP(s) to the upstream router, which spreads traffic over all
    healthy nodes with ECMP. Every node announces the same VIPs.

    - The ExaBGP config (`<install_path>/etc/bgp/config.ini`) has one `neighbor` block per entry
      in `bgp.neighbors`, with IPv4 unicast and the configured hold time. ExaBGP has no BFD, so
      a link failure is detected by the hold time (3 s by default), or in 0.9 s with
      `bgp.bfd_enabled` (FRR `bfdd`, unit `ober-bfd`, config `/etc/frr/bfdd.conf`).
    - ExaBGP spawns the health checker `<venv>/bin/python -m ober.commands.health`. It polls
      HAProxy's `http://127.0.0.1:<stats_port>/health` and announces `<vip>/32 next-hop self`
      for every configured VIP after `bgp.health_rise` good probes (not before
      `bgp.health_holddown` seconds). It withdraws after `bgp.health_fall` failed probes, or at
      once in maintenance mode. On SIGTERM or SIGINT it withdraws all routes before exiting. On
      SIGHUP (sent by `ober apply` and `ober rollback`) it reads the VIP list again.
    - The VIPs are held on the dummy interface `lo-vip`, so they cause no ARP conflicts.
    - `ober test` checks that TCP port 179 is reachable on every neighbor.

=== "Keepalived (ha_mode: keepalived)"

    Each node owns one VIP, and VRRP moves a VIP to another node when its owner fails. Clients
    are spread over the nodes with DNS round-robin, which you manage outside Herr Ober.

    - `keepalived.conf` has one `vrrp_instance` per VIP. The nodes are `local_ip` (or the
      detected IP) plus `keepalived.peers`. Duplicates and the node itself are dropped from the
      peers, so one `ober.yaml` can list all nodes.
    - Each VIP has one owner: `vips[].owner` when set, else the md5 of the VIP over the sorted
      node list, so every node computes the same owner. The owner gets priority 100, the others
      90. All instances start as `BACKUP` with preempt.
    - Enter the peers as IP addresses, so that every node has the same node list.
    - `virtual_router_id` is md5(VIP) % 255 + 1 over the sorted VIPs, or the next free ID on a
      collision.
    - Unicast (`unicast_src_ip` + `unicast_peer`) is the default; multicast is optional. VRRP
      authentication is not used.
    - The `chk_haproxy` track script probes `/health` every 2 seconds (weight -50, fall 2,
      rise 15). `ober maintenance on` lowers the priority by 100 through a `track_file`.
      With `keepalived.gateway_check` set, a node that cannot ping that IP goes to FAULT and
      releases its VIPs.
    - Failover timing: a failing node loses its VIPs in a few seconds. A started owner waits
      30 s (`vrrp_startup_delay`), and an owner with recovered health 30 s (`rise 15`), before
      it takes its VIP back.
    - Configure one VIP per node for even load distribution.
    - `ober test` validates the file with `keepalived -t` and pings every peer.

## Install path

Every command finds the install path the same way (`OberConfig()` default), in this order:

1. The `OBER_INSTALL_PATH` environment variable.
2. The pointer file `/etc/ober/install_path`, written by `ober bootstrap` and removed by
   `ober uninstall`.
3. `sys.prefix` when `ober` runs in a venv (for the recommended pipx install,
   `/opt/pipx/venvs/herr-ober`).
4. `<tempdir>/ober`, a fallback meant for tests.

## Files

| Path | Content |
|------|---------|
| `<install_path>/etc/ober.yaml` | Main configuration |
| `<install_path>/etc/ober.local.yaml` | Optional per-node overrides, see [Per-node values](#per-node-values) |
| `<install_path>/etc/haproxy/haproxy.cfg` | Generated HAProxy configuration |
| `<install_path>/etc/bgp/config.ini` | Generated ExaBGP configuration (BGP mode) |
| `<install_path>/etc/keepalived/keepalived.conf` | Generated keepalived configuration (keepalived mode) |
| `<install_path>/etc/certs/` | Certificates; the wizard suggests `server.pem` here |
| `<install_path>/etc/haproxy/allowlist.lst`, `trusted.lst`, `blocklist.lst` | Security lists for Internet VIPs (see [Security monitoring](security-monitoring.md)) |
| `<install_path>/var/history/` | States stored by `ober apply`, restored by `ober rollback` |
| `<install_path>/var/watch/` | `ober watch` data directory (default of `watch.data_dir`) |
| `/run/ober/haproxy.sock` | HAProxy admin socket (local, root only) |
| `/etc/ober/install_path` | Install path pointer |
| `/etc/sysctl.d/99-herr-ober.conf` | Kernel tuning written by `ober bootstrap` |
| `/etc/systemd/system/ober-*.service` | Units written by `ober bootstrap` |
| `~/.ober/login` | Secrets (mode 600), see [Secrets](#secrets) |

## ober.yaml reference

Field names and defaults below are those of `ober/config.py`.

### Top level

| Key | Default | Description |
|-----|---------|-------------|
| `install_path` | see [Install path](#install-path) | Root of the configuration tree |
| `ha_mode` | `bgp` | `bgp` or `keepalived` |
| `local_ip` | `""` | This node's IP, when the default route uses another NIC (keepalived `unicast_src_ip`, BGP router ID and local address). Per node |
| `nodes` | `[]` | All ingress nodes, for `ober cluster status`, `ober push` and `--all-nodes` |
| `log_retention_days` | `7` | Log retention in days |
| `stats_port` | `8404` | HAProxy stats port; also serves `/health` and `/metrics` |
| `stats_bind` | `[]` | Extra addresses for the stats port (always on 127.0.0.1), for example a management IP for Prometheus. Not `0.0.0.0` when a VIP is on the Internet |
| `drain_timeout` | `30` | Seconds `ober stop` waits for open connections to close |
| `venv_path` | (unset) | Venv of a pipx install, saved when it differs from `<install_path>/venv` |

### bgp

Used when `ha_mode: bgp`.

| Key | Default | Description |
|-----|---------|-------------|
| `local_as` | `65001` | AS number of the node |
| `peer_as` | `65000` | AS number of the router |
| `neighbors` | `[]` | BGP neighbor IPs |
| `router_id` | `""` | BGP router ID (empty = this node's IP). Per node |
| `local_address` | `""` | Local address of the BGP sessions (empty = this node's IP). Per node |
| `hold_time` | `3` | BGP hold time in seconds (0 or 3-65535) |
| `health_rise` | `3` | Good `/health` probes before the routes are announced |
| `health_fall` | `2` | Failed probes before the routes are withdrawn |
| `health_holddown` | `5` | Seconds after the start before the first announcement |
| `bfd_enabled` | `false` | Run FRR `bfdd` (unit `ober-bfd`) with one BFD peer per neighbor. Needs the `frr` package and BFD on the router |
| `bfd_interval_ms` | `300` | BFD transmit and receive interval (10-60000) |
| `bfd_multiplier` | `3` | Missed BFD packets before the peer is down (2-255) |

### keepalived

Used when `ha_mode: keepalived`.

| Key | Default | Description |
|-----|---------|-------------|
| `peers` | `[]` | IPs of the other nodes (the node itself is dropped, so all nodes can be listed) |
| `interface` | `""` | Interface for the VIPs; auto-detected when empty |
| `use_multicast` | `false` | Use multicast VRRP instead of unicast |
| `advert_int` | `1` | VRRP advertisement interval in seconds |
| `gateway_check` | `""` | IP to ping; a node that cannot reach it goes to FAULT and releases its VIPs |

### vips

A list; one entry per VIP.

| Key | Default | Description |
|-----|---------|-------------|
| `address` | `""` | One IPv4 address, bare or `/32`, for example `10.0.100.1` |
| `interface` | `lo-vip` | Interface name recorded for the VIP |
| `exposure` | `internal` | `internal` or `internet` (hardened, see [Security monitoring](security-monitoring.md)) |
| `owner` | `""` | Keepalived: the node IP (`local_ip` or one of `keepalived.peers`) that holds this VIP. Empty = md5 of the VIP picks one. Ignored in BGP mode (a warning) |

```yaml
vips:
  - {address: 192.168.1.100, owner: 10.0.0.1}
  - {address: 192.168.1.101, owner: 10.0.0.2}
  - {address: 192.168.1.102, owner: 10.0.0.3}
```

### backends

A list with one backend group (Ceph RGW servers). All VIPs use it; more than one group is an
error.

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `""` | HAProxy backend name; the wizard suggests `s3_backend_1` |
| `servers` | `[]` | Servers as `host:port`. The HAProxy server name comes from it (`rgw1:7480` is `rgw1_7480`) |
| `health_check_path` | `/` | Path of the HTTP health check |
| `health_check_interval` | `1000` | Health check interval in milliseconds |
| `fall` | `3` | Failed checks before a server is marked down |
| `rise` | `2` | Good checks before a server is marked up |
| `slowstart_s` | `10` | Seconds over which a recovered server gets its full share (0 = off) |
| `maxconn` | `0` | Connections per server; more wait in the queue (0 = no limit) |
| `health_check_expect` | `""` | `status <codes>` or `rstatus <regex>`; empty is the HAProxy default (2xx or 3xx) |
| `tls` | `false` | HTTPS to the servers (AWS S3, RGW behind TLS) |
| `tls_verify` | `true` | Verify the server cert (CA chain; the host name for servers given by name) |
| `tls_ca_file` | `""` | CA bundle for a private CA; empty uses the distro bundle (`/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem` or `/etc/ssl/certs/ca-certificates.crt`, else `@system-ca`) |

Backends balance with `leastconn`. The S3 frontends are only generated when VIPs, a
certificate path and a backend are configured.

### certs

| Key | Default | Description |
|-----|---------|-------------|
| `path` | `""` | PEM file with certificate and private key (or a directory of PEM files), used by the HTTPS frontends |

New certificates go in with `ober cert install`, which swaps them into HAProxy without a
reload. Renewal with lego and DNS-01 is described in
[Operations](operations.md#certificates). The `acme_*` and `route53_*` keys still load but do
nothing: Let's Encrypt through Route53 is not implemented.

### security, watch, sentinel, mde

These sections configure the hardening of Internet VIPs, the `ober watch` analysis host, and
the Microsoft Sentinel and Defender for Endpoint integrations. The wizard asks only for the
Internet-facing VIPs, `security.s3_hostnames`, `security.allowlist`,
`security.trusted_networks` and `security.log_target`; set everything else in `ober.yaml`.
All keys, defaults and their meaning are documented in
[Security monitoring](security-monitoring.md#1-expose-a-vip-to-the-internet).

## Per-node values

`ober.local.yaml` next to `ober.yaml` holds the values that must differ between nodes. It is
deep-merged over `ober.yaml` when the file is loaded, and `ober config` saves its keys back
there. Then `ober.yaml` can be the same on every node and `ober push` can copy it:

```yaml
# etc/ober.local.yaml on node ober2
local_ip: 10.0.0.2
bgp:
  router_id: 10.0.0.2
```

`ober push` refuses an `ober.yaml` that sets `local_ip`, `bgp.router_id` or
`bgp.local_address`. See [Operations](operations.md#one-oberyaml-for-the-whole-cluster).

## Examples

=== "BGP/ECMP"

    ```yaml
    ha_mode: bgp
    nodes: [ober1, ober2, ober3]
    bgp:
      local_as: 65001
      peer_as: 65000
      neighbors:
      - 10.0.0.1
      - 10.0.0.2
      hold_time: 3
    vips:
    - address: 10.0.100.1
      exposure: internal
    backends:
    - name: s3_backend_1
      servers:
      - rgw01.internal:8080
      - rgw02.internal:8080
    certs:
      path: /opt/pipx/venvs/herr-ober/etc/certs/server.pem
    ```

=== "Keepalived"

    ```yaml
    ha_mode: keepalived
    nodes: [10.0.0.11, 10.0.0.12, 10.0.0.13]
    keepalived:
      peers: [10.0.0.11, 10.0.0.12, 10.0.0.13]
      interface: ens18
    vips:
    - {address: 192.168.1.100, owner: 10.0.0.11}
    - {address: 192.168.1.101, owner: 10.0.0.12}
    - {address: 192.168.1.102, owner: 10.0.0.13}
    backends:
    - name: s3_backend_1
      servers:
      - rgw01.internal:8080
      - rgw02.internal:8080
    certs:
      path: /opt/pipx/venvs/herr-ober/etc/certs/server.pem
    ```

Missing keys take their defaults when the file is loaded. Unknown keys and values of the wrong
type are reported by the validation.

## Secrets

Secrets never go into `ober.yaml`. They are read from `~/.ober/login` of the user running the
command, one `KEY=value` per line, with mode 600. The keys used by `ober watch`, the blocklist
commands and the Sentinel and Defender for Endpoint clients are listed in
[Security monitoring](security-monitoring.md#6-secrets).

## Applying changes

After you change `ober.yaml`, run `sudo ober apply`. It renders the files, shows the diff,
checks them, reloads and rolls back by itself when the new state fails its health checks. See
[Operations](operations.md#change-the-configuration-apply-and-rollback).
