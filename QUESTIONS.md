# Open questions

Work continues on the default answer for each question below. To change a decision, edit the
answer here or say so in the session.

## Process

- **Where do the fixes land?** Default: each area gets a local branch (`fix/<area>`), and the
  branches are merged into the local integration branch `improve/ingress-hardening`. Nothing is
  pushed until you say so. `gh` is not installed, so pull requests can't be opened from here.
- **AWS lab lifetime.** Default: a 3-node lab plus one helper node (S3 backend and BGP peer)
  in us-west-2, using the `iam-dirk` profile. It keeps running while the loop is active, and
  `~/herr-ober-lab/teardown.sh` deletes it. The cost is roughly $0.12 an hour (t4g instances).
- **AWS lab OS.** Default: Amazon Linux 2027 *preview* on arm64 (`al2027-preview-ami-*`,
  kernel 7.2). There is no non-preview AL2027 image yet. Amazon Linux reports `ID=amzn`,
  `ID_LIKE=fedora`, so ober gets an Amazon Linux branch in the RHEL family.
- **Should the AWS lab scripts be in the repo?** Default: no. They live in `~/herr-ober-lab/`.

## Fix decisions (wave 1)

- **1.4 Let's Encrypt via Route53:** implement ACME or hide the option? Default: hide the
  option in the wizard, and make "no S3 frontend generated" an error.
- **3.1 BindsTo:** default: keep `BindsTo=` (HA stops when HAProxy stops), and also install
  the HA unit as `WantedBy=ober-http.service`, so it starts again when HAProxy restarts.
- **9.1 ExaBGP pin:** default: pin to the 4.2.x series, which is long-term stable and packaged
  by the distributions. The generated config is validated against both 4.2 and 5.0.
- **4.5 `ober sync`:** remove it, fix it, or deprecate it? Default: the agent decides after
  checking what the files were meant for, and records the choice here.
- **8.1 Firewall:** default: opt-in at bootstrap. SSH is always allowed, the VRRP rule is
  persisted, and port 80 is no longer opened.
- **3.4 Maintenance mode:** default: `ober maintenance on|off` writes a flag file. While it is
  set, health reports down, so the node withdraws its BGP routes or drops its VRRP priority but
  keeps serving existing connections.
