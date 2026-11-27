# Herr Ober

**High-performance S3 ingress controller (BGP/ECMP or VRRP)**

[![PyPI version](https://badge.fury.io/py/herr-ober.svg)](https://pypi.org/project/herr-ober/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml/badge.svg)](https://github.com/dirkpetersen/herr-ober/actions/workflows/test.yml)

Herr Ober ("Head Waiter") is a lightweight, high-throughput (50GB/s+) ingress controller
designed for Ceph RGW clusters. It uses **HAProxy 3.4** for SSL offloading (the Ubuntu PPA and distro builds use OpenSSL;
`ober doctor` shows the TLS library) and one of two high-availability modes:

- **BGP/ECMP mode** uses **ExaBGP** for Layer 3 HA via BGP (requires a BGP-capable router).
- **Keepalived mode** uses **VRRP** for VIP failover (no BGP required).

**Supported:** Ubuntu, Debian, RHEL 10+ on Proxmox VMs (KVM), and Amazon Linux 2027 on EC2.
Python 3.12+. IPv4 only.

The whole stack is installed and managed by one CLI, `ober`, published on PyPI as
[`herr-ober`](https://pypi.org/project/herr-ober/).

## How it works

Herr Ober is a "shared nothing" cluster: each node runs the same stack and works on its own.

```text
       [ Upstream Router (ECMP) ]               (BGP mode)
              |        |        |
      +-------+        |        +-------+
      |                |                |
[ Herr Ober 01 ] [ Herr Ober 02 ] [ Herr Ober 03 ]
      |                |                |
      +-------+--------+--------+-------+
              | (Internal Network)
      [ Ceph Object Store (S3 Backend) ]
```

Each node runs these systemd services:

| Service | Component | Role |
|---------|-----------|------|
| `ober-http` | HAProxy 3.4 | SSL offloading, S3 frontends, proxying to the Ceph RGW backends |
| `ober-bgp` | ExaBGP | BGP mode only: announces the shared VIP(s) to the router |
| `ober-ha` | Keepalived | Keepalived mode only: VRRP failover of one VIP per node |

`ober-bgp` and `ober-ha` are bound to `ober-http` (`BindsTo=`): if HAProxy stops, the BGP routes
are withdrawn or the VIP fails over immediately. A Proxmox `i6300esb` hardware watchdog
hard-resets a VM whose OS freezes.

## Choosing an HA mode

The two modes are mutually exclusive and are selected with `ha_mode` in the configuration.

| | BGP/ECMP (`ha_mode: bgp`) | Keepalived (`ha_mode: keepalived`) |
|---|---|---|
| Network requirement | BGP-capable upstream router with ECMP | No router support; the nodes exchange VRRP (unicast by default) |
| VIPs | All nodes announce the same shared VIP(s) | One VIP per node, owned by one node at a time |
| Load spreading | Router ECMP across all healthy nodes | DNS round-robin over the VIPs (managed by you) |
| Failover | Route withdrawn, router stops sending traffic | VIP moves to a backup node |

See [Configuration](configuration.md#ha-modes) for the details of each mode.

## Failure and recovery

=== "BGP/ECMP mode"

    | Event | Recovery |
    |-------|----------|
    | **Node crash** | Traffic fails over via ECMP (instant) |
    | **OS freeze** | Proxmox watchdog hard-resets the VM (10s) |
    | **HAProxy crash** | BGP withdraws immediately (`BindsTo=`) |
    | **Network cut** | BGP hold timer expires and the router removes the route (3s default hold time) |

=== "Keepalived mode"

    | Event | Recovery |
    |-------|----------|
    | **Node crash** | VIP fails over to a backup node (instant) |
    | **OS freeze** | Proxmox watchdog resets the VM, VRRP timeout triggers failover (3-10s) |
    | **HAProxy crash** | Health check fails, priority drops, VIP fails over (4-6s) |
    | **Keepalived crash** | VRRP advertisements stop, VIP fails over (3s) |
    | **Network partition** | Risk of split-brain (both nodes may claim the VIP) |

The full failure matrix is in [Architecture](architecture.md).

## Security monitoring

For internet-exposed S3 endpoints, `ober watch` analyzes HAProxy logs off the data path, can
push proxy-side blocklists, and forwards findings (never raw logs) to Microsoft Sentinel and,
optionally, Defender for Endpoint. See [Security monitoring](security-monitoring.md) and
[Sentinel and Defender](sentinel.md).

## Where to go next

- [Getting started](getting-started.md): VM prerequisites, install, bootstrap, configure, start.
- [Configuration](configuration.md): HA modes, `ober.yaml` reference, file locations, secrets.
- [Operations](operations.md): apply and roll back changes, maintenance, one `ober.yaml` for
  the cluster, metrics, certificates, rolling upgrade.
- [CLI reference](cli.md): every `ober` command and option.
- [Architecture](architecture.md): kernel tuning, watchdog, components and failure handling.
- [Proxmox deployment](proxmox-deployment.md): host BIOS, SR-IOV, hugepages, VM definition.
- [Development](development.md): tests, linting and building these docs.

## License

MIT. Source code: [github.com/dirkpetersen/herr-ober](https://github.com/dirkpetersen/herr-ober).
