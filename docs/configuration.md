# Configuration

All settings live in one YAML file, `<install_path>/etc/ober.yaml`. The interactive wizard
`sudo ober config` writes it and generates the HAProxy, ExaBGP or keepalived files from it. The
wizard can be run again at any time: it loads the existing file and offers the current values as
defaults.

## The configuration wizard

`sudo ober config` asks its questions in this order:

| Section | Questions | Written to |
|---------|-----------|------------|
| 1. HA mode | BGP/ECMP or Keepalived/VRRP | `ha_mode` |
| 2. BGP or Keepalived | BGP: local AS, peer AS, neighbor IP(s), router ID, local address, hold time, BFD. Keepalived: peers (Slurm hostlist supported), interface, multicast or unicast, advertisement interval | `bgp` or `keepalived` |
| 3. VIPs | VIP address(es), then which VIPs are Internet-facing. For Internet VIPs also: S3 hostnames, allowed source CIDRs, trusted networks, `ober watch` syslog target | `vips`, `security` |
| 4. Backends | One or more backend groups: name, servers (`host:port`), health check path and interval | `backends` |
| 5. Certificates | Certificate file path, Let's Encrypt via Route53 DNS-01, or skip | `certs` |
| 6. Additional | Log retention (days), HAProxy stats port | `log_retention_days`, `stats_port` |

It then shows a summary and, after confirmation:

1. saves `ober.yaml`;
2. writes the security lists and `haproxy.cfg`;
3. in BGP mode, writes the ExaBGP config and the `lo-vip` dummy interface that holds the VIPs
   (netplan `/etc/netplan/60-vip.yaml` on Ubuntu/Debian, `nmcli` on RHEL); in keepalived mode,
   writes `keepalived.conf` (keepalived manages the VIPs itself);
4. as root, installs a systemd drop-in so HAProxy can create `/run/ober`, and rewrites the
   `ober watch` and blocklist sync units.

`ober config --dry-run` asks the same questions and prints the summary without applying
anything. It works without root.

VIPs are entered in CIDR notation; `/32` is added when no prefix is given. In keepalived mode,
the wizard warns when the number of VIPs differs from the number of nodes.

## HA modes

The two modes are mutually exclusive. `ha_mode` decides which configuration `ober config`
generates and which service `ober start` starts next to `ober-http`.

=== "BGP/ECMP (ha_mode: bgp)"

    ExaBGP announces the shared VIP(s) to the upstream router, which spreads traffic over all
    healthy nodes with ECMP. Every node announces the same VIPs.

    - The ExaBGP config (`<install_path>/etc/bgp/config.ini`) has one `neighbor` block per entry
      in `bgp.neighbors`, with IPv4 unicast, the configured hold time and, when `bfd_enabled`
      is true, BFD.
    - ExaBGP spawns the health checker `<venv>/bin/python -m ober.commands.health`. It polls
      HAProxy's `http://127.0.0.1:<stats_port>/health` and announces or withdraws
      `<vip>/32 next-hop self` for every configured VIP. On SIGTERM or SIGINT it withdraws all
      routes before exiting.
    - The VIPs are held on the dummy interface `lo-vip`, so they cause no ARP conflicts.
    - `ober test` checks that TCP port 179 is reachable on every neighbor.

