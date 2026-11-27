#!/usr/bin/env bash
# s3test.sh [command...]: run an AWS CLI command on the helper, through herr-ober (the S3 names
# resolve to the VIP there). Credentials come from this host as env vars over ssh stdin and are
# never written on the helper. Without arguments: the standard data test (GET of an existing
# object, PUT/GET/DELETE under $TEST_PREFIX). Writes and deletes stay under $TEST_PREFIX.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

CREDS="$(aws configure export-credentials --profile "$AWS_PROFILE" --format env)"
if [ $# -gt 0 ]; then
    SCRIPT="$(printf '%q ' "$@")"
else
    SCRIPT="$(cat <<EOF
set -e
B=$BUCKET; P=$TEST_PREFIX
OBJ=\$(aws s3api list-objects-v2 --bucket \$B --max-keys 1 --query 'Contents[0].Key' --output text)
echo "GET s3://\$B/\$OBJ"
time aws s3 cp --only-show-errors "s3://\$B/\$OBJ" /var/tmp/get.bin
ls -l /var/tmp/get.bin
head -c 64M /dev/urandom >/var/tmp/put.bin
time aws s3 cp --only-show-errors /var/tmp/put.bin "s3://\$B/\${P}put.bin"
aws s3 cp --only-show-errors "s3://\$B/\${P}put.bin" /var/tmp/back.bin
cmp /var/tmp/put.bin /var/tmp/back.bin && echo "PUT/GET 64 MiB: identical"
aws s3 rm --only-show-errors "s3://\$B/\${P}put.bin"
aws s3 ls "s3://\$B/\$P" || true
rm -f /var/tmp/get.bin /var/tmp/put.bin /var/tmp/back.bin
EOF
)"
fi
# The CA bundle makes the client trust the lab cert that herr-ober shows for the AWS names.
lab_ssh helper bash -s <<EOF
$CREDS
export AWS_REGION=$AWS_REGION AWS_CA_BUNDLE=\$HOME/lab-ca.pem AWS_PAGER=
$SCRIPT
EOF
