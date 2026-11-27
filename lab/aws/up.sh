#!/usr/bin/env bash
# Create (or reconcile) the herr-ober lab: VPC, subnet, IGW, S3 endpoint, SG, key pair,
# 4 instances.
# Idempotent: every resource is looked up by the Project=herr-ober-lab tag first.
# Afterwards it writes ./inventory and ./ssh_config and runs ./provision.sh.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

MYIP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
log "Operator public IP: $MYIP"

# ---- VPC ----------------------------------------------------------------------------
VPC_ID="$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'Vpcs[0].VpcId' --output text | none_to_empty)"
if [ -z "$VPC_ID" ]; then
    VPC_ID="$(aws ec2 create-vpc --cidr-block "$VPC_CIDR" \
        --tag-specifications "ResourceType=vpc,Tags=[$TAGS,{Key=Name,Value=$PROJECT}]" \
        --query 'Vpc.VpcId' --output text)"
    aws ec2 wait vpc-available --vpc-ids "$VPC_ID"
    aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'
    log "Created VPC $VPC_ID"
else
    log "VPC $VPC_ID exists"
fi

# ---- Internet gateway ---------------------------------------------------------------
IGW_ID="$(aws ec2 describe-internet-gateways --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'InternetGateways[0].InternetGatewayId' --output text | none_to_empty)"
if [ -z "$IGW_ID" ]; then
    IGW_ID="$(aws ec2 create-internet-gateway \
        --tag-specifications "ResourceType=internet-gateway,Tags=[$TAGS,{Key=Name,Value=$PROJECT}]" \
        --query 'InternetGateway.InternetGatewayId' --output text)"
    log "Created IGW $IGW_ID"
fi
ATTACHED="$(aws ec2 describe-internet-gateways --internet-gateway-ids "$IGW_ID" \
    --query 'InternetGateways[0].Attachments[0].VpcId' --output text | none_to_empty)"
if [ "$ATTACHED" != "$VPC_ID" ]; then
    aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
    log "Attached IGW to VPC"
fi

# ---- Subnet -------------------------------------------------------------------------
SUBNET_ID="$(aws ec2 describe-subnets --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'Subnets[0].SubnetId' --output text | none_to_empty)"
if [ -z "$SUBNET_ID" ]; then
    SUBNET_ID="$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$SUBNET_CIDR" \
        --availability-zone "$AZ" \
        --tag-specifications "ResourceType=subnet,Tags=[$TAGS,{Key=Name,Value=$PROJECT-public}]" \
        --query 'Subnet.SubnetId' --output text)"
    aws ec2 modify-subnet-attribute --subnet-id "$SUBNET_ID" --map-public-ip-on-launch
    log "Created subnet $SUBNET_ID"
fi

# ---- Route table --------------------------------------------------------------------
RTB_ID="$(aws ec2 describe-route-tables --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'RouteTables[0].RouteTableId' --output text | none_to_empty)"
if [ -z "$RTB_ID" ]; then
    RTB_ID="$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
        --tag-specifications "ResourceType=route-table,Tags=[$TAGS,{Key=Name,Value=$PROJECT}]" \
        --query 'RouteTable.RouteTableId' --output text)"
    log "Created route table $RTB_ID"
fi
aws ec2 create-route --route-table-id "$RTB_ID" --destination-cidr-block 0.0.0.0/0 \
    --gateway-id "$IGW_ID" >/dev/null 2>&1 || true
ASSOC="$(aws ec2 describe-route-tables --route-table-ids "$RTB_ID" \
    --query "RouteTables[0].Associations[?SubnetId=='$SUBNET_ID'].RouteTableAssociationId" \
    --output text | none_to_empty)"
if [ -z "$ASSOC" ]; then
    aws ec2 associate-route-table --route-table-id "$RTB_ID" --subnet-id "$SUBNET_ID" >/dev/null
fi

