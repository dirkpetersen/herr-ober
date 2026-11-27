# herr-ober AWS lab

A small lab in us-west-2 to test herr-ober on real nodes: 3 ingress nodes (t4g.medium) and one
helper (t4g.small). OS: AlmaLinux 10 (newest official arm64 AMI, owner 764336703387), SELinux
enforcing, login `ec2-user`. Cost: about $0.12 per hour while it runs.

- **Backend:** AWS S3 itself (`s3.us-west-2.amazonaws.com:443`, TLS), bucket `hcic-townhall`.
  The nodes reach S3 through an S3 gateway VPC endpoint (private, no charge).
- **Helper:** bird (EPEL, BIRD 3.4 with bird2 config syntax) as the upstream router: one BGP
  session per node with `bfd on`, ECMP into the kernel. Also the S3 client (`awscli2`). Its
  `/etc/hosts` maps `s3.us-west-2.amazonaws.com` and `hcic-townhall.s3.us-west-2.amazonaws.com`
  to the VIP `10.99.0.100`, because SigV4 signs the host name.
- **Cert:** `configure.sh` makes a lab CA and a server cert for `s3.us-west-2.amazonaws.com`
  and `*.s3.us-west-2.amazonaws.com` (`lab-ca.pem`, `server.pem`, git-ignored), installs the
  cert with `ober cert install --all-nodes`, and copies the CA to the helper (`~/lab-ca.pem`,
  used as `AWS_CA_BUNDLE`).

Every resource carries the tag `Project=herr-ober-lab`. SSH is open only from the operator's
public IP. VIPs are `10.99.0.100` and `10.99.0.101`, outside the VPC CIDR `10.77.0.0/16`.

## Use

```bash
export AWS_PROFILE=iam-dirk          # default in lib.sh
lab/aws/up.sh                        # create or reconcile, then provision.sh
lab/aws/deploy.sh HEAD               # build a wheel of a git ref, install on the nodes
lab/aws/configure.sh bgp             # bootstrap, ober.yaml + ober.local.yaml, cert, start
lab/aws/configure.sh keepalived      # switch mode (rewrites ober.yaml)
lab/aws/s3test.sh                    # GET an object, PUT/GET/DELETE under herr-ober-test/
lab/aws/s3test.sh aws s3 ls s3://hcic-townhall/   # any AWS CLI command, through the VIP
ssh -F lab/aws/ssh_config ober1      # log in
lab/aws/teardown.sh                  # delete everything with the lab tag
```

`s3test.sh` passes the credentials of this host (`aws configure export-credentials`) as env
vars over ssh stdin; nothing is written on the helper. **Tests write and delete only under
`herr-ober-test/`.** The other objects (80-300 MB videos) are read-only test data.

`up.sh` writes `inventory`, `ssh_config`, `known_hosts` and `node_key` (root on each node uses it
to reach the others as ec2-user, for `ober push`, `cluster status` and `--all-nodes`) here
(git-ignored) and the key to `~/.ssh/herr-ober-lab.pem`. `teardown.sh` removes all of them.

## Known limits

- A VPC ignores gratuitous ARP. In keepalived mode only VRRP state changes can be tested, not
  traffic moving to the new owner.
- BIRD 3.4 misses the kernel update when a route grows from one path to more. The kernel
  protocol scans every 2 s (`scan time 2`), which repairs it. `export settle time 0 ms 10 ms`
  on the table removes BIRD's default 1 s delay before routes reach the kernel.
- AlmaLinux 10 ships HAProxy 3.0.5 and keepalived 2.2.8 (no 3.4 package); `ober doctor` warns. `deploy/el10-haproxy/build-rpm.sh` builds 3.4.
- `deploy.sh` uses a plain venv at `/opt/herr-ober` (not pipx) and links `ober` into
  `/usr/local/bin` and `/usr/bin` (RHEL sudo has no `/usr/local/bin` in `secure_path`).
