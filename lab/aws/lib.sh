# Shared settings for the herr-ober lab scripts. Sourced, not executed.
# shellcheck shell=bash

export AWS_PROFILE="${AWS_PROFILE:-iam-dirk}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="herr-ober-lab"
AZ="${AZ:-us-west-2a}"
VPC_CIDR="10.77.0.0/16"
SUBNET_CIDR="10.77.1.0/24"
KEY_NAME="herr-ober-lab"
KEY_FILE="$HOME/.ssh/herr-ober-lab.pem"
SSH_CONFIG="$LAB_DIR/ssh_config"
KNOWN_HOSTS="$LAB_DIR/known_hosts"

# Newest official AlmaLinux 10 arm64 AMI (owner: AlmaLinux OS Foundation). Login: ec2-user.
AMI_OWNER="764336703387"
AMI_PATTERN="AlmaLinux OS 10.* aarch64"

# AWS S3 is the backend. Tests write and delete only under BUCKET/TEST_PREFIX.
BUCKET="hcic-townhall"
TEST_PREFIX="herr-ober-test/"
S3_HOST="s3.$AWS_REGION.amazonaws.com"

# name:type:private-ip:root-GB
NODES=(
    "ober1:t4g.medium:10.77.1.11:10"
    "ober2:t4g.medium:10.77.1.12:10"
    "ober3:t4g.medium:10.77.1.13:10"
    "helper:t4g.small:10.77.1.10:12"
)
INGRESS_NODES=(ober1 ober2 ober3)
HELPER_IP="10.77.1.10"

# VIPs live outside the VPC CIDR; the helper (bird2) routes them to the nodes via BGP.
VIPS=(10.99.0.100 10.99.0.101)

TAGS="{Key=Project,Value=$PROJECT}"

log() { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }

# Print the id of the first resource that carries the Project tag, or nothing.
none_to_empty() { local v; v="$(cat)"; [ "$v" = "None" ] && v=""; printf '%s' "$v"; }

lab_ssh() {
    # lab_ssh <host> <command...>
    local host="$1"; shift
    ssh -F "$SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$host" "$@"
}

lab_scp() {
    scp -F "$SSH_CONFIG" -o BatchMode=yes -q "$@"
}
