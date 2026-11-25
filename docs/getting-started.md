# Getting started

This page takes one node from an empty VM to running services. Repeat it on every node of the
cluster.

## Requirements

- Ubuntu, Debian or RHEL 10+ running in a Proxmox (KVM) VM.
- Python 3.12 or newer, and root access.
- Internet access during installation (packages, PyPI, and the HAProxy PPA on Ubuntu).
- For BGP mode, a BGP-capable upstream router with ECMP and BFD.

## 1. Prepare the Proxmox VM

Before installing, configure the VM for high throughput:

- **CPU:** type `host` (passes AES-NI/VAES through to HAProxy's TLS code).
- **Network:** SR-IOV VFs passed through to the VM. VirtIO with multiqueue (queues = vCPUs) is
  a fallback that tops out at tens of Gbit/s.
- **Memory:** fixed size, ballooning off, 1G hugepages.
- **Hardware watchdog:** add device `Intel 6300ESB` with action `Reset`.

For host BIOS and kernel settings, a full VM config and a verification checklist, see
[Proxmox deployment](proxmox-deployment.md).

## 2. Install

=== "One-liner (recommended)"

    ```bash
    curl -fsSL https://raw.githubusercontent.com/dirkpetersen/herr-ober/main/install.sh | sudo bash
    sudo ober bootstrap
    ```

    The installer installs pipx if needed, then `herr-ober` globally with
    `PIPX_HOME=/opt/pipx` and `PIPX_BIN_DIR=/usr/local/bin`. It also writes
    `/etc/profile.d/herr-ober.sh`, which exports both variables, so a later
    `pipx upgrade herr-ober` in a new root shell finds the global install.

=== "Manual install"

    ```bash
    sudo su -
    apt install -y pipx
    # Install globally (outside /root): the systemd units use ProtectHome=true
    export PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
    pipx install herr-ober
    ober bootstrap
    ```

!!! note "Why a global pipx install"
    The generated systemd units use `ProtectHome=true` and cannot see `/root`. If the install
    or venv path is under `/root` or `/home`, bootstrap falls back to `ProtectHome=read-only`
    and prints a warning recommending a global reinstall.

### Upgrading from v0.3.0 or earlier

Re-run the one-liner. The old install lived under `/root/.local/pipx`; the installer prints how
to keep it (`sudo ober bootstrap /root/.local/pipx/venvs/herr-ober`) or copy its `etc/` to
`/opt/pipx/venvs/herr-ober/etc`. Because the `hostlist` dependency was replaced by
`python-hostlist`, reinstall into a clean venv so a stale `hostlist` package cannot break
`ober sync`:

```bash
PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx reinstall herr-ober
```

## 3. Bootstrap

`sudo ober bootstrap` runs once per node. It:

1. Creates the directories under the install path and writes the install pointer
   `/etc/ober/install_path`.
2. Writes kernel tuning to `/etc/sysctl.d/99-herr-ober.conf` and applies it
   (see [Architecture](architecture.md)).
3. Installs `haproxy` and `keepalived` from the OS packages. On Ubuntu it first adds
   `ppa:vbernat/haproxy-3.3`.
4. Installs ExaBGP with pip into the venv `ober` runs from.
5. Writes the systemd units `ober-http`, `ober-bgp` and `ober-ha` (plus the `ober watch` and
   blocklist sync units).
6. Writes initial configuration templates.
7. Configures the systemd hardware watchdog (`RuntimeWatchdogSec=10s`).
8. Opens TCP ports 80 and 443 and the VRRP protocol (ufw, firewalld or iptables).

When `ober` runs from a venv (the pipx install above), bootstrap reuses an existing install
pointer, or else installs into that venv. Outside a venv, give the install path explicitly:

```bash
sudo ober bootstrap /opt/ober
```

If bootstrap fails midway, run `sudo ober uninstall` before retrying. See
[Configuration](configuration.md#install-path) for how later commands find the install path.

## 4. Configure

The interactive wizard selects the HA mode (BGP or Keepalived) and configures VIPs, backends
and certificates:

```bash
sudo ober config
```

Choose BGP mode if you have a BGP-capable router for ECMP load balancing. Choose Keepalived
mode for VRRP-based failover without BGP. `ober config --dry-run` previews the result without
root and without applying anything. The wizard and the YAML file it writes are described in
[Configuration](configuration.md).

## 5. Verify and start

```bash
ober doctor        # prerequisites and configuration
sudo ober test     # BGP neighbors or keepalived peers, backends, certificate
sudo ober start    # ober-http first, then ober-bgp or ober-ha
ober status        # service state, versions, HAProxy stats
```

Check the health endpoint directly with:

```bash
curl http://127.0.0.1:8404/health
```

## Example: three-node keepalived cluster

For a three-node keepalived cluster without BGP, on node 1:

```bash
sudo ober bootstrap
sudo ober config
# Select: Keepalived/VRRP mode
# Peers: 10.0.0.2,10.0.0.3 (the IPs of node 2 and node 3)
# VIPs: 192.168.1.100,192.168.1.101,192.168.1.102 (one per node)
sudo ober start
```

Repeat the same steps on nodes 2 and 3 with the matching peer IPs and the same VIPs. Use IP
addresses for the peers, so that every node computes the same VIP owners (see
[Configuration](configuration.md#ha-modes)). Then publish all VIPs under one name with DNS
round-robin:

```text
s3.example.com  IN  A  192.168.1.100
s3.example.com  IN  A  192.168.1.101
s3.example.com  IN  A  192.168.1.102
```

## Example: BGP/ECMP cluster

Every node announces the same VIP(s). Run `sudo ober bootstrap` and `sudo ober config` on each
node, select BGP/ECMP mode and enter the local and peer AS numbers, the BGP neighbor IP(s), the
router ID and the local address. Then run `sudo ober test`, which checks that TCP port 179 is
reachable on every neighbor, and `sudo ober start`. On the router, configure the nodes as BGP
neighbors with ECMP and enable BFD.
