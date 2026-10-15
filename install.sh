#!/bin/bash
# Herr Ober installer - https://github.com/dirkpetersen/herr-ober
# Usage: curl -fsSL https://raw.githubusercontent.com/dirkpetersen/herr-ober/main/install.sh | bash
set -e

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root"
    echo "Usage: curl -fsSL https://raw.githubusercontent.com/dirkpetersen/herr-ober/main/install.sh | sudo bash"
    exit 1
fi

echo "Installing Herr Ober - High-Performance S3 Ingress Controller"
echo "=============================================================="

# Detect package manager
if command -v apt-get &> /dev/null; then
    PKG_MANAGER="apt"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v yum &> /dev/null; then
    PKG_MANAGER="yum"
else
    echo "Error: No supported package manager found (apt, dnf, yum)"
    exit 1
fi

# Install pipx if not present
if ! command -v pipx &> /dev/null; then
    echo "Installing pipx..."
    case $PKG_MANAGER in
        apt)
            apt-get update -qq
            apt-get install -y -qq pipx
            ;;
        dnf)
            dnf install -y -q pipx
            ;;
        yum)
            yum install -y -q pipx
            ;;
    esac
fi

# Install globally, outside /root: the systemd units use ProtectHome=true, so a
# venv under /root would be invisible to HAProxy/ExaBGP. (Env vars work on all
# pipx versions; 'pipx install --global' needs pipx >= 1.5.)
export PIPX_HOME=/opt/pipx
export PIPX_BIN_DIR=/usr/local/bin
export PATH="$PATH:/usr/local/bin"

# Migration from v0.3.0, which installed under root's home (~/.local/pipx).
LEGACY_VENV=/root/.local/pipx/venvs/herr-ober
NEW_VENV=/opt/pipx/venvs/herr-ober

# A stale /usr/local/bin/ober (symlink into the old venv or ~/.local/bin) would make
# pipx refuse to replace it ("File exists ... Not modifying"), keeping the old version.
if [ -e /usr/local/bin/ober ] || [ -L /usr/local/bin/ober ]; then
    OBER_TARGET="$(readlink -f /usr/local/bin/ober || true)"
    case "$OBER_TARGET" in
        /opt/pipx/*) ;;
        *)
            echo "Removing stale /usr/local/bin/ober (points to '${OBER_TARGET:-nowhere}')"
            rm -f /usr/local/bin/ober
            ;;
    esac
fi

LEGACY_NOTICE=0
if [ -d "$LEGACY_VENV/etc" ]; then
    LEGACY_NOTICE=1
fi

# Install herr-ober via pipx
echo "Installing herr-ober..."
if pipx list 2>/dev/null | grep -q "herr-ober"; then
    echo "Upgrading existing installation..."
    pipx upgrade herr-ober
else
    pipx install herr-ober
fi

# Make later 'pipx upgrade herr-ober' in a new root shell find the global install
cat > /etc/profile.d/herr-ober.sh <<'EOF'
# Herr Ober: global pipx location (written by install.sh)
export PIPX_HOME=/opt/pipx
export PIPX_BIN_DIR=/usr/local/bin
EOF
chmod 644 /etc/profile.d/herr-ober.sh

OBER_TARGET="$(readlink -f /usr/local/bin/ober 2>/dev/null || true)"
case "$OBER_TARGET" in
    /opt/pipx/*) ;;
    *)
        echo "Error: /usr/local/bin/ober does not resolve into /opt/pipx (got '${OBER_TARGET:-missing}')"
        echo "Remove it and re-run this installer."
        exit 1
        ;;
esac

if [ "$LEGACY_NOTICE" -eq 1 ]; then
    echo ""
    echo "NOTE: A configuration from a previous install was found at:"
    echo "  $LEGACY_VENV/etc"
    echo "It was NOT moved or deleted. Choose one:"
    echo "  a) Keep using that location:"
    echo "       sudo ober bootstrap $LEGACY_VENV"
    echo "  b) Move to the new location:"
    echo "       sudo cp -a $LEGACY_VENV/etc $NEW_VENV/etc && sudo ober bootstrap"
fi

# Verify installation
echo ""
echo "Installation complete!"
echo "======================"
ober --version
echo ""
echo "Next steps:"
echo "  1. Run 'sudo ober bootstrap' to set up HAProxy and ExaBGP"
echo "  2. Run 'sudo ober config' to configure your cluster"
echo "  3. Run 'sudo ober doctor' to verify prerequisites"
