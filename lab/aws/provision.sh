#!/usr/bin/env bash
# Provision the lab hosts after up.sh. Idempotent.
# helper: bird (EPEL; BIRD 3, bird2 config syntax) as the upstream router: one BGP session with
#         BFD per ingress node, ECMP routes into the kernel. Also the S3 test client (awscli2),
#         with hosts entries that point the AWS S3 names at the first VIP.
# ingress nodes: only base packages. ober itself is installed by deploy.sh.
# The backend is AWS S3 itself, reached through the S3 gateway endpoint.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

NODE_IPS=""
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] || NODE_IPS="$NODE_IPS $ip"
done

# ober push, cluster status and --all-nodes run `ssh NODE sudo -n ober ...` as root. Real
# clusters need the same: a key for root on each node that may log in as a sudo user.
NODE_KEY="$LAB_DIR/node_key"
[ -f "$NODE_KEY" ] || ssh-keygen -q -t ed25519 -N '' -C herr-ober-lab-nodes -f "$NODE_KEY"
for name in "${INGRESS_NODES[@]}"; do
    log "Provisioning $name"
    if [ "$NODE_OS" = ubuntu ]; then
        lab_ssh "$name" 'sudo apt-get -qq update && sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y python3-venv python3-pip jq >/dev/null'
    else
        lab_ssh "$name" sudo dnf -y -q install python3-pip tar jq >/dev/null
    fi
    lab_scp "$NODE_KEY" "$name:/tmp/node_key"
    lab_ssh "$name" sudo bash -s -- "'$(cat "$NODE_KEY.pub")'" "$NODE_USER" <<'EOF'
set -euo pipefail
install -d -m 700 /root/.ssh
install -m 600 /tmp/node_key /root/.ssh/id_ed25519 && rm -f /tmp/node_key
grep -qF "$1" /home/$2/.ssh/authorized_keys || echo "$1" >>/home/$2/.ssh/authorized_keys
printf 'Host 10.77.1.*\n    User %s\n    StrictHostKeyChecking accept-new\n' "$2" >/root/.ssh/config
EOF
done

log "Provisioning helper"
NODE_CSV="${NODE_IPS# }"
# ssh joins its arguments with spaces, so pass the list as one comma-separated word
lab_ssh helper sudo bash -s -- "$HELPER_IP" "${NODE_CSV// /,}" "${VIPS[0]}" "$S3_HOST" "$BUCKET" <<'EOF'
set -euo pipefail
HELPER_IP="$1"; IFS=, read -r -a NODE_IPS <<<"$2"; VIP="$3"; S3_HOST="$4"; BUCKET="$5"
dnf -y -q install epel-release >/dev/null
dnf -y -q install bird awscli2 jq iproute tcpdump >/dev/null

# bird: AS 65000, one session per ingress node (AS 65001), BFD on each. ECMP: the kernel
# protocol merges the equal BGP routes into one multipath route. BIRD 3.4 misses the netlink
# update when a route grows from 1 to more paths; the kernel scan repairs it, so scan often.
{
    printf 'router id %s;\nlog syslog all;\n\n' "$HELPER_IP"
    printf 'ipv4 table master4 { export settle time 0 ms 10 ms; };\n\n'
    printf 'protocol device {}\n\n'
    # same timers as the ober defaults (bgp.bfd_interval_ms 300, bgp.bfd_multiplier 3)
    printf 'protocol bfd {\n  interface "*" { min rx interval 300 ms; min tx interval 300 ms; multiplier 3; };\n}\n\n'
    printf 'protocol kernel {\n  ipv4 { import none; export all; };\n  merge paths on;\n  scan time 2;\n}\n\n'
    i=0
    for ip in "${NODE_IPS[@]}"; do
        i=$((i + 1))
        printf 'protocol bgp ober%s {\n  local %s as 65000;\n  neighbor %s as 65001;\n' \
            "$i" "$HELPER_IP" "$ip"
        printf '  hold time 9;\n  keepalive time 3;\n  error wait time 5, 30;\n  bfd on;\n'
        printf '  ipv4 { import all; export none; };\n}\n\n'
    done
} >/etc/bird.conf
bird -p -c /etc/bird.conf
systemctl enable -q bird
systemctl restart bird

# Hash ECMP on L4 ports, so different client connections use different nodes.
sysctl -q -w net.ipv4.fib_multipath_hash_policy=1
echo 'net.ipv4.fib_multipath_hash_policy = 1' >/etc/sysctl.d/90-lab-ecmp.conf

# The client signs for the AWS names (SigV4 binds the host), so they must resolve to the VIP.
sed -i "/ $S3_HOST\$/d; / $BUCKET.$S3_HOST\$/d" /etc/hosts
printf '%s %s\n%s %s.%s\n' "$VIP" "$S3_HOST" "$VIP" "$BUCKET" "$S3_HOST" >>/etc/hosts
EOF
log "Provisioned. Next: ./deploy.sh <git-ref>"
