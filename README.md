# 🤵 Herr Ober

**High-Performance S3 Ingress Controller (BGP/ECMP)**

Herr Ober ("Head Waiter") is a lightweight, high-throughput (50GB/s+) ingress controller designed for Ceph RGW clusters. It utilizes **HAProxy 3.3 (AWS-LC)** for SSL offloading and **ExaBGP** for Layer 3 High Availability via ECMP.

It is designed specifically for **Proxmox VMs (KVM)** running **Ubuntu 24.04**.

---

### 📚 Documentation
> **Start Here:** For deep internals, kernel tuning, and failure recovery logic, read **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

### 🚀 Quick Start (5 Minutes)

#### 1. Proxmox VM Prerequisites
Before installing software, ensure the VM is configured for 50GB/s throughput:
*   **CPU:** Type `host` (AES-NI passthrough).
*   **Network:** `VirtIO` with Multiqueue enabled (Queues = vCPUs).
*   **Hardware Watchdog:** Add device `Intel 6300ESB` -> Action: `Reset`.

#### 2. System Prep (Ubuntu 24.04)
Apply the 50GB/s kernel tuning and systemd watchdog settings:

```bash
# 1. Enable Hardware Watchdog support
sudo sed -i 's/#RuntimeWatchdogSec=0/RuntimeWatchdogSec=10s/' /etc/systemd/system.conf
sudo sed -i 's/#ShutdownWatchdogSec=10min/ShutdownWatchdogSec=2min/' /etc/systemd/system.conf

# 2. Apply Kernel Tuning for High Throughput
cat <<EOF | sudo tee /etc/sysctl.d/99-herr-ober.conf
net.core.netdev_max_backlog = 250000
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_congestion_control = bbr
vm.panic_on_oom = 1
kernel.panic = 10
EOF

sudo sysctl --system
sudo systemctl daemon-reload
```

#### 3. Install Herr Ober
We need basic python capabilities to bootstrap the rest of the system.

```bash
# 1. Install Basic Requirements
sudo apt-get update
sudo apt-get install -y python3-click python3-venv git

# 2. Setup Directories & Clone Repo
sudo mkdir -p /opt/ober/{bin,etc/haproxy,etc/bgp,etc/certs,etc/http}
# (Copy the 'ober' script to /opt/ober/bin/ober)
sudo cp bin/ober /opt/ober/bin/
sudo chmod +x /opt/ober/bin/ober
```

#### 4. Bootstrap (The Magic Step)
This command runs as root. It detects the latest **HAProxy 3.3 (AWS-LC)** repository, installs it, creates a Python Virtual Environment in `/opt/ober/venv`, and installs **ExaBGP** and dependencies into it.

```bash
sudo /opt/ober/bin/ober bootstrap
```

#### 5. Configure BGP & VIP
1.  **Set the VIP:** Edit `/etc/netplan/60-vip.yaml` to add your S3 Floating IP on a dummy interface.
2.  **Configure ExaBGP:** Edit `/opt/ober/etc/bgp/config.ini`.
    *   Set `local-address` (Node IP).
    *   Set `router-id` (Node IP).
    *   Set `neighbor` (Upstream Router IP).

#### 6. Enable Services
Deploy the provided Systemd units.

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ober-http
sudo systemctl enable --now ober-bgp
```

---

### 🎮 Usage

Herr Ober is controlled via the `ober` CLI.

#### Syncing the Guest List (Slurm ACLs)
When researchers need plain HTTP access (bypassing TLS) for specific compute nodes, use the `sync` command. This accepts Slurm-style hostlists.

```bash
# Updates HAProxy ACLs and reloads the service instantly
/opt/ober/bin/ober sync --allow-http "compute[001-100],login[01-02]"
```

#### Checking Health
Each node runs a local watchdog. To verify the node is announcing routes:

```bash
# Check BGP status
systemctl status ober-bgp

# Check internal Health API
curl http://127.0.0.1:8404/health
```

---

### ⚠️ Failure & Recovery
*   **Node Crash:** Traffic automatically fails over (ECMP) via Upstream Router.
*   **OS Freeze:** Proxmox Watchdog hard-resets the VM after 10s.
*   **Service Failure:** `ober-bgp` is bound to `ober-http`. If HAProxy dies, BGP withdraws immediately.

*See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed failure scenarios.*
