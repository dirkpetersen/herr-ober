# herr-ober AWS lab

A small lab in us-west-2 to test herr-ober on real nodes: 3 ingress nodes (t4g.medium) and one
helper (t4g.small). The helper runs two moto S3 servers as the backends, GoBGP as the upstream
router (ECMP), and the S3 test client. OS: Amazon Linux 2027 preview, arm64. Cost: about
$0.12 per hour while it runs.

Every resource carries the tag `Project=herr-ober-lab`. SSH is open only from the operator's
public IP. VIPs are `10.99.0.100` and `10.99.0.101`, outside the VPC CIDR `10.77.0.0/16`.

## Use

```bash
export AWS_PROFILE=iam-dirk          # default in lib.sh
lab/aws/up.sh                        # create or reconcile, then provision.sh
lab/aws/deploy.sh HEAD               # build a wheel of a git ref, install on the nodes
lab/aws/configure.sh bgp             # bootstrap, ober.yaml + ober.local.yaml, cert, start
lab/aws/configure.sh keepalived      # switch mode (rewrites ober.yaml)
ssh -F lab/aws/ssh_config ober1      # log in
lab/aws/teardown.sh                  # delete everything with the lab tag
```

`up.sh` writes `inventory`, `ssh_config`, `known_hosts` and `node_key` (root on each node uses it
to reach the others as ec2-user, for `ober push`, `cluster status` and `--all-nodes`) here (git-ignored) and the key to
`~/.ssh/herr-ober-lab.pem`. `teardown.sh` removes all of them.

## Known limits

- A VPC ignores gratuitous ARP. In keepalived mode only VRRP state changes can be tested, not
  traffic moving to the new owner.
- GoBGP does not program the kernel. `lab-route-sync` on the helper polls the GoBGP RIB and
  writes ECMP kernel routes. Its JSON parsing is not yet checked against a running GoBGP.
- Amazon Linux 2027 has no pipx and no NetworkManager. `deploy.sh` uses a plain venv at
  `/opt/herr-ober`.
