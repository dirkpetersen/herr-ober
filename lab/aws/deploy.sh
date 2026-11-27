#!/usr/bin/env bash
# deploy.sh <git-ref>: build a wheel of <git-ref> and install it on the ingress nodes.
# Not pipx (Amazon Linux 2027 has none), so it matches the earlier lab runs:, so ober goes into a plain venv at /opt/herr-ober
# (outside /root and /home, so the units can keep ProtectHome=true) and `ober` is linked
# into /usr/local/bin. Configuration is a separate step (ober bootstrap / ober config).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

REF="${1:?usage: deploy.sh <git-ref>}"
REPO="$(git -C "$LAB_DIR" rev-parse --show-toplevel)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

git -C "$REPO" archive --format=tar "$REF" | tar -x -C "$BUILD"
uv build -q --wheel --out-dir "$BUILD/dist" "$BUILD"
WHEEL="$(ls "$BUILD"/dist/*.whl)"
log "Built $(basename "$WHEEL") from $REF ($(git -C "$REPO" rev-parse --short "$REF"))"

for name in "${INGRESS_NODES[@]}"; do
    log "Installing on $name"
    lab_scp "$WHEEL" "$name:/tmp/"
    lab_ssh "$name" sudo bash -s -- "/tmp/$(basename "$WHEEL")" <<'EOF'
set -euo pipefail
[ -x /opt/herr-ober/bin/python ] || python3 -m venv /opt/herr-ober
/opt/herr-ober/bin/pip -q install --force-reinstall --no-deps "$1"
/opt/herr-ober/bin/pip -q install "$1"
ln -sf /opt/herr-ober/bin/ober /usr/local/bin/ober
# RHEL sudo has no /usr/local/bin in secure_path; same link as install.sh makes
[ -e /usr/bin/ober ] || ln -s /usr/local/bin/ober /usr/bin/ober
ober --version
EOF
done
