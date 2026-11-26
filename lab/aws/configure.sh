#!/usr/bin/env bash
# configure.sh bgp|keepalived: bootstrap (first run only), write ober.yaml + ober.local.yaml
# and a self-signed cert on each ingress node, render the configs and start ober.
# Run after deploy.sh. Switching modes stops ober first.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MODE="${1:?usage: configure.sh bgp|keepalived}"
NODE_IPS=()
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] || NODE_IPS+=("$ip")
done
NODES_CSV="$(IFS=,; echo "${NODE_IPS[*]}")"

for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] && continue
    log "Configuring $name ($MODE)"
    lab_ssh "$name" sudo bash -s -- "$MODE" "$ip" "$NODES_CSV" "$HELPER_IP" "$(IFS=,; echo "${VIPS[*]}")" <<'EOF'
set -euo pipefail
MODE="$1"; IP="$2"; NODES_CSV="$3"; HELPER="$4"; IFS=, read -r -a VIPS <<<"$5"
I=/opt/herr-ober
[ -f /etc/ober/install_path ] || ober bootstrap -y >/dev/null
systemctl is-active -q ober-http && ober stop >/dev/null 2>&1 || true
mkdir -p $I/etc/certs
if [ ! -f $I/etc/certs/server.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=s3.lab.internal \
        -addext "subjectAltName=DNS:s3.lab.internal,DNS:*.s3.lab.internal" \
        -keyout /tmp/k.pem -out /tmp/c.pem 2>/dev/null
    cat /tmp/c.pem /tmp/k.pem >$I/etc/certs/server.pem && rm -f /tmp/k.pem /tmp/c.pem
    chmod 600 $I/etc/certs/server.pem
fi
{
    echo "ha_mode: $MODE"
    echo "nodes: [$NODES_CSV]"
    echo "bgp: {local_as: 65001, peer_as: 65000, neighbors: [$HELPER], bfd_enabled: false}"
    echo "keepalived: {peers: [$NODES_CSV]}"
    echo "vips:"
    for v in "${VIPS[@]}"; do echo "  - {address: $v}"; done
    echo "backends:"
    echo "  - {name: rgw, servers: ['$HELPER:9001'], health_check_path: /}"
    echo "certs: {path: $I/etc/certs/server.pem}"
    echo "security: {s3_hostnames: [s3.lab.internal]}"
} >$I/etc/ober.yaml
echo "local_ip: $IP" >$I/etc/ober.local.yaml
ober config --non-interactive >/dev/null
ober start
EOF
done
