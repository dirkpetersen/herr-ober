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
