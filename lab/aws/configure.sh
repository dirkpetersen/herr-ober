#!/usr/bin/env bash
# configure.sh bgp|keepalived: bootstrap (first run only), write ober.yaml + ober.local.yaml on
# each ingress node, render the configs and start ober. Then install the lab server cert for
# the AWS S3 names on all nodes with `ober cert install --all-nodes`, and copy the lab CA to
# the helper (~ec2-user/lab-ca.pem, for AWS_CA_BUNDLE). Run after deploy.sh.
# Backend: AWS S3 over HTTPS (needs ober with backends[].tls, 0.5+).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MODE="${1:?usage: configure.sh bgp|keepalived}"
NODE_IPS=()
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] || NODE_IPS+=("$ip")
done
NODES_CSV="$(IFS=,; echo "${NODE_IPS[*]}")"

# Lab CA and server cert for the AWS S3 names (the client resolves them to the VIP).
CA="$LAB_DIR/lab-ca.pem"
CERT="$LAB_DIR/server.pem"
if [ ! -f "$CERT" ]; then
    (
        umask 077
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' EXIT
        openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj "/CN=herr-ober lab CA" \
            -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign \
            -keyout "$tmp/ca.key" -out "$CA" 2>/dev/null
        openssl req -newkey rsa:2048 -nodes -subj "/CN=$S3_HOST" \
            -keyout "$tmp/srv.key" -out "$tmp/srv.csr" 2>/dev/null
        printf 'subjectAltName=DNS:%s,DNS:*.%s\nextendedKeyUsage=serverAuth\n' \
            "$S3_HOST" "$S3_HOST" >"$tmp/ext"
        openssl x509 -req -in "$tmp/srv.csr" -CA "$CA" -CAkey "$tmp/ca.key" -days 30 \
            -extfile "$tmp/ext" -out "$tmp/srv.pem" 2>/dev/null
        cat "$tmp/srv.pem" "$tmp/srv.key" >"$CERT"
    )
    log "Created lab CA $CA and server cert $CERT"
fi

for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] && continue
    log "Configuring $name ($MODE)"
    lab_ssh "$name" sudo bash -s -- "$MODE" "$ip" "$NODES_CSV" "$HELPER_IP" \
        "$(IFS=,; echo "${VIPS[*]}")" "$S3_HOST" <<'EOF'
set -euo pipefail
MODE="$1"; IP="$2"; NODES_CSV="$3"; HELPER="$4"; IFS=, read -r -a VIPS <<<"$5"; S3="$6"
I=/opt/herr-ober
[ -f /etc/ober/install_path ] || ober bootstrap -y >/dev/null
systemctl is-active -q ober-http && ober stop >/dev/null 2>&1 || true
mkdir -p $I/etc/certs
if [ ! -f $I/etc/certs/server.pem ]; then
    # placeholder until `ober cert install` below; HAProxy needs a cert to start
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=$S3" \
        -addext "subjectAltName=DNS:$S3,DNS:*.$S3" \
        -keyout /tmp/k.pem -out /tmp/c.pem 2>/dev/null
    cat /tmp/c.pem /tmp/k.pem >$I/etc/certs/server.pem && rm -f /tmp/k.pem /tmp/c.pem
    chmod 600 $I/etc/certs/server.pem
fi
BACKEND="{name: s3, servers: ['$S3:443'], tls: true, health_check_expect: 'rstatus ^[234]'}"
{
    echo "ha_mode: $MODE"
    echo "nodes: [$NODES_CSV]"
    echo "bgp: {local_as: 65001, peer_as: 65000, neighbors: [$HELPER], bfd_enabled: true}"
    echo "keepalived: {peers: [$NODES_CSV]}"
    echo "vips:"
    for v in "${VIPS[@]}"; do echo "  - {address: $v}"; done
    echo "backends:"
    echo "  - $BACKEND"
    echo "certs: {path: $I/etc/certs/server.pem}"
    echo "security: {s3_hostnames: [$S3]}"
} >$I/etc/ober.yaml
echo "local_ip: $IP" >$I/etc/ober.local.yaml
ober config --non-interactive >/dev/null
ober start
EOF
done

log "Installing the lab server cert on all nodes"
lab_scp "$CERT" ober1:/tmp/server.pem
lab_ssh ober1 'sudo ober cert install --all-nodes /tmp/server.pem; rc=$?; rm -f /tmp/server.pem; exit $rc'
lab_scp "$CA" helper:lab-ca.pem
