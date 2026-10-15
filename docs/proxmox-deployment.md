# Proxmox Deployment

How to set up the Proxmox hosts and the KVM VMs that run Herr Ober at high throughput. This is the
host-side counterpart to `ober bootstrap`, which tunes the guest.

The reference deployment is three KVM VMs, one per Proxmox host. Each VM gets:

- its NIC passed through via SR-IOV;
- 8 vCPUs that run unpinned at high scheduling priority;
- memory backed by 1G hugepages.

The VMs run Herr Ober (HAProxy plus BGP or Keepalived) as the S3 ingress in front of a 32-node Ceph
RGW cluster.

- **Target:** 50 GB/s (400 Gbit/s) aggregate, about 135 Gbit/s per proxy node. With 8 vCPUs per VM,
  expect roughly 50–100 Gbit/s per node (about 20–35 GB/s in total). Client-side TLS is the limit,
  and load tests have to confirm the figure.
- **Why it's hard:** every byte crosses a proxy twice, client to proxy and proxy to RGW. Each node
  therefore needs about 135 Gbit/s in each direction on both its front and back sides.
- **What this means:** plain virtio bridging tops out far below that. SR-IOV (or full NIC
  passthrough) is required, together with the VM settings below.
- **Why KVM, not LXC:**
  - The proxy terminates TLS for Internet clients, so it needs a hypervisor boundary between it and
    the host. In the reference deployment the host runs the Ceph that holds every VM's disks.
  - Herr Ober needs its own kernel sysctls, a watchdog device and the Defender for Endpoint (MDE)
    kernel sensor. An LXC container can't provide any of these.
- **Ceph:** the S3 object store (32 HDD nodes) is a separate cluster. The Proxmox farm's own
  all-flash Ceph runs on the same hosts as ober, so ober must not compete with its OSDs for cores or
  NIC ports.

```
                       +---------------- Proxmox host (one of three) ----------------+
                       |                                                             |
 Clients  --HTTPS-->   |  [Front VF] --> [ ober VM (KVM)          ] --> [Back VF]    |  --HTTP-->  Ceph RGW
 (Internet,            |   no Proxmox     HAProxy + BGP/VRRP           no Proxmox    |             (32 nodes)
  institutions)        |   firewall       8 vCPUs, MDE sensor          firewall      |
                       |                          |                                  |
                       |                   [vmbr0, virtio]                           |
                       +--------------------------|----------------------------------+
                                                  v
                                      Management net (SSH, MDE, logs)
```

## Hardware and BIOS

In the reference deployment, each Proxmox host is a Supermicro AS-2115HS-TNR with:

- one AMD EPYC 9534 (64 cores);
- 2.3 TB of DDR5;
- 12 × 7.68 TB NVMe for Proxmox Ceph;
- an NVIDIA A16;
- 2 × 25G (ConnectX-6 Lx);
- 2 × 200G (ConnectX-6 VPI, PCIe Gen4 x16).

At 8 vCPUs, ober can't fill a 200G port, so port 1 carries ober and port 2 carries Proxmox Ceph.

| Setting | Value | Why |
| --- | --- | --- |
| NIC | ConnectX-6 VPI port 1 (200G), with front and back as two VFs on separate VLANs | A proxy sends out what it receives, so one full-duplex port carries both sides. Port 2 keeps Proxmox Ceph off ober's port |
| PCIe slot | Gen4 x16 riser slot; every slot attaches to the single CPU | The card moves about 200 Gbit/s per direction in total, shared by ober and Ceph |
| NIC firmware | Current version, Ethernet mode, SR-IOV on: `mlxconfig set LINK_TYPE_P1=2 LINK_TYPE_P2=2 SRIOV_EN=1 NUM_OF_VFS=8` | The VPI card can come up as InfiniBand, and VFs can't be created without SR-IOV in firmware |
| Virtualization | SVM mode on | Needed for KVM |
| IOMMU | IOMMU on, ACS on, SR-IOV on | Needed for passthrough; on AMD this needs no kernel boot option |
| Power profile | Maximum performance, Determinism slider set to Performance | Avoids clock ramp-up delay on bursts |
| C-states | Deep C-states (C6) off, or capped at C1/C1E | Wake-up latency costs throughput on small files |
| Turbo / SMT | On / on | TLS crypto benefits from clock speed; SMT helps HAProxy threads |
| NUMA (AMD EPYC) | NPS1, "L3 as NUMA" off | The single 64-core socket stays one NUMA node, so unpinned vCPUs and memory are always local to the NIC |
| Memory | 24 × 96 GB, all 12 channels, 2 DIMMs per channel (about 3600 MT/s on Genoa) | Memory bandwidth limits packet copy rates |
| PCIe ASPM | Off | Prevents link power-state latency on the NIC |

