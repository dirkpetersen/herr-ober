# AWS lab findings (2026-11-25)

Lab: 3 ingress nodes (t4g.medium) and 1 helper, Amazon Linux 2027 preview
(`al2027-preview-ami-2027.0.20261120.0-kernel-7.2-arm64`), SELinux enforcing, us-west-2.
Tested: `main` after the wave 1 merge (0.4.0), then branch `fix/selinux`.
Packages on the nodes: haproxy-awslc 3.4.7, keepalived 2.3.4, ExaBGP 4.2.25 (venv), Python 3.15.

## Bugs found and fixed (branch `fix/selinux`)

1. **ober-http did not start on SELinux hosts.** `NoNewPrivileges=true` blocked the
   `init_t -> haproxy_t` transition (`nnp_transition` denied). As `init_t`, HAProxy could not
   rename its master socket in `/run/ober`. Fix: no `NoNewPrivileges` on ober-http, and
   `setsebool -P haproxy_connect_any 1` (bootstrap and upgrade), so `haproxy_t` may connect to
   RGW on non-http ports. This also affects RHEL 10.
2. **The BGP health state file was never written.** ExaBGP drops to `nobody` when root starts it,
   and the health process runs as `nobody` too. Fix: bootstrap and upgrade give the state dir
   to `nobody`.
3. **Keepalived mode: no node became MASTER.** With `NoNewPrivileges`, keepalived stayed in
   `init_t` and could not open the VRRP raw socket (`errno=13`) or exec `ping`. Fix: no
   `NoNewPrivileges` on ober-ha.
4. **Keepalived mode under `keepalived_t`:** the check script could not connect to the stats
   port, the notify hook could not write the state file, `ping` was denied and the maintenance
   `track_file` could not be watched. Fix: `semanage permissive -a keepalived_t` (no weaker
   than before 0.4, when keepalived ran as `init_t`). Wave 2 (8.6): ship a policy module.
5. **Maintenance did not work in keepalived mode.** keepalived ignored the `track_file`,
   because `<install>/etc` is read-only under `ProtectSystem=strict`. Fix: the flag moved to
   `<install>/var/run/maintenance`, which ober-ha may write.