# ---- S3 gateway endpoint: S3 traffic from the nodes stays private and costs nothing --
VPCE_ID="$(aws ec2 describe-vpc-endpoints --filters "Name=tag:Project,Values=$PROJECT" \
    "Name=vpc-id,Values=$VPC_ID" "Name=vpc-endpoint-state,Values=available,pending" \
    --query 'VpcEndpoints[0].VpcEndpointId' --output text | none_to_empty)"
if [ -z "$VPCE_ID" ]; then
    VPCE_ID="$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Gateway \
        --service-name "com.amazonaws.$AWS_REGION.s3" --route-table-ids "$RTB_ID" \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[$TAGS,{Key=Name,Value=$PROJECT-s3}]" \
        --query 'VpcEndpoint.VpcEndpointId' --output text)"
    log "Created S3 gateway endpoint $VPCE_ID"
fi

# ---- Security group -----------------------------------------------------------------
SG_ID="$(aws ec2 describe-security-groups --filters "Name=tag:Project,Values=$PROJECT" \
    "Name=vpc-id,Values=$VPC_ID" --query 'SecurityGroups[0].GroupId' --output text | none_to_empty)"
if [ -z "$SG_ID" ]; then
    SG_ID="$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "$PROJECT" \
        --description "herr-ober lab: SSH from operator, all traffic inside the group" \
        --tag-specifications "ResourceType=security-group,Tags=[$TAGS,{Key=Name,Value=$PROJECT}]" \
        --query 'GroupId' --output text)"
    log "Created SG $SG_ID"
fi
# All traffic between members (VRRP 112, BGP 179, S3, syslog, ...).
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --ip-permissions "IpProtocol=-1,UserIdGroupPairs=[{GroupId=$SG_ID,Description=intra-lab}]" \
    >/dev/null 2>&1 || true
# SSH only from this machine; drop stale operator IPs.
for cidr in $(aws ec2 describe-security-groups --group-ids "$SG_ID" \
    --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[].CidrIp" --output text); do
    if [ "$cidr" != "$MYIP/32" ]; then
        aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 \
            --cidr "$cidr" >/dev/null
        log "Revoked stale SSH source $cidr"
    fi
done
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MYIP/32,Description=operator-ssh}]" \
    >/dev/null 2>&1 || true

# ---- Key pair -----------------------------------------------------------------------
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    [ -e "$KEY_FILE" ] && { echo "Refusing to overwrite $KEY_FILE" >&2; exit 1; }
    umask 077
    aws ec2 create-key-pair --key-name "$KEY_NAME" --key-type ed25519 \
        --tag-specifications "ResourceType=key-pair,Tags=[$TAGS]" \
        --query 'KeyMaterial' --output text >"$KEY_FILE"
    chmod 600 "$KEY_FILE"
    log "Created key pair $KEY_NAME -> $KEY_FILE"
elif [ ! -f "$KEY_FILE" ]; then
    echo "Key pair $KEY_NAME exists in AWS but $KEY_FILE is missing" >&2
    exit 1
fi

# ---- AMI ----------------------------------------------------------------------------
AMI_ID="${AMI_ID:-$(aws ec2 describe-images --owners "$AMI_OWNER" \
    --filters "Name=name,Values=$AMI_PATTERN" "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)}"
ami_q() { aws ec2 describe-images --image-ids "$AMI_ID" --query "Images[0].$1" --output text; }
ROOT_DEV="$(ami_q RootDeviceName)"
log "AMI $AMI_ID ($(ami_q Name), root $ROOT_DEV)"

# ---- Instances ----------------------------------------------------------------------
instance_id() {
    aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" "Name=tag:Name,Values=$1" \
        "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[0].Instances[0].InstanceId' --output text | none_to_empty
}