Plan for one node being down: either the two surviving nodes can carry peak load, or a reduced peak
is accepted during maintenance.

## Proxmox host settings

The host kernel command line stays as installed. On each of the three hosts:

- set the CPU governor at runtime;
- load VFIO;
- create persistent SR-IOV VFs;
- let Proxmox allocate the VM's 1G hugepages when the VM starts.

1. **Kernel command line and governor.** Leave the command line unchanged. On AMD the IOMMU is
   active once it's on in the BIOS, which is all VF passthrough needs. Set the performance governor
   at runtime with a small unit (from the `linux-cpupower` package):

   ```ini
   # /etc/systemd/system/cpu-performance.service
   [Unit]
   Description=CPU governor performance

   [Service]
   Type=oneshot
   ExecStart=/usr/bin/cpupower frequency-set -g performance

   [Install]
   WantedBy=multi-user.target
   ```

   Enable it with `systemctl enable --now cpu-performance`.

   Proxmox reserves the 1G hugepages itself when the ober VM starts. Start ober first at boot
   (`startup: order=1`), so that 64 free 1G pages are still available.
2. **VFIO modules.** Add `vfio`, `vfio_iommu_type1` and `vfio_pci` to `/etc/modules`, then run
   `update-initramfs -u -k all`.
3. **SR-IOV VFs.** Create them on port 1 (ober's port), and persist them across reboots with a udev
   rule or a small systemd unit:

   ```bash
   echo 4 > /sys/class/net/<pf>/device/sriov_numvfs
   ip link set <pf> vf 0 mac <fixed-mac> vlan <vlan-id> spoofchk on trust off
   ```

   Set the VLAN on the host, not in the guest, so the VM can't leave its VLAN.
4. **Resource mappings.** Register each VF under Datacenter → Resource Mappings → PCI Devices (for
   example `ober-front` and `ober-back`), so the VM config refers to names, not PCI addresses.
5. **Memory sharing.** KSM can stay on for the other VMs; it never merges the ober VM's 1G
   hugepages. With 2.3 TB RAM, avoid host swap or set `vm.swappiness=1`.
6. **CPU priority, no pinning.** Give ober `cpuunits: 10000` (the default is 100), so the scheduler
   serves its 8 vCPUs first when the host is busy. Set no `cpulimit`.
7. **Proxmox HA.** Leave the ober VMs out of Proxmox HA groups and never live-migrate them. Herr Ober
   fails over on its own (BGP or VRRP) within seconds, and a VM with a passed-through VF can't
   live-migrate anyway.
8. **Time sync.** Run chrony on hosts and VMs, so proxy logs line up with SIEM and EDR timestamps.

### CPU budget per host

Each host has one 64-core EPYC 9534 (128 threads), shared by:

- the host;
- 12 Ceph NVMe OSDs;
- the ober VM;
- the A16 VDI VMs and other VMs.

Nothing is pinned. The kernel scheduler places every thread, and `cpuunits` decides who waits when
cores are busy.

| Consumer | Share | Set by |
| --- | --- | --- |
| ober VM | 8 vCPUs, first claim on CPU time | `cores: 8`, `cpuunits: 10000` |
| Proxmox host and 12 Ceph OSDs | As needed; plan on 16–24 cores under load (2 or more per NVMe OSD) | Host processes, unrestricted |
| A16 VDI VMs and other VMs | The rest | Default `cpuunits` |

8 vCPUs cap ober at roughly 50–100 Gbit/s per node, and load tests will show the real figure. If
that falls short, adding cores takes only a VM restart.

## VM definition

Each ober VM gets:

- host CPU flags;
- 8 unpinned vCPUs at high priority;
- 64 GB RAM on 1G hugepages;
- two passed-through VFs;
- a virtual watchdog.

An example for `ober1` on `pve1`:

```
# /etc/pve/qemu-server/201.conf
name: ober1
ostype: l26
machine: q35
agent: 1
onboot: 1
startup: order=1
tablet: 0

# CPU: host flags (AES-NI, VAES, AVX-512 reach HAProxy's TLS code); no pinning, high priority
cpu: host
sockets: 1
cores: 8
cpuunits: 10000
# numa is required for hugepages; the host has one NUMA node, so nothing is bound
numa: 1

# Memory: fixed size, no ballooning, 1G hugepages reserved by Proxmox at VM start
memory: 65536
balloon: 0
hugepages: 1024
keephugepages: 1

# Data plane: SR-IOV VFs on 200G port 1 (client-facing and RGW-facing VLANs)
hostpci0: mapping=ober-front,pcie=1
hostpci1: mapping=ober-back,pcie=1

# Management only (SSH, MDE, log shipping); Proxmox firewall applies here
net0: virtio=BC:24:11:00:02:01,bridge=vmbr0,firewall=1

# Disk: OS and HAProxy binaries only, no data path
scsihw: virtio-scsi-single
scsi0: local-lvm:vm-201-disk-0,iothread=1,discard=on,ssd=1,size=64G

# Hung-guest recovery: ober bootstrap configures the systemd watchdog against this device
watchdog: model=i6300esb,action=reset
```

- **No `affinity`:** the 8 vCPUs float over all 128 host threads. `cpuunits: 10000` gives them first
  claim when the host is busy.
- **`cpu: host`:** this ties the VM to this CPU model. That's fine, because these VMs never migrate.
- **CPU sizing:** 8 vCPUs is the fixed budget, and TLS is the main CPU cost. If load tests fall
  short, cutting the number of TLS handshakes (keep-alive, session resumption) helps more than any
  other tuning.
- **Fallback without SR-IOV:** if passthrough isn't possible, use
  `net1: virtio=…,bridge=vmbr1,queues=8,firewall=0` and run `ethtool -L <if> combined 8` in the
  guest. Expect tens of Gbit/s per VM, well short of the target.

## Network and security

Data traffic goes straight from the NIC into the VM, so the Proxmox firewall never sees it.
Filtering on the data ports happens inside the VM (the firewall from `ober bootstrap` plus HAProxy's
rules) and in the upstream switch or router ACLs. Only the management NIC goes through a Proxmox
bridge, where the Proxmox firewall still applies.

- **No host IP on the data ports:** the Proxmox host itself has no IP on the Internet-facing VLAN.
  Its management stays on a separate NIC or VLAN.
- **Separate from Proxmox Ceph:** ober's VFs come from port 1 of the 200G card, and port 2 carries
  the Proxmox Ceph public and cluster networks. Both share the card's Gen4 x16 link, which still has
  plenty of room with 8 vCPUs of ober traffic.
- **VF lock-down:** fixed MAC, VLAN set on the host side, `spoofchk on`, `trust off`. The guest
  can't change its MAC or VLAN, or go promiscuous.
- **Keepalived:** use unicast VRRP, which is the Herr Ober default. VFs drop multicast unless
  `trust on` is set. A VIP is an extra IP on the VF's own MAC, so spoof checking does not block
  failover.
- **BGP:** the router peers with each VM's front-side address. ECMP with resilient (consistent)
  hashing keeps existing flows on their node when one node leaves.
- **MTU:** the client-facing side stays at 1500. The RGW-facing side uses 9000 only if switches, RGW
  nodes and VFs all match.
- **Switch ports:** each host's ober port (port 1) goes to a switch that carries both the client and
  RGW VLANs, with no oversubscription between the proxies and the RGW nodes.

## Inside the guest

Most guest tuning comes from `ober bootstrap`. Admins only need to install the OS (Ubuntu 26.04 LTS
in the reference deployment), install the EDR agent, and apply the NIC settings below that ober
doesn't set yet.

| Setting | Set by | Value |
| --- | --- | --- |
| Network backlog, `somaxconn` | ober bootstrap | 250000, 65535 |
| TCP buffers | ober bootstrap | Up to 128 MB per socket |
| Congestion control | ober bootstrap | BBR with `fq` qdisc |
| `ip_nonlocal_bind`, local port range | ober bootstrap | 1, 1024–65535 |
| Panic on OOM, reboot on panic | ober bootstrap | On, after 10 s |
| systemd watchdog | ober bootstrap | Uses `/dev/watchdog` from the `i6300esb` device |
| HAProxy threads | HAProxy | One per vCPU, auto-detected |
| NIC ring buffers | admin | `ethtool -G <vf> rx <max> tx <max>` (get the max from `ethtool -g`) |
| NIC queues | admin | `ethtool -L <vf> combined <vCPUs>` |
| IRQ spread | admin | `irqbalance` on (spreads the 8 queue IRQs across the 8 vCPUs) |
| MTU, RGW-facing VF | admin | 9000 only if the RGW network is jumbo end to end, else 1500 |
| MTU, client-facing VF | admin | 1500 |
| RHEL only | admin | `tuned-adm profile network-throughput` |
| EDR (e.g. Defender for Endpoint) | admin | Onboarded as on every other Linux host |

Persist the `ethtool` settings with a NetworkManager dispatcher script or a systemd-networkd `.link`
file. A later ober release may set them during bootstrap.

## Verification checklist

Run these after a host reboot, before installing Herr Ober.

**On each Proxmox host**

- [ ] `dmesg | grep -i AMD-Vi` shows the IOMMU enabled
- [ ] `cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages` shows 64 once the ober VM
      has started (`HugePages_Total` in `/proc/meminfo` counts only the default 2M pages)
- [ ] `cpupower frequency-info` reports governor `performance`
- [ ] `cat /sys/class/net/<pf>/device/sriov_numvfs` shows the expected VF count after a reboot
- [ ] `ip link show <pf>` lists each VF with its fixed MAC, its VLAN and `spoof checking on`

**Inside each ober VM**

- [ ] `lspci` lists both VFs, and `ethtool <if>` shows the expected link speed
- [ ] `grep -m1 -o -w -e aes -e vaes /proc/cpuinfo` finds AES instructions
- [ ] `wdctl` shows the `i6300esb` watchdog
- [ ] `mpstat 5` shows `%steal` near 0 during load tests. If it doesn't, other VMs are crowding ober
      and `cpuunits` needs to go higher
- [ ] Multi-stream `iperf3` (3.16 or later, `-P 16`) reaches near line rate between two ober VMs,
      and from an ober VM to an RGW node
- [ ] The EDR agent reports the VM as onboarded and healthy
- [ ] Failover test once ober runs: power off one VM. Its VIP moves (Keepalived), or its routes are
      withdrawn (BGP), within seconds

## Decisions before deployment

- Port plan: which port carries ober, which carries Proxmox Ceph, and which carries management and
  other VMs.
- CPU headroom: how many cores the Proxmox Ceph OSDs use under load today.
- Management: Proxmox management on its own NIC or VLAN, separate from the data ports.
- HA mode: BGP with ECMP (the router peers with the three VMs), or Keepalived with DNS round-robin.
- Addressing: VLANs and subnets for the Internet-facing VIPs, the internal VIPs and the RGW-facing
  side.
- Backend network: whether the RGW network is jumbo-frame end to end.