Lab script bugs (fixed): GoBGP got only one neighbor (ssh split the IP list), and
`lab-route-sync` deleted and re-added the route every second (`/32` vs the kernel's host route).

## Results after the fixes

| Test | Result |
|---|---|
| BGP: 3 sessions, ECMP route via 3 next hops for both VIPs | pass |
| S3 PUT/GET of 50 MB via the VIP, byte-identical | pass |
| Requests spread over all 3 nodes (ECMP, L4 hash) | pass |
| `ober status --check` on every node | OK, exit 0 |
| Graceful `ober stop`: maintenance, withdraw, drain, stop | 2 s; route gone at once |
| `ober start` after stop | back in the route after ~7 s (hold-down + rise) |
| `kill -9` of HAProxy (BGP) | route withdrawn in 1-2 s (BindsTo); back after ~9 s by itself |
| Reboot of a node | services, lo-vip and route back without action |
| Switch to keepalived: lo-vip removed | pass |
| Keepalived: one MASTER per VIP from a shared peer list | pass |
| Keepalived: maintenance moves the VIP, `off` preempts it back | pass |
| Keepalived: `kill -9` of HAProxy moves the VIP | < 4 s; back after restart |

## Not tested / known gaps

- A VPC ignores gratuitous ARP, so in keepalived mode only the VRRP state and the VIP address
  were checked, not client traffic moving to the new owner.
- `ober status --check` is per node: when no node holds a VIP (all BACKUP), each node still
  reports OK. A cluster view (wave 2, 7.6) is needed for that.
- Two independent moto servers do not share state, so the data test used one backend.
- Amazon Linux 2027 is a preview. Ubuntu 26.04 and RHEL 10 were not tested on real nodes.

# AWS lab findings (2026-11-26)

Same lab layout and AMI (`al2027-preview-ami-2027.0.20261120.0-kernel-7.2-arm64`), SELinux
enforcing. Tested: `main` at af23e45 (PRs #14 to #16), then rebased on `main` with #17 to #19,
then branch `wave2/selinux-lab`. HAProxy 3.4.7 (`haproxy-awslc`), keepalived 2.3.4,
ExaBGP 4.2.25, GoBGP 4.11.0. New lab helpers: `configure.sh bgp|keepalived`, and a node key so
root on each node can `ssh 10.77.1.x sudo -n ober ...` as ec2-user (real clusters need the same:
a prompt-free key, `sudo -n ober`, and known host keys, because ober uses `BatchMode=yes`).

## Bugs found and fixed (branch `wave2/selinux-lab`)

1. **`ober apply` crashed** with `UnicodeDecodeError`. With a plain venv the install path is the
   venv, and pip puts ExaBGP's examples (with `.pyc` files) under `etc/exabgp`. Snapshots now
   skip files that are not text.
2. **A failed health gate printed "Rolled back" without a check.** The failed config left
   `/health` down, so the restore skipped its gate ("/health was failing before"). The restore
   now always waits for `/health`.
3. **Keepalived failover took 36 s (PR #18 regression).** `preempt_delay 30` also makes backups
   ignore adverts from a lower-priority master for 30 s, so a node with failed `/health` kept
   its VIP. Now `vrrp_startup_delay 30` (a started node waits 30 s) and `chk_haproxy` `rise 15`
   plus `init_fail`. Measured below.
4. **Watchdog was never configured on Amazon Linux 2027**: there is no
   `/etc/systemd/system.conf`, so bootstrap wrote nothing. Now a drop-in in
   `/etc/systemd/system.conf.d/` (active after reboot: `RuntimeWatchdogUSec=10s`). EC2 has no
   `/dev/watchdog`, so bootstrap warns there.
5. **SELinux (8.6):** the permissive `keepalived_t` is replaced by the `herr_ober` module.
   AVCs collected with the domain permissive (all from `keepalived_t`): `name_connect` to
   `unreserved_port_t` (8404), write/create/rename/unlink/watch in `<install>/var/run`
   (`usr_t`), `dac_override` (the dir belongs to `nobody`), execute `ping_exec_t` and
   `ifconfig_exec_t` plus `process setcap`, `filesystem getattr` on `fs_t`. The module gives
   `<install>/var/run` its own type `ober_state_t` (no write access to `usr_t`), lets
   `haproxy_t` read it (server-state file), and allows the rest. It builds with `checkmodule`
   (from `checkpolicy`, now in the RHEL package list); no `selinux-policy-devel` needed.

Lab script bugs (fixed): `configure.sh` passed the VIP list as several ssh words (only the
first VIP was configured).

## Results

| Test | Result |
|---|---|
| BGP: 3 sessions, ECMP via 3 next hops for both VIPs | pass |
| S3 PUT/GET 50 MB via VIPs .100 and .101, byte-identical (BGP mode, before and after the module) | pass |
| `ober apply` adds VIP 10.99.0.102 on ober1: in GoBGP RIB and kernel route in < 10 s, ExaBGP and HAProxy PIDs unchanged (SIGHUP, PR #14) | pass (after bug 1) |
| `ober apply` removes 10.99.0.102: withdrawn from RIB and kernel | pass |
| Failing apply, bad cert (fails `haproxy -c`): exit 1, haproxy.cfg unchanged, master PID unchanged | pass |
| Failing apply, dead backend (fails `/health`): exit 1 after 15 s, old files back, `/health` 200 | pass (after bug 2) |
| `ober rollback --list`, `ober rollback`: `timeout server` 700 s -> 600 s | pass |
| `ober.local.yaml` (`local_ip`): router-id / local-address per node after push | pass |
| `ober push -y` from ober1 to ober2/ober3 (11 s); refuses an ober.yaml with `local_ip` | pass |
| `ober cluster status`: all OK, exit 0 | pass |
| cluster status, ober3 stopped: CRIT (inactive, not announced), exit 2 | pass |
| cluster status, ober3 unreachable (blackhole route): CRIT unreachable, exit 2 | pass |
| cluster status, keepalived with ober-ha stopped on all nodes: CRIT "not held (VRRP MASTER) on any node" | pass |
| `ober backend drain/ready 10.77.1.10_9001 --all-nodes` | pass, DRAIN/UP on all 3 |
| Drain survives `ober restart` (server-state file, also when labeled `ober_state_t`) | pass |
| `ober metrics` and `--output /var/tmp/ober.prom`; `promtool check metrics` | pass, no findings |
| `ober cert install` (CN/SAN `s3.lab.internal`, `*.s3.lab.internal`): new cert on new connections, master and worker PIDs unchanged, mode 600 | pass |
| `ober cert install --all-nodes`: same on ober2/ober3, no reload | pass |
| `ober cert install`: cert without SAN cover and key mismatch refused, exit 1 | pass |
| `ober doctor` TLS line | "Version 3.4.7, TLS: AWS-LC FIPS 5.12.0" (AL2027 `haproxy-awslc` is AWS-LC, not OpenSSL) |
| `ober stop` (2.2 s, route gone) / `ober start` (back in the route after ~5 s) | pass |
| Reboot ober3 (BGP): services, lo-vip and route back; SELinux enforcing | pass |
| 3.9: `arp_ignore=1`, `arp_announce=2` after reboot | pass (ober3 BGP, ober2 keepalived) |
| 8.7: NIC tuning | ober applies none (no `ethtool` calls, no `.link` files); nothing to persist |
| 8.5: watchdog drop-in | missing before bug 4; now present, `RuntimeWatchdogUSec=10s` after reboot |
| Keepalived, module loaded, `keepalived_t` NOT permissive, enforcing: zero AVC denials on all 3 nodes during all rows below and after a reboot | pass |
| Keepalived: one MASTER per VIP | pass |
| Keepalived: maintenance on moves the VIP (3 s), off brings it back (3 s) | pass |
| Keepalived: `kill -9` of the HAProxy master: VIP leaves at once; back 36 s later (systemd restart + startup delay) | pass |
| Keepalived: `/health` fails (server in maint): VIP moves in 7 s (was 36 s with `preempt_delay 30`); back 34 s after recovery | pass (after bug 3) |
| Keepalived: `chk_gateway` (ping) to a dead IP: FAULT, VIP leaves; good IP: back | pass |
| Keepalived: notify hook writes `vrrp-state.json` (`ober_state_t`) | pass |
| Keepalived: `vips[].owner: 10.77.1.13` (PR #18): ober3 takes .100 within 1 s of the push | pass |
| Keepalived: `ober stop`/`start` of the owner: VIP back after 33 s | pass |
| Keepalived: `systemctl reload ober-ha` (`ober apply`) keeps the VIPs | pass |
| Reboot ober2 (keepalived): VIP back 34 s after keepalived start, no AVCs | pass |
| BGP mode after the module: health process (`unconfined_service_t`) writes `ober-health.json`, routes OK, no AVCs | pass |

## Not tested / known gaps

- Keepalived client traffic after a failover (the VPC ignores gratuitous ARP).
- A real all-BACKUP state with keepalived running (forced only by stopping ober-ha).
- `ober uninstall` on a node (module and label removal is unit-tested only).
- RHEL 10 and Ubuntu 26.04 on real nodes.

# AWS lab findings (2026-11-27, AlmaLinux 10, wave 3)

Lab: 3 ingress nodes (t4g.medium) and 1 helper (t4g.small), AlmaLinux 10.3
(`AlmaLinux OS 10.3.20261116.0 aarch64`, ami-09905381bb1145fc7), SELinux enforcing, us-west-2.
Distro packages: HAProxy 3.0.5 (OpenSSL), keepalived 2.2.8, FRR 10.6.1 (bfdd), Python 3.12.16;
ExaBGP 4.2.25 (venv). Helper: BIRD 3.4.0 (EPEL `bird`; there is no bird2 package on EL10, the
bird2 config syntax works), awscli2 2.33. Backend: AWS S3 `s3.us-west-2.amazonaws.com:443` over
TLS, through an S3 gateway VPC endpoint. Client: AWS CLI on the helper, S3 names mapped to the
VIP in `/etc/hosts`, lab CA as `AWS_CA_BUNDLE`. Tested: `main` with PRs #23 (TLS backends),
#24 (runtime servers, cluster upgrade) and #25 (BFD), then branch `wave3/lab-alma`.

## Bugs found and fixed (branch `wave3/lab-alma`)

1. **TLS backends were DOWN on AlmaLinux 10** ("Layer6 invalid response, SSL handshake
   failure"). HAProxy 3.0.5 loads no CAs from `ca-file @system-ca` there. An empty
   `tls_ca_file` now renders the distro bundle (`/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`,
   else `/etc/ssl/certs/ca-certificates.crt`, else `@system-ca`).
2. **A TLS server added by `ober apply` over the runtime API stayed DOWN** (L7TOUT) until a
   reload: a dynamic server does not get `check-ssl` from `ssl`, so its health check went in
   plain HTTP to port 443. TLS server lines now have an explicit `check-ssl`.
3. **`ober upgrade` broke ExaBGP** when `ober.yaml` has no `venv_path` (written by hand or by
   a tool, here `configure.sh`): it rendered `ober-bgp.service` with
   `<install>/venv/bin/exabgp`, which does not exist. ExaBGP (and so BFD) failed at the next
   start. `ober upgrade` now uses the venv ober runs in when `<install>/venv` is not a venv.
4. **`ober cluster upgrade` stopped after the first node**: it waits for `cluster status` OK,
   but the ober version differs between nodes during a rolling upgrade (WARN). Version drift
   (ober and HAProxy) no longer blocks that wait; config drift still does.
5. **`sudo ober` fails on RHEL**: sudo's `secure_path` has no `/usr/local/bin`, where
   `install.sh` puts `ober`. This also breaks `ober push`, `cluster status`, `--all-nodes` and
   `cluster upgrade` (`ssh NODE sudo -n ober ...`). `install.sh` now links `/usr/bin/ober` when
   needed; `ober uninstall` removes only that link.

Lab notes: BIRD 3.4 misses the kernel update when a route grows from 1 to more paths (the
first node in, then the others: the kernel kept 1 next hop until the 60 s scan). Workaround
`scan time 2`. BIRD also waits about 1 s before it writes a route change to the kernel; with
`export settle time 0 ms 10 ms` on `master4` route removal dropped from ~1 s to ~40 ms.

## Results

| Test | Result |
|---|---|
| `up.sh`/`provision.sh` on AlmaLinux 10 (root device `/dev/sda1`, S3 gateway endpoint) | pass |
| `ober bootstrap` with SELinux enforcing (haproxy, keepalived, policy module, sysctl) | pass, 0 AVC denials |
| BGP: 3 sessions to bird, ECMP route via 3 next hops for both VIPs | pass (with `scan time 2`) |
| TLS backend to AWS S3: `ssl verify required`, SNI, `resolvers sys` (S3 IPs change every few s) | pass after bug 1, L7OK (GET / gives 307) |
| `ober cert install --all-nodes` (lab cert for `s3.us-west-2.amazonaws.com`, `*.s3...`) | pass, no reload |
| S3 GET of an existing 168 MB video through the VIP (helper, AWS CLI, SigV4) | pass, 1.4 s (~120 MB/s, helper limit) |
| S3 PUT/GET/DELETE of 64 MiB under `herr-ober-test/`, byte-identical; prefix empty after | pass |
| Requests spread over all 3 nodes (backend session counters 12 / 27 / 7) | pass |
| Runtime servers: `ober apply` adds an IP TLS server: "Runtime API, no reload", master and worker PIDs unchanged, server UP | pass after bug 2 (before: DOWN L7TOUT) |
| Runtime servers: `ober apply` removes it: runtime `del server`, PIDs unchanged | pass |
| `ober rollback` | pass (reload; restored snapshot) |
| `ober push -y` from ober1 (11 s, both nodes render `inter 2000ms`) | pass |
| `ober cluster status`, `ober metrics` (`ober_bfd_peer_up 1`, `ober_service_active{unit="ober-bfd"} 1`) | pass |
| Old nodes to BFD: `dnf install frr`, `ober upgrade -y` (adds `ober-bfd.service`), `bfd_enabled: true`, `ober apply` | pass, but see bug 3 |
| BFD: bfdd runs as `frr_t`, `/etc/frr/bfdd.conf` is `frr_conf_t`, 0 AVC denials; 3 sessions Up in bird (300 ms x 3) | pass |
| BFD: node loses the router (blackhole route on the node): route removed on the helper | 1.1-1.4 s (BFD) |
| Same without BFD on that node: route removed by BGP hold timer (ExaBGP `hold-time 3`) | 3.2 s |
| `systemctl stop ober-http`: ober-bgp and ober-bfd stop at once (BindsTo), bfdd exits cleanly | pass; route removed after 1.1 s, 40 ms with the settle fix |
| `ober cluster upgrade` with a locally built 0.5.0.dev2 wheel (`--command` pip install) | pass after bug 4, 3 nodes in 94 s |
| Probe from the helper every 0.2 s during the rolling upgrade (452 requests) | 0 failures |
| Reboot ober2: route removed after 1 s, back after 31 s, BGP and BFD up, `ober status` OK | pass, 0 AVC denials |

## Not tested / known gaps

- Keepalived mode on AlmaLinux 10 (keepalived 2.2.8) was not run this time.
- A fresh `ober bootstrap` that installs `frr` itself (the nodes were bootstrapped before
  PR #25 and got `frr` with `dnf`).
- `ober apply` says "No changes." when only `bgp.bfd_enabled` changes, although it starts or
  stops `ober-bfd` correctly.
- When a node turns BFD off, bird (which still has `bfd on`) drops the BGP session once
  ("BFD session down"); it comes back without BFD after the error wait.
- HAProxy 3.0.5 is the only HAProxy on EL10; no HAProxy 3.3 build was tested on Alma.
- The lab cert expires after 30 days, the lab CA after 90.

# 2026-11-27: HAProxy 3.4 on AlmaLinux 10 and Ubuntu 26.04

Same lab (3 x t4g.medium arm64, bird helper with BFD, AWS S3 backend over TLS, BGP mode).
First AlmaLinux 10.3 (HAProxy 3.0.5 rolled to 3.4.8), then the nodes replaced with Ubuntu 26.04
(`NODE_OS=ubuntu`, fresh bootstrap).

| Test | AlmaLinux 10.3 | Ubuntu 26.04 |
|---|---|---|
| HAProxy source | `deploy/el10-haproxy/build-rpm.sh` on the helper: Fedora 3.4.8 SRPM, ~6 min on t4g.small | `ppa:vbernat/haproxy-3.4` (3.4.9), added by `ober bootstrap` |
| TLS library (`ober doctor`) | OpenSSL 3.5.9 | OpenSSL 3.5.5 |
| Install | `ober cluster upgrade --command "dnf install -y <rpm>"` from 3.0.5, 95 s | fresh `ober bootstrap` (also installed `frr` itself, first time tested) |
| Probe every 0.2 s during the rolling upgrade | 479 / 479 OK | 430 / 430 OK (`--reinstall haproxy`, 75 s) |
| SELinux | 0 AVC denials (the RPM keeps `/usr/sbin/haproxy`, `haproxy_exec_t`) | n/a |
| `haproxy -c`, cluster status, BFD peers up, 3-way ECMP | pass | pass |
| S3 GET 168 MB video / PUT+GET 64 MiB under `herr-ober-test/` | 1.2 s / identical | 1.5 s / identical |
| Runtime add + del of a TLS server (`ober apply`) | no reload, UP L7OK, same master PID | same |
| BFD: node loses the router | route removed after 1.5 s | 1.0 s |
| Reboot ober2 | not repeated | services up, route back to 3 paths, `ober status` OK |
| Distro `haproxy`/`frr` units after the package (re)install | stay disabled | stay disabled |

Findings:

1. The 3.4 PPA builds only for 26.04 (resolute). Adding it on 24.04 makes `apt-get update` fail
   ("does not have a Release file"), so bootstrap keeps the 3.3 PPA on noble.
2. Fedora's spec puts halog/iprange in `%{_bindir}` (Fedora merged `/usr/sbin`); on EL10 they are
   in `%{_sbindir}`. `build-rpm.sh` fixes the three `%files` lines; nothing else changes.
3. `ausearch` without a terminal reads stdin and hangs; use `ausearch --input-logs`.
4. CI (GitHub `ubuntu-26.04`, an amd64v3 runner) installed the distro HAProxy 3.2.25 although the
   3.4 PPA was added: apt reads only the PPA's amd64v3 index, and the PPA builds no amd64v3
   haproxy. Fixed in bootstrap (`Architectures:` in the PPA `.sources`) and in CI. The arm64 lab
   nodes were not affected.
