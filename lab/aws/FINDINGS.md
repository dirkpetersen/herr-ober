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
