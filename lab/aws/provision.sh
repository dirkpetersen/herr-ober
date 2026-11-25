#!/usr/bin/env bash
# Provision the lab hosts after up.sh. Idempotent.
# helper: two moto S3 servers (the "RGW" backends), GoBGP as the upstream router with an
#         ECMP route installer, and the S3 test client (awscli via pip).
# ingress nodes: only base packages. ober itself is installed by deploy.sh.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

GOBGP_VERSION="${GOBGP_VERSION:-4.11.0}"
NODE_IPS=""
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ ip _ <<<"$spec"
    [ "$name" = helper ] || NODE_IPS="$NODE_IPS $ip"
done

for name in "${INGRESS_NODES[@]}"; do
    log "Provisioning $name"
    lab_ssh "$name" sudo dnf -y -q install python3-pip tar jq >/dev/null
done

log "Provisioning helper"
NODE_CSV="${NODE_IPS# }"
# ssh joins its arguments with spaces, so pass the list as one comma-separated word
lab_ssh helper sudo bash -s -- "$GOBGP_VERSION" "$HELPER_IP" "${NODE_CSV// /,}" <<'EOF'
set -euo pipefail
GOBGP_VERSION="$1"; HELPER_IP="$2"; IFS=, read -r -a NODE_IPS <<<"$3"
dnf -y -q install python3-pip tar jq iproute >/dev/null

# S3 backends: two moto servers, one per port.
if [ ! -x /opt/lab/bin/moto_server ]; then
    python3 -m venv /opt/lab
    /opt/lab/bin/pip -q install 'moto[server]' awscli
fi
for port in 9001 9002; do
    cat >/etc/systemd/system/lab-s3-$port.service <<UNIT
[Unit]
Description=lab S3 backend (moto) on $port
After=network-online.target
[Service]
ExecStart=/opt/lab/bin/moto_server -H 0.0.0.0 -p $port
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
done

# GoBGP: AS 65000, passive peers for the ingress nodes (AS 65001).
if [ ! -x /usr/local/bin/gobgpd ] || ! /usr/local/bin/gobgp --version | grep -q "$GOBGP_VERSION"; then
    curl -fsSL "https://github.com/osrg/gobgp/releases/download/v$GOBGP_VERSION/gobgp_${GOBGP_VERSION}_linux_arm64.tar.gz" \
        | tar -xz -C /usr/local/bin gobgp gobgpd
fi
mkdir -p /etc/gobgp
{
    printf '[global.config]\n  as = 65000\n  router-id = "%s"\n\n' "$HELPER_IP"
    printf '[global.use-multiple-paths.config]\n  enabled = true\n\n'
    for ip in "${NODE_IPS[@]}"; do
        printf '[[neighbors]]\n  [neighbors.config]\n    neighbor-address = "%s"\n    peer-as = 65001\n' "$ip"
        printf '  [neighbors.timers.config]\n    hold-time = 9\n    keepalive-interval = 3\n\n'
    done
} >/etc/gobgp/gobgpd.toml
cat >/etc/systemd/system/gobgpd.service <<'UNIT'
[Unit]
Description=GoBGP (lab upstream router)
After=network-online.target
[Service]
ExecStart=/usr/local/bin/gobgpd -f /etc/gobgp/gobgpd.toml -t toml
Restart=always
[Install]
WantedBy=multi-user.target
UNIT

# GoBGP does not program the kernel. This loop turns the RIB into ECMP kernel routes.
cat >/usr/local/bin/lab-route-sync <<'SH'
#!/usr/bin/env bash
# shortcut: polls the GoBGP RIB every second; fine for a lab, not a router.
while true; do
    want="$(/usr/local/bin/gobgp global rib -j 2>/dev/null \
        | jq -r 'to_entries[] | (.key | sub("/32$"; "")) as $p
                 | [.value[] | .attrs[] | select(.type==3) | .nexthop]
                 | unique | "\($p) \(join(","))"' 2>/dev/null || true)"
    # The kernel prints host routes without /32; nexthop lines start with whitespace.
    have="$(ip -4 route show proto 99 | awk '/^[0-9]/{print $1}')"
    while read -r prefix hops; do
        [ -n "$prefix" ] && [ -n "$hops" ] || continue
        args=()
        for h in ${hops//,/ }; do args+=(nexthop via "$h"); done
        ip route replace "$prefix" proto 99 "${args[@]}"
    done <<<"$want"
    for p in $have; do
        grep -q "^$p " <<<"$want" || ip route del "$p" proto 99
    done
    sleep 1
done
SH
chmod +x /usr/local/bin/lab-route-sync
cat >/etc/systemd/system/lab-route-sync.service <<'UNIT'
[Unit]
Description=Install GoBGP routes as ECMP kernel routes
After=gobgpd.service
[Service]
ExecStart=/usr/local/bin/lab-route-sync
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
# Hash ECMP on L4 ports, so different client connections use different nodes.
sysctl -q -w net.ipv4.fib_multipath_hash_policy=1
echo 'net.ipv4.fib_multipath_hash_policy = 1' >/etc/sysctl.d/90-lab-ecmp.conf

systemctl daemon-reload
systemctl enable --now lab-s3-9001 lab-s3-9002 gobgpd lab-route-sync
EOF
log "Provisioned. Next: ./deploy.sh <git-ref>"
