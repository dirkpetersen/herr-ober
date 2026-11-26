# Open questions

Work continues on the default answer for each question below. To change a decision, edit the
answer here or say so in the session.

## Process

- **Where do the fixes land?** Default: each area gets a local branch (`fix/<area>`), and the
  branches are merged into the local integration branch `improve/ingress-hardening`. Nothing is
  pushed until you say so. `gh` is not installed, so pull requests can't be opened from here.
- **AWS lab lifetime.** Default: a 3-node lab plus one helper node (S3 backend and BGP peer)
  in us-west-2, using the `iam-dirk` profile. It keeps running while the loop is active, and
  `lab/aws/teardown.sh` (still to be written) deletes it by tag. The cost is roughly $0.12 an hour (t4g instances).
- **AWS lab OS.** Default: Amazon Linux 2027 *preview* on arm64 (`al2027-preview-ami-*`,
  kernel 7.2). There is no non-preview AL2027 image yet. Amazon Linux reports `ID=amzn`,
  `ID_LIKE=fedora`, so ober gets an Amazon Linux branch in the RHEL family.
- **Should the AWS lab scripts be in the repo?** Default: yes, under `lab/aws/`, so they move with
  the work to the new server. Remove them before merging to `main` if you don't want them there.

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

## Decisions made during wave 1 (2026-11-24)

These were not in the list above. Each one is the default the work used.

- **4.5 `ober sync`:** deprecated, not removed. Its three files were never read. Each list now
  has its own field in ober.yaml (`bgp.neighbors` / `keepalived.peers`,
  `security.internal_networks` / `security.allowlist`, `backends[].servers`). The command is
  hidden, prints where each list now lives, and exits 1. The name stays free for 4.4.
- **Peers must be IP addresses.** `keepalived.peers` and `bgp.neighbors` are validated as IPs,
  because keepalived `unicast_peer` needs IPs. A config with hostnames there is now refused by
  `config`, `start` and `test` (`stop`, `status`, `logs` still work).
- **Unknown keys in ober.yaml** are warnings. Duplicate keys are errors.
- **Plain `ober restart`** reloads HAProxy (and HUPs keepalived) but does not restart ExaBGP,
  so the BGP session stays up. ExaBGP config changes need `ober restart --hard`, which also
  drains first.
- **Existing nodes** get the new units through `ober upgrade`, which re-renders them. Then one
  `ober restart --hard` per node is needed to move HAProxy to `Type=notify`.
- **Keepalived script security:** on (`enable_script_security`, `script_user root`). Without it
  `keepalived -t` fails. Side effect: keepalived refuses the check scripts if the venv path is
  writable by a non-root user (an install under /home). The recommended pipx install under
  /opt/pipx is fine. Follow-up: doctor should warn about this case.
- **Maintenance:** keepalived `track_file` weight -100, so the owner drops to priority 1 and the
  peers (90) win. If every node is in maintenance, the VIP stays on one node and keeps serving.
  The wait for "out of rotation" in `ober stop` is a fixed 15 s; on timeout it warns and goes on.
- **Stats listener:** bound to 127.0.0.1 plus the addresses in the new `stats_bind` field.
  Remote Prometheus scraping of :8404 needs `stats_bind: [<mgmt ip>]`. Basic auth was not
  added (the password would sit in haproxy.cfg and travel over plain HTTP).
- **Several backend groups** are now an error. Before, only the first was used.
- **BFD:** the ExaBGP `bfd {}` block is no longer written. `bgp.bfd_enabled` stays as an unused
  field so old files load. Failure detection is the BGP hold time (3 s).
- **Conntrack:** limits raised (`nf_conntrack_max` 1048576), no `notrack` rules.
- **Firewall:** the VRRP rule allows any source (peers are unknown at bootstrap). `--firewall`
  does not open 179/tcp, the stats port or the watch ports.
- **`ober status --check`** exits 0/1/2 (OK/WARN/CRIT), an intended exception to the 0/1 rule.
  A route that is not announced is WARN, not CRIT, because it can be maintenance.
- **ECMP and drain:** without resilient hashing on the router, a withdraw still resets
  in-flight flows, and a VRRP move does the same in keepalived mode. The drain only protects
  connections that still reach the node. The default `drain_timeout` stays 30 s (wave 2: revisit
  with resilient hashing on the router, and count old workers after a reload).

## Decisions made during wave 2 (2026-11-25)

- **Server names (5.1)** come from the address: `rgw1:7480` becomes `rgw1_7480`. Metrics and
  alerts that use the old `srv1`, `srv2` names must be updated once.
- **Backend tuning (5.3):** `fall` 3 and `rise` 2 (the HAProxy defaults), `slowstart_s` 10
  (new: a recovered RGW gets traffic over 10 s), `maxconn` 0 (no limit). With `maxconn`, extra
  requests wait in the queue for `timeout connect` (5 s). `observe layer4` was not added.
- **Drain (5.2):** `ober backend drain|maint|ready` acts on this node only. `--all-nodes` waits
  for `ober push` (4.4), which needs the same SSH channel. The state goes to
  `<install>/var/run/haproxy-server-state` after each change and before each `ober restart`
  or `ober stop`, so a drain survives a reload, restart and reboot. A plain
  `systemctl reload ober-http` does not save it first.
- **PPA (9.4):** when `ppa:vbernat/haproxy-3.3` cannot be added, bootstrap warns and uses the
  distro HAProxy instead of failing (Ubuntu 26.04 has no PPA build but ships 3.2).
- **Apply (2.2, 2.5):** `ober apply` is a new command; `ober config --non-interactive` plus
  `ober restart` still work. A failed apply keeps the edit in `ober.yaml` (so it can be fixed and
  applied again) and puts back the generated files. Snapshots hold the applied state, 20 are
  kept. Not covered: certificates, the blocklist and the `lo-vip` network files. The health gate
  is skipped when `/health` was failing before the apply, because it could never pass. An
  ExaBGP config change still needs `ober restart --hard` (only a message, no automatic restart).
- **Cluster file and push (4.1, 4.4).** No new file format: `ober.yaml` is the cluster file,
  and `ober.local.yaml` on each node holds the values that differ (`local_ip`,
  `bgp.router_id`, `bgp.local_address`, or anything else). No migration is needed. `ober push`
  sends the file over SSH (stdin to `ober apply -f -`), so it needs no knowledge of the remote
  install path, and each node runs the same check, health gate and restore as a local apply.
  Certificates are not pushed (keys stay where they are made; see 6.2). Drift shows in
  `ober cluster status` (hash of `haproxy.cfg`), not yet as a metric (7.4).
- **Issue auto-fix (10.5):** the workflow runs only on the label `auto-fix` added by a user
  with write, maintain or admin permission (default label name `auto-fix`). Auto-merge uses
  `gh pr merge --auto` and is skipped when `main` has no branch protection, so the owner must
  require the test checks and one review on `main`. The README one-liner uses a release tag
  (`v0.4.0`, which must exist before the README is published). `install.sh` still installs the
  newest herr-ober from PyPI: pinning it would put the version in a third file.
