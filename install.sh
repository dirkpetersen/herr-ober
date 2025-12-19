#!/bin/bash
# Herr Ober installer - https://github.com/dirkpetersen/ober
# Usage: curl -fsSL https://raw.githubusercontent.com/dirkpetersen/ober/main/install.sh | bash
set -e

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root"
    echo "Usage: curl -fsSL https://raw.githubusercontent.com/dirkpetersen/ober/main/install.sh | sudo bash"
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

# Ensure pipx path is set up
export PATH="$PATH:/root/.local/bin"
pipx ensurepath &> /dev/null || true

# Install herr-ober via pipx
echo "Installing herr-ober..."
if pipx list 2>/dev/null | grep -q "herr-ober"; then
    echo "Upgrading existing installation..."
    pipx upgrade herr-ober
else
    pipx install herr-ober
fi

# Create symlink in /usr/local/bin
OBER_BIN="/root/.local/bin/ober"
SYMLINK="/usr/local/bin/ober"

if [ -f "$OBER_BIN" ]; then
    echo "Creating symlink at $SYMLINK..."
    ln -sf "$OBER_BIN" "$SYMLINK"
else
    echo "Error: ober binary not found at $OBER_BIN"
    exit 1
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
