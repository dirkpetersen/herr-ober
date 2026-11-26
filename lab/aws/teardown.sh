#!/usr/bin/env bash
# Delete every lab resource that carries the Project=herr-ober-lab tag, in dependency order.
# Safe to run more than once. Only touches resources with the lab tag (and the lab key pair).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

tagged() {  # tagged <describe-command> <query>
    aws ec2 "$1" --filters "Name=tag:Project,Values=$PROJECT" --query "$2" --output text
}

IIDS="$(aws ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ -n "$IIDS" ]; then
    log "Terminating $IIDS"
    # shellcheck disable=SC2086
    aws ec2 terminate-instances --instance-ids $IIDS >/dev/null
    # shellcheck disable=SC2086
    aws ec2 wait instance-terminated --instance-ids $IIDS
fi

for VPC_ID in $(tagged describe-vpcs 'Vpcs[].VpcId'); do
    for sg in $(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" \
        --query "SecurityGroups[?GroupName!='default'].GroupId" --output text); do
        log "Deleting SG $sg"; aws ec2 delete-security-group --group-id "$sg"
    done
    for sn in $(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
        --query 'Subnets[].SubnetId' --output text); do
        log "Deleting subnet $sn"; aws ec2 delete-subnet --subnet-id "$sn"
    done
    for rtb in $(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
        --query 'RouteTables[?!(Associations[?Main])].RouteTableId' --output text); do
        log "Deleting route table $rtb"; aws ec2 delete-route-table --route-table-id "$rtb"
    done
    for igw in $(aws ec2 describe-internet-gateways \
        --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
        --query 'InternetGateways[].InternetGatewayId' --output text); do
        log "Deleting IGW $igw"
        aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$VPC_ID"
        aws ec2 delete-internet-gateway --internet-gateway-id "$igw"
    done
    log "Deleting VPC $VPC_ID"; aws ec2 delete-vpc --vpc-id "$VPC_ID"
done
# An IGW that was created but never attached.
for igw in $(tagged describe-internet-gateways 'InternetGateways[].InternetGatewayId'); do
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw"
done

if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    log "Deleting key pair $KEY_NAME and $KEY_FILE"
    aws ec2 delete-key-pair --key-name "$KEY_NAME"
    rm -f "$KEY_FILE"
fi
rm -f "$LAB_DIR/inventory" "$LAB_DIR/ssh_config" "$LAB_DIR/known_hosts" "$LAB_DIR"/node_key*

LEFT="$(aws resourcegroupstaggingapi get-resources --tag-filters "Key=Project,Values=$PROJECT" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text)"
# The tagging API lists terminated instances for about an hour; ignore those.
log "Done. Tagged ARNs still listed (terminated instances can show for a while): ${LEFT:-none}"