=== "Keepalived (ha_mode: keepalived)"

    Each node owns one VIP, and VRRP moves a VIP to another node when its owner fails. Clients
    are spread over the nodes with DNS round-robin, which you manage outside Herr Ober.

    - `keepalived.conf` has one `vrrp_instance` per VIP. The nodes are the local IP plus
      `keepalived.peers`.
    - The owner of each VIP is chosen deterministically from the MD5 hash of the VIP over the
      sorted node list, so every node computes the same owner. The owner gets priority 100,
      the others 90. All instances start as `BACKUP` with preempt, so a recovered node takes
      its VIP back.
    - Enter the peers as IP addresses. The node list is the local IP plus the peers as
      entered, so hostnames would give each node a different list (and they are also written
      to `unicast_peer` unchanged).
    - `virtual_router_id` is MD5(VIP) % 255 + 1.
    - Unicast (`unicast_src_ip` + `unicast_peer`) is the default; multicast is optional. VRRP
      authentication is not used.
    - The `chk_haproxy` track script curls `/health` every 2 seconds (weight -50, fall and rise
      2), and `track_interface` lowers the priority by 50 when the interface goes down.
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
| `<install_path>/etc/haproxy/haproxy.cfg` | Generated HAProxy configuration |
| `<install_path>/etc/bgp/config.ini` | Generated ExaBGP configuration (BGP mode) |
| `<install_path>/etc/keepalived/keepalived.conf` | Generated keepalived configuration (keepalived mode) |
| `<install_path>/etc/certs/` | Certificates; the wizard suggests `server.pem` here |
| `<install_path>/etc/haproxy/allowlist.lst`, `trusted.lst`, `blocklist.lst` | Security lists for Internet VIPs (see [Security monitoring](security-monitoring.md)) |
| `<install_path>/etc/haproxy/routers.lst`, `frontend-http.lst`, `backend-http.lst` | Whitelists written by [`ober sync`](cli.md#ober-sync) |
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
| `log_retention_days` | `7` | Log retention in days |
| `stats_port` | `8404` | HAProxy stats port; also serves `/health` |
| `venv_path` | (unset) | Venv of a pipx install, saved when it differs from `<install_path>/venv` |

### bgp

Used when `ha_mode: bgp`.

| Key | Default | Description |
|-----|---------|-------------|
| `local_as` | `65001` | AS number of the node |
| `peer_as` | `65000` | AS number of the router |
| `neighbors` | `[]` | BGP neighbor IPs |
| `router_id` | `""` | BGP router ID |
| `local_address` | `""` | Local address of the BGP sessions |
| `hold_time` | `3` | BGP hold time in seconds |
| `bfd_enabled` | `true` | Enable BFD on every neighbor |

### keepalived

Used when `ha_mode: keepalived`.

| Key | Default | Description |
|-----|---------|-------------|
| `peers` | `[]` | IPs or hostnames of the other nodes (the wizard accepts Slurm hostlists) |
| `interface` | `""` | Interface for the VIPs; auto-detected when empty |
| `use_multicast` | `false` | Use multicast VRRP instead of unicast |
| `advert_int` | `1` | VRRP advertisement interval in seconds |

### vips

A list; one entry per VIP.

| Key | Default | Description |
|-----|---------|-------------|
| `address` | `""` | VIP in CIDR notation, for example `10.0.100.1/32` |
| `interface` | `lo-vip` | Interface name recorded for the VIP |
| `exposure` | `internal` | `internal` (unchanged behavior) or `internet` (hardened, see [Security monitoring](security-monitoring.md)) |

### backends

A list of backend groups (Ceph RGW servers).

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `""` | HAProxy backend name; the wizard suggests `s3_backend_1`, `s3_backend_2`, ... |
| `servers` | `[]` | Servers as `host:port` |
| `health_check_path` | `/` | Path of the HTTP health check |
| `health_check_interval` | `1000` | Health check interval in milliseconds |

Backends balance with `leastconn`. The generated S3 frontends route to the first backend
group, and they are only generated when VIPs, a certificate path and at least one backend
are configured.

### certs

| Key | Default | Description |
|-----|---------|-------------|
| `path` | `""` | PEM file with certificate and private key, used by the HTTPS frontends |
| `acme_enabled` | `false` | Set by the Route53 option of the wizard |
| `acme_email` | `""` | Email for Let's Encrypt notifications |
| `acme_domain` | `""` | Domain name of the certificate |
| `route53_enabled` | `false` | Let's Encrypt via Route53 DNS-01 |
| `route53_profile` | `default` | AWS profile for Route53 |
| `route53_hosted_zone_id` | `""` | Route53 hosted zone ID |

The Route53 option needs `boto3`; the wizard installs it if it is missing (it is also the
`route53` extra of the package). The generated HTTPS frontends use the certificate file in
`certs.path`.

### security, watch, sentinel, mde

These sections configure the hardening of Internet VIPs, the `ober watch` analysis host, and
the Microsoft Sentinel and Defender for Endpoint integrations. The wizard asks only for the
Internet-facing VIPs, `security.s3_hostnames`, `security.allowlist`,
`security.trusted_networks` and `security.log_target`; set everything else in `ober.yaml`.
All keys, defaults and their meaning are documented in
[Security monitoring](security-monitoring.md#1-expose-a-vip-to-the-internet).

## Examples

=== "BGP/ECMP"

    ```yaml
    install_path: /opt/pipx/venvs/herr-ober
    ha_mode: bgp
    bgp:
      local_as: 65001
      peer_as: 65000
      neighbors:
      - 10.0.0.1
      - 10.0.0.2
      router_id: 10.0.0.11
      local_address: 10.0.0.11
      hold_time: 3
      bfd_enabled: true
    vips:
    - address: 10.0.100.1/32
      interface: lo-vip
      exposure: internal
    backends:
    - name: s3_backend_1
      servers:
      - rgw01.internal:8080
      - rgw02.internal:8080
      health_check_path: /
      health_check_interval: 1000
    certs:
      path: /opt/pipx/venvs/herr-ober/etc/certs/server.pem
    log_retention_days: 7
    stats_port: 8404
    ```

=== "Keepalived"

    ```yaml
    install_path: /opt/pipx/venvs/herr-ober
    ha_mode: keepalived
    keepalived:
      peers:
      - 10.0.0.12
      - 10.0.0.13
      interface: ens18
      use_multicast: false
      advert_int: 1
    vips:
    - address: 192.168.1.100/32
      interface: lo-vip
      exposure: internal
    - address: 192.168.1.101/32
      interface: lo-vip
      exposure: internal
    - address: 192.168.1.102/32
      interface: lo-vip
      exposure: internal
    backends:
    - name: s3_backend_1
      servers:
      - rgw01.internal:8080
      - rgw02.internal:8080
      health_check_path: /
      health_check_interval: 1000
    certs:
      path: /opt/pipx/venvs/herr-ober/etc/certs/server.pem
    log_retention_days: 7
    stats_port: 8404
    ```

`ober config` writes every section with all keys, including the ones that are not used by the
selected mode. Missing keys take their defaults when the file is loaded.

## Secrets

Secrets never go into `ober.yaml`. They are read from `~/.ober/login` of the user running the
command, one `KEY=value` per line, with mode 600. The keys used by `ober watch` and the
blocklist commands are listed in [Security monitoring](security-monitoring.md#6-secrets).

## Applying changes

Run `sudo ober config` again after changing settings, or after editing `ober.yaml` by hand:
it regenerates `haproxy.cfg` and the ExaBGP or keepalived configuration. It does not restart
anything, so restart the services afterwards (`sudo ober restart`, or
`sudo ober restart --reload-only` for a zero-downtime HAProxy reload). See the
[CLI reference](cli.md).