for spec in "${NODES[@]}"; do
    IFS=: read -r name itype ip disk <<<"$spec"
    iid="$(instance_id "$name")"
    if [ -z "$iid" ]; then
        userdata="$(printf '#cloud-config\nhostname: %s\nfqdn: %s.lab.internal\npreserve_hostname: false\n' "$name" "$name")"
        iid="$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type "$itype" \
            --key-name "$KEY_NAME" --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
            --private-ip-address "$ip" --associate-public-ip-address \
            --metadata-options 'HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1' \
            --credit-specification CpuCredits=standard \
            --block-device-mappings "DeviceName=$ROOT_DEV,Ebs={VolumeSize=$disk,VolumeType=gp3,DeleteOnTermination=true}" \
            --user-data "$userdata" \
            --tag-specifications \
                "ResourceType=instance,Tags=[$TAGS,{Key=Name,Value=$name}]" \
                "ResourceType=volume,Tags=[$TAGS,{Key=Name,Value=$name}]" \
                "ResourceType=network-interface,Tags=[$TAGS,{Key=Name,Value=$name}]" \
            --query 'Instances[0].InstanceId' --output text)"
        log "Launched $name $iid"
    else
        state="$(aws ec2 describe-instances --instance-ids "$iid" \
            --query 'Reservations[0].Instances[0].State.Name' --output text)"
        if [ "$state" = "stopped" ]; then
            aws ec2 start-instances --instance-ids "$iid" >/dev/null
            log "Started $name $iid"
        else
            log "$name $iid is $state"
        fi
    fi
done

ALL_IDS=()
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ <<<"$spec"
    ALL_IDS+=("$(instance_id "$name")")
done
aws ec2 wait instance-running --instance-ids "${ALL_IDS[@]}"

# Ingress nodes receive and send packets for VIPs that are not their ENI address.
for name in "${INGRESS_NODES[@]}"; do
    aws ec2 modify-instance-attribute --instance-id "$(instance_id "$name")" \
        --no-source-dest-check
done
# The helper routes VIP traffic it learned via BGP; its own ENI only sends with its own IP,
# but disabling the check there too keeps asymmetric return paths working.
aws ec2 modify-instance-attribute --instance-id "$(instance_id helper)" --no-source-dest-check

# ---- Inventory + ssh_config ---------------------------------------------------------
{
    echo "# herr-ober lab inventory, written by up.sh on $(date -u +%FT%TZ)"
    echo "# region=$AWS_REGION az=$AZ vpc=$VPC_ID subnet=$SUBNET_ID sg=$SG_ID igw=$IGW_ID rtb=$RTB_ID"
    echo "# ami=$AMI_ID key=$KEY_NAME ($KEY_FILE)"
    printf '%-8s %-21s %-11s %-13s %-15s %s\n' name instance-id type private-ip public-ip role
} >"$LAB_DIR/inventory"
{
    echo "# Generated by up.sh. Use: ssh -F $SSH_CONFIG ober1"
    echo "Host *"
    echo "    User ec2-user"
    echo "    IdentityFile $KEY_FILE"
    echo "    IdentitiesOnly yes"
    echo "    UserKnownHostsFile $KNOWN_HOSTS"
    echo "    StrictHostKeyChecking accept-new"
    echo "    ServerAliveInterval 15"
} >"$SSH_CONFIG"
for spec in "${NODES[@]}"; do
    IFS=: read -r name itype ip _ <<<"$spec"
    iid="$(instance_id "$name")"
    pub="$(aws ec2 describe-instances --instance-ids "$iid" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
    role="ingress"; [ "$name" = helper ] && role="bird2-router+s3-client"
    printf '%-8s %-21s %-11s %-13s %-15s %s\n' "$name" "$iid" "$itype" "$ip" "$pub" "$role" \
        >>"$LAB_DIR/inventory"
    printf 'Host %s\n    HostName %s\n' "$name" "$pub" >>"$SSH_CONFIG"
done
cat "$LAB_DIR/inventory"

# ---- Wait for SSH, then provision ---------------------------------------------------
for spec in "${NODES[@]}"; do
    IFS=: read -r name _ <<<"$spec"
    for _ in $(seq 1 60); do
        lab_ssh "$name" true 2>/dev/null && break
        sleep 5
    done
    lab_ssh "$name" true || { echo "SSH to $name failed" >&2; exit 1; }
done
log "All instances reachable over SSH"

if [ "${SKIP_PROVISION:-0}" != 1 ]; then
    "$LAB_DIR/provision.sh"
fi
