# Herr Ober: day-2 operability review of the ingress cluster

Repo: `herr-ober` at `1a90bc4` (v0.3.1). Reviewed for a small ops team that runs 3+ HAProxy
ingress VMs on Proxmox in front of Ceph RGW. The main question was whether the cluster is easy to
manage. Performance and reliability were judged by how they affect day-2 operations.

**Evidence tags used below**

- **[V]**: verified by running code or the real binaries (commands are in the appendix).
- **[R]**: verified by reading code. The behavior is deterministic from the cited lines.
- **[I]**: inferred, not executed. Usually it needs root or a real cluster. Confidence is stated.

**Test environment.** Ubuntu 26.04, Python 3.14. The repo ran from a snapshot copy, so the working
tree was not touched. Binaries used:

- HAProxy 3.2.25, the Ubuntu package, extracted without installing;
- ExaBGP 5.0.15 (what `pip install exabgp` gives today) and ExaBGP 4.2.25;
- keepalived 2.3.4;
- netplan from Ubuntu 26.04;
- systemd 259.

The test suite gives 495 passed and 41 skipped. With a real HAProxy on `PATH`, the live HAProxy
tests give 74 passed and 1 failed. That failure is local only: the temp-dir path was too long for
a unix socket.

**Priority:** P0 means fix before the next production rollout (it causes an outage, a lockout or
silent loss of HA). P1 is needed for comfortable day-2 operation. P2 is an improvement.

**Effort:** S is up to about 1 day, M is 2 to 5 days, L is more than a week.

---

## Executive summary: top 5

1. **Several generated configs and bootstrap steps break real deployments, and CI can't catch it
   (P0, mostly S).** All four config problems were run against the real binaries:
   - Two or more VIPs with the same exposure produce an invalid HAProxy `bind` line, so HAProxy
     refuses to start. This is exactly the keepalived topology in the README (3 VIPs).
   - The default BGP config (`bfd_enabled: true`) contains a `bfd {}` block that ExaBGP 4.2 and
     5.0 both reject, so `ober-bgp` can't start.
   - The netplan file for the BGP VIP interface (`tunnels: mode: dummy`) is rejected by netplan, so
     on Ubuntu the VIPs are never configured locally.
   - Choosing "Let's Encrypt via Route53" in the wizard generates no S3 frontend at all. ACME
     issuance is not implemented anywhere.

   Bootstrap also runs `ufw --force enable` and opens only 80 and 443, which on stock Ubuntu blocks
   new SSH sessions [R plus the stock ufw defaults, effect I]. CI installs no HAProxy, ExaBGP or
   keepalived, and one test asserts the invalid `bfd {` block. *Fix the bugs and add golden-config
   validation (`haproxy -c`, `exabgp validate`, `keepalived -t`, `netplan generate`) in CI and in
   `ober test`.*

2. **Changes can't be applied safely (P0, S to M).** `ober-http` runs HAProxy with `-D` and
   reloads with `kill -USR2`. On a real HAProxy that signal is a no-op: the process keeps serving
   the old config [V]. So these all print success and change nothing:
   - `ober restart --reload-only`;
   - the reload in `ober sync`;
   - the "zero-downtime reload" in the docs.

   The only way to apply a change is a full restart. It isn't validated, drops every connection on
   the node and withdraws BGP. If a reload fails, the code falls back to a hard restart, which
   turns a bad config into a dead node. In `-D` mode HAProxy's `log stdout` goes to `/dev/null`
   [V], so with the default settings there are no HAProxy logs at all, not even "server DOWN"
   events. *Use the master-worker unit that the Ubuntu package already ships (`-Ws`,
   `Type=notify`, `haproxy -c` before every reload). This was verified to reload and to survive a
   bad config. Add a non-interactive `ober apply`: render, validate, atomic swap, reload, health
   gate, roll back on failure.*

3. **Nodes silently drop out of rotation, or stay in it while broken (P0, S).**
   - After an HAProxy crash and systemd's auto-restart, `ober-bgp` or `ober-ha` stays stopped
     forever because of `BindsTo=` [V, reproduced with the same unit directives]. One adjustment
     fixes it [V]: `WantedBy=ober-http.service` on the HA unit, or `Upholds=` on `ober-http`.
   - The health signal used by both HA modes is HAProxy's `monitor-uri`. It returns **200 while
     every RGW backend is DOWN** [V], so a node with a broken back-side VF keeps attracting traffic
     and answers 503. Adding `monitor fail if { nbsrv(<backend>) lt 1 }` fixes this [V].
   - The BGP health loop has no hysteresis (one slow probe withdraws the route), and there is no
     maintenance mode.

4. **No single source of truth, and no non-interactive configuration (P0/P1, S to M).**
   - `ober config` crashes without a TTY, even with `--dry-run` [V], so Ansible and CI can't drive
     it.
   - Re-running the wizard and pressing Enter at every prompt **wipes the backend server list**
     and renames the backend [V]. The next restart serves 503 for every request while the node is
     still announced.
   - The YAML mixes per-node fields (router-id, local address, keepalived `peers` = "all nodes but
     me") with cluster-wide ones, so it can't be the same file on every node.
   - `ober sync` writes `routers.lst`, `frontend-http.lst` and `backend-http.lst`, which nothing
     reads [V].
   - In keepalived mode, VIP ownership uses md5 modulo the node count. In the README example it
     puts 2 VIPs on one node and none on another [V]. Adding a node moves every VIP [V]. Hostname
     peers make nodes disagree about who owns what [V].

   *Use one cluster file with a `nodes:` list, identical on every node. Make the wizard edit that
   file non-destructively, and make `ober apply` plus a push or Ansible path the normal workflow.*

5. **The operator can't see the cluster's real state (P1, S to M).**
   - The "Announced Routes" line in `ober status` comes from a hard-coded stub that always returns
     nothing.
   - VRRP state is scraped from the last 100 journal lines.
   - Backend health isn't shown.
   - `ober status` exits 0 when every service is down [V], and the documented `ober status --json`
     fails with "No such option" [V].
   - `ober doctor` always flags the HA unit of the other mode [V], and it never checks config
     validity, VIP presence, certificate expiry or a pending restart.
   - There is no cluster-wide view and no alerting hooks, and certificate expiry is not visible
     anywhere.

   *Make status truthful: a state file from the health process, VIP presence from `ip addr`, and a
   backend summary. Add `--check` exit codes, a small set of node metrics plus alert rules, and
   `ober cluster status`.*

---

## What a day-2 operator faces today

| Operation | Manual steps today | Main risk today |
|---|---|---|
| First install, per node | Proxmox VM plus host setup, `install.sh`, `ober bootstrap`, about 25 wizard prompts, `ober test`, `ober start`; manual `ethtool` persistence; router config | Bootstrap firewall locks out SSH (Ubuntu); multi-VIP or BFD defaults break HAProxy or ExaBGP; netplan VIP missing |
| Add a node, BGP | Same as first install, plus a router neighbor | Different ExaBGP/HAProxy versions depending on the install date |
| Add a node, keepalived | New node, then **re-run the wizard on every existing node** (re-typing backends), `ober restart` on each (hard cut), DNS record | Every VIP moves (md5 mod N); nodes disagree mid-change |
| Change RGW servers | Wizard on every node (destructive), full restart on every node | Backend wipe; connection drops; no drain of the RGW server |
| Drain an RGW server | Not supported. Needs `socat` to `/run/ober/haproxy.sock` on every node; the state is lost on restart | Easy to miss a node |
| Maintenance on an ingress node | `ober stop` (withdraw, fixed 5 s sleep, stop) | The sleep is meaningless (see 3.4); there is no "out of rotation but running" mode |
| Rotate a certificate | Copy the PEM to every node by hand, full restart (the reload is a no-op) | Expiry is invisible; partial rotation goes unnoticed |
| Upgrade ober | `pipx upgrade herr-ober` per node | Units and generated configs aren't re-rendered; the only re-render path is the destructive wizard |
| Upgrade HAProxy/ExaBGP | `ober upgrade` then `ober restart` (hard cut) | The ExaBGP check is silently skipped on pipx installs; no restart-pending detection |
| HAProxy crash | Auto-restarts | **The node stays out of rotation until someone runs `ober start`** |
| Reboot | Units come back | VRRP firewall rule is not persisted; netplan VIP missing (BGP mode) |

**What is already good** (so it isn't re-recommended):

- `ober test` already runs `haproxy -c` and `keepalived -t` and probes neighbors and backends.
- HAProxy's Prometheus exporter is enabled on `:8404/metrics`.
- `BindsTo=` captures the right intent.
- The install-path pointer works.
- The `ober watch` and blocklist subsystem is well engineered: atomic file writes, HAProxy runtime
  API instead of reloads, bounded queues, and fail-open when the watch host is down.
- The live HAProxy security tests are good. The core config path should copy these patterns.

---

## Recommendations by theme

### 1. Make the generated configuration correct and continuously validated

**1.1 Multi-VIP `bind` line is invalid HAProxy syntax** [V]. P0, effort S.
- *Evidence:* `ober/commands/config.py:1545` and `:1549` build
  `binds = " ".join(f"{h}:{https_port}" ...)`. With two internal VIPs, `haproxy -c` (3.2.25) says:
  `'bind 192.168.1.100:8443' in section 'frontend': unknown keyword '192.168.1.102:8443'`. The same
  file with comma-separated addresses passes `haproxy -c` (rc=0).
- *Why it matters:* the README keepalived example (one VIP per node, all internal) can't start
  HAProxy. Tests never use two VIPs of the same exposure.
- *Change:* join with `","`, or emit one `bind` line per VIP. Add a test that runs `haproxy -c`.

**1.2 Default ExaBGP config is rejected by ExaBGP** [V]. P0, effort S.
- *Evidence:* `config.py:1601-1609` emits `bfd { enabled; }`, and `bfd_enabled` defaults to `True`
  (`ober/config.py:22`). `exabgp validate` (5.0.15) and `exabgp --validate` (4.2.25) both report
  `section bfd is invalid in neighbor`. Without the block, the config validates.
  `tests/test_integration.py:76` asserts that `"bfd {"` is present, which locks the bug in.
- *Why it matters:* a node configured with defaults never brings up BGP. ExaBGP has no BFD, so the
  "BFD ~150 ms" failover in `README.md:169` and `architecture.md:230` isn't real. Failure
  detection is the 3 s hold time.
- *Change:* stop emitting the block and remove or rename the option. If sub-second detection is
  required, see 3.6.

**1.3 netplan VIP interface uses a mode netplan rejects** [V]. P0, effort S.
- *Evidence:* `config.py:1737-1756` writes `tunnels: lo-vip: mode: dummy`. `netplan generate`
  says `tunnel mode 'dummy' is not supported` (rc=120). The equivalent `dummy-devices: lo-vip:`
  generates cleanly. `netplan apply` runs with `check=False`, so the failure is silent.
- *Why it matters:* in BGP mode on Ubuntu the node announces VIPs that the kernel doesn't own, so
  traffic is dropped. The health check doesn't notice (see 3.2).
- *Related on RHEL* [R]: `nmcli connection add` runs on every `ober config` (`config.py:1763`),
  which creates duplicate profiles. VIPs are only ever added (`+ipv4.addresses`), never removed.
- *Related on Debian* [I]: Debian doesn't ship netplan by default, so
  `run_command(["netplan", ...])` raises `FileNotFoundError`.
- *Change:* use `dummy-devices:`. On RHEL, make it idempotent (`nmcli con modify ... ipv4.addresses
  <full list>`). Check return codes. Have doctor and status verify VIP presence.

**1.4 The ACME/Route53 option produces no S3 frontend and issues nothing** [V]. P0, effort S.
- *Evidence:* the wizard offers "Let's Encrypt via Route53 DNS-01" (`config.py:620-656`) but sets
  no `certs.path`. The S3 frontends are only emitted `if config.vips and config.certs.path and
  config.backends` (`config.py:1541`). Running the generator gives only `bind *:8404`. A grep finds
  no ACME client code. AWS keys typed into the wizard are used to list zones and then discarded.
- *Why it matters:* the node passes health, announces the VIP and refuses port 443. The summary
  prints no warning for this case. Only "no backends" gets one, at `config.py:1055-1060`.
- *Change:* remove the option until it exists (see 6.3). Until then, make "no S3 frontend" a hard
  error in `ober test`, doctor and the summary.

**1.5 Validate every generated artifact in CI and before apply** [V that CI lacks the binaries].
P0, effort S to M.
- *Evidence:* `.github/workflows/test.yml` installs only Python dependencies, so the 41 live
  HAProxy tests are skipped in CI. Nothing runs `exabgp validate` or `netplan generate`. Each bug
  in 1.1 to 1.3 is caught by a one-line binary check.
- *Change:*
  - In CI, `apt-get install haproxy keepalived netplan.io` and `pip install` the pinned ExaBGP.
  - Add a parametrized "golden cluster" test that renders BGP and keepalived 3-node configs (with
    multi-VIP, internet plus internal, BFD on and off) and runs `haproxy -c -f`,
    `exabgp validate`, `keepalived -t` and `netplan generate --root-dir` on them.
  - Call the same validator from `ober test` and `ober apply`. `ober test` lacks the ExaBGP and
    netplan checks today.

**1.6 ober.yaml has no schema validation, and a syntax error disables every command** [V]. P1,
effort S to M.
- *Evidence:*
  - `cli.py:123-124` loads the config for every command. A YAML indentation error makes
    `ober status`, and also `ober stop` during an incident, crash with a traceback [V].
  - `_load_from_file` (`ober/config.py:303-378`) accepts `ha_mode: bgpp` [V]. Everything that
    isn't `"bgp"` is then treated as keepalived (`service.py:54/65`).
  - It accepts `servers: "10.1.0.1:7480"` (a string), which renders `server srv1 1 check`,
    `server srv2 0 check`, and so on [V].
  - Unknown keys (typos) are silently ignored (`_section_from_dict`, `config.py:112-118`).
- *Change:* use a typed loader with validation: IP and CIDR, enums, lists, ranges, unknown-key
  warnings. Give clear errors with the file and key. Let `stop`, `status` and `logs` run even with
  an invalid config.

### 2. Safe apply and reload (changes without outages)

**2.1 Fix the HAProxy unit: the reload is a no-op and logs go to /dev/null** [V]. P0, effort S.
- *Evidence:* `bootstrap.py:64-67` uses `Type=forking`,
  `ExecStart=/usr/sbin/haproxy -D -f ... -p ...` and `ExecReload=/bin/kill -USR2 $MAINPID`. On
  HAProxy 3.2.25 in `-D` mode:
  - SIGUSR2 leaves the process running and still serving the old config (`v1` after editing to
    `v2`) [V];
  - fd 1 points to `/dev/null`, so `log stdout` (the default when `security.log_target` is empty,
    `config.py:1482`) logs nothing [V].

  In master-worker mode (`-W`), USR2 reloads (`v1` to `v2`) [V]. A reload with a broken config
  keeps the old workers serving [V].
- *Why it matters:* every "reload" in the tool (`service.py:210`, `sync.py:140`, and the docs'
  zero-downtime claim) silently does nothing. Operators then fall back to full restarts. Without
  logs, nobody sees "rgw/srv3 is DOWN" or config warnings.
- *Change:* copy the distro unit, which the Ubuntu package already ships with
  `Type=notify`, `-Ws`, and `ExecReload=haproxy -Ws -f $CONFIG -c` followed by `kill -USR2`:

  ```ini
  Type=notify
  ExecStartPre=/usr/sbin/haproxy -Ws -f {cfg} -c -q
  ExecStart=/usr/sbin/haproxy -Ws -f {cfg} -p /run/ober-http.pid -S /run/ober/master.sock
  ExecReload=/usr/sbin/haproxy -Ws -f {cfg} -c -q
  ExecReload=/bin/kill -USR2 $MAINPID
  ```

  With `-Ws`, stdout reaches journald, so `ober logs` becomes useful. Keep per-request logs on the
  UDP syslog target and the admin and alert logs on stdout. Because `ober upgrade` never rewrites
  units (`config.py:1126`), ship this as a drop-in that `ober apply` installs (see 9.3).

**2.2 Add `ober apply`, the one safe path for every change** [R]. P0/P1, effort M.
- *Evidence:*
  - `_apply_configuration` (`config.py:1083-1113`) saves the YAML, overwrites `haproxy.cfg`,
    `config.ini` and `keepalived.conf` in place, and stops. There is no validation, no backup, no
    diff and no reload.
  - If HAProxy config generation raises an error (for example on a bad hostname,
    `config.py:1170`), the YAML is already saved but `haproxy.cfg` isn't.
  - The ExaBGP health process reads the VIP list only at start (`health.py:307-313`), so adding a
    VIP needs an ExaBGP restart.
- *Change:* `ober apply [--dry-run] [--diff]`, non-interactive and idempotent:
  1. Load and validate the YAML.
  2. Render into a staging directory.
  3. Run the validators from 1.5.
  4. Show a unified diff against the live files.
  5. Back up the live files to `etc/.history/<ts>/`.
  6. Swap in with an atomic rename.
  7. Reload only what changed: HAProxy reload, keepalived `HUP`, netplan only if the VIPs changed.
  8. Health gate: `/health` returns 200 and the backends are UP within N seconds.
  9. On failure, roll back automatically.

  Add `ober rollback [ts]`. Let `health.py` re-read the VIP list on `SIGHUP` (or when the mtime
  changes) and announce or withdraw only the difference, so a VIP change doesn't reset the BGP
  session. The wizard should end by calling `ober apply`.

**2.3 Remove the fallback from a failed reload to a hard restart** [R]. P0, effort S.
- *Evidence:* when `systemctl reload` fails, `service.py:209-215` runs `systemctl restart
  ober-http` (with `check=True`, outside any try). The restart applies the same bad config, so
  HAProxy stays down and BGP is withdrawn.
- *Change:* a failed reload must keep the running process and exit 1 with the `haproxy -c` output.

**2.4 Restarts should be gentle by default** [R]. P1, effort S.
- *Evidence:* `ober restart` with no flags restarts `ober-http` first (`service.py:223`). That
  drops every connection and, through `BindsTo`, the BGP session or VIPs. `ober upgrade` and the
  wizard both tell the operator to run `ober restart`.
- *Change:* make `restart` mean "validated reload". Keep the hard path behind `--hard`, which
  enters maintenance first (see 3.4).

**2.5 Write configs atomically and keep a history** [R]. P1, effort S.
- *Evidence:* `OberConfig.save` (`config.py:443-444`) and every generator use `write_text`
  directly. A crash mid-write leaves a truncated file. Hand edits to generated files are lost
  silently on the next wizard run.
- *Change:* use the same tmp-plus-`os.replace` pattern as `watch/sync.py:51-64`, keep the last N
  copies, and put a "generated, do not edit" header with a content hash in each file. Doctor
  should flag hand edits (a hash mismatch).

### 3. Health, failover and self-healing

**3.1 The HA service stays dead after an HAProxy crash** [V]. P0, effort S.
- *Evidence:* `ober-bgp.service` and `ober-ha.service` have `BindsTo=ober-http.service`
  (`bootstrap.py:92`, `:115`) and `WantedBy=multi-user.target` only. I reproduced this with user
  units using the same directives: after `kill -9` of the "http" main PID, systemd restarts it, but
  the "bgp" unit stays `inactive` [V]. Adding `[Install] WantedBy=ober-http.service` (enabled), or
  `Upholds=` on the http unit, brings it back automatically [V]. A deliberate `stop` of http still
  stops it [V].
- *Why it matters:* every HAProxy crash silently removes a node's capacity until someone notices.
  Nothing alerts.
- *Change:* add `WantedBy=ober-http.service` to the HA units. `ober start` already enables the
  unit for the current mode. Keep `BindsTo`. Health still gates announcements.

**3.2 Make health reflect "can serve S3", not "HAProxy is running"** [V]. P0, effort S.
- *Evidence:* the stats frontend has `monitor-uri /health` with no `monitor fail`
  (`config.py:1527-1535`). With all four RGW servers DOWN, `/health` returned 200 while S3 requests
  got 503 [V]. With `monitor fail if { nbsrv(rgw) lt 1 }` it returned 503 [V]. Both `health.py`
  (BGP) and `chk_haproxy` (keepalived, `config.py:1651-1659`) use this endpoint.
- *Change:*
  - Emit `monitor fail if { nbsrv(<backend>) lt <min> }` (min defaults to 1, configurable as a
    percentage) and `|| { nbsrv(...) ... }` for each backend used by a frontend.
  - Have `health.py` also check that each VIP is present on `lo-vip` before announcing. This would
    have masked 1.3.
  - Have `health.py` check a maintenance flag (see 3.4).

**3.3 Add hysteresis to the BGP health loop** [R]. P1, effort S.
- *Evidence:* `health.py:329-359` announces after one good probe and withdraws after one failed
  probe (2 s timeout, 1 s interval).
- *Why it matters:* every flap is a route withdraw and re-announce. Without resilient hashing on
  the router, that rehashes flows across all nodes and resets connections cluster-wide.
- *Change:* use rise and fall counters (for example rise 3, fall 2), configurable, plus a startup
  hold-down until the backends have been checked. Log transitions with the reason.

**3.4 Add a real maintenance mode and honest drain semantics** [R plus I]. P1, effort S to M.
- *Evidence:* `ober stop` stops ExaBGP or keepalived, then `time.sleep(5)` "to drain"
  (`service.py:138-141`), then stops HAProxy. There is no way to take a node out of rotation while
  HAProxy keeps running.
- *Inferred:* with ECMP, a withdraw moves the node's flows to other next-hops, which RST them. With
  VRRP, the VIP moves and the new owner RSTs them. Either way, in-flight requests die at the
  withdraw, so the 5 s wait protects nothing. This is standard L3 behavior; confidence is high.
- *Change:*
  - `ober maintenance on|off`, which writes `<install>/etc/maintenance`. `health.py` withdraws
    while the file exists, and keepalived uses `track_file` with weight -100. Have
    `ober status`/doctor show it loudly.
  - `ober drain [--timeout]`, which turns maintenance on, polls `show info` until the current
    connections fall to a threshold or the timeout passes, and reports it.
  - Optionally, a pre-drain phase that sends `Connection: close` on responses (gated on a variable
    set over the runtime socket), so idle keep-alive clients reconnect elsewhere before the
    withdraw.
  - Document that long uploads on a drained node fail and rely on S3 client retries. Recommend
    resilient ECMP hashing on the router; the Proxmox guide already mentions it.

**3.5 Switching modes leaves the old HA unit enabled** [R]. P1, effort S.
- *Evidence:* `ober start` enables only the current mode's unit (`service.py:53-76`). `disable`
  appears only in `uninstall.py:123`. After switching from BGP to keepalived, `ober-bgp` is still
  enabled. On the next boot it announces the VIPs from the old ExaBGP config next to VRRP.
- *Change:* `start` and `apply` disable and stop the other mode's unit. Doctor flags both enabled.

**3.6 Decide what sub-second failure detection means** [V/I]. P2, effort M to L.
- *Evidence:* ExaBGP has no BFD (see 1.2). Detection today is BGP hold-time 3 s, or the watchdog
  for a frozen OS.
- *Change:* document 3 s honestly. If faster detection is required, run FRR's `bfdd` next to
  ExaBGP, or evaluate FRR (`bgpd` plus `bfdd`) in place of ExaBGP. FRR also gives `vtysh` session
  and RIB visibility for 7.1. This is a design decision. Only take it if 3 s is too slow.

**3.7 Make keepalived VIP placement deterministic, balanced and stable** [V]. P1, effort S to M.
- *Evidence:*
  - `get_vip_owner` (`config.py:54-73`) picks the owner as md5(VIP) mod N over the sorted
    `[local_ip] + peers` (`config.py:1639`).
  - In the README example (VIPs .100-.102, nodes 10.0.0.1-3), node 10.0.0.1 owns 2 VIPs and
    10.0.0.2 owns none [V]. Over 246 blocks of 3 consecutive VIPs, only 27% landed one per node;
    for 4 nodes it was 10% [V].
  - Adding a 4th node changed the owner of 3 out of 3 VIPs [V].
  - With hostname peers, as the README suggests at `README.md:99`, each node sorts a different
    list. All three nodes claimed priority 100 for two VIPs, and nobody claimed the third [V].
  - VRIDs are md5 mod 255 with no collision check (`config.py:76-86`). 10 consecutive VIPs collide
    in 26% of blocks [V].
  - Self listed in `peers` is not deduplicated [R].
- *Change:*
  - Put an explicit `owner:` (node name) per VIP in the cluster file. The default comes from
    round-robin assignment over the sorted node list, written into the file once, so adding a node
    moves nothing unless the operator asks.
  - Make VRIDs explicit or validated unique, with a configurable base so they don't collide with
    other VRRP users on the VLAN.
  - Resolve peers to IPs at render time.
  - Use `preempt_delay` (for example 30 to 60 s), so a recovering owner doesn't yo-yo the VIP.

**3.8 Per-node identity is auto-detected from the default route** [R/I]. P1, effort S.
- *Evidence:*
  - `unicast_src_ip` is `get_local_ip()`, the source address for `1.1.1.1` (`config.py:1630`).
  - The VRRP and VIP interface falls back to the default-route device (`config.py:1634-1636`,
    `122-163`).
  - BGP `router_id` and `local_address` defaults use the same logic.
  - In the Proxmox design the default route may be the management NIC (`net0`), not the front VF
    [I].
- *Change:* in the cluster file, give each node an explicit `ip`, `vrrp_interface`, `router_id` and
  `bgp_local_address`. Validate that each keepalived VIP is on-link for its interface.

**3.9 ARP for VIPs in BGP mode** [I, medium confidence]. P2, effort S.
- If a BGP VIP is on-link on the front VLAN, every node answers ARP for it, because the default
  `arp_ignore=0` lets Linux answer for addresses on `lo-vip`.
- *Change:* set `arp_ignore=1` and `arp_announce=2` in bootstrap's sysctl, or validate that BGP
  VIPs are off-link.

**3.10 Keepalived split-brain** [R]. P2, effort S.
- This is documented and inherent to VRRP.
- *Change:* add an optional gateway or router reachability `vrrp_script`, so an isolated node
  demotes itself. Log VRRP transitions through a `notify` script into a state file used by status
  (see 7.1).

### 4. Config as code, a single source of truth, and the node lifecycle

**4.1 One cluster file, identical on every node** [R]. P1, effort M.
- *Evidence:* `ober.yaml` holds both cluster-wide data (VIPs, backends, certs, security, watch) and
  per-node data (`bgp.router_id`, `bgp.local_address`, `keepalived.peers` meaning "the other
  nodes", `keepalived.interface`), plus auto-detected values at render time. Every node is
  configured by hand through its own wizard run.
- *Change:* a cluster file like this:

  ```yaml
  cluster: s3-ingress
  ha_mode: keepalived
  nodes:
    - {name: ober1, ip: 10.0.0.1, vrrp_interface: ens1f0v0}
    - {name: ober2, ip: 10.0.0.2, vrrp_interface: ens1f0v0}
    - {name: ober3, ip: 10.0.0.3, vrrp_interface: ens1f0v0}
  vips:
    - {address: 192.168.1.100, owner: ober1}
    - {address: 192.168.1.101, owner: ober2}
  backends: ...
  ```

  - The node finds itself by hostname, or by `OBER_NODE`.
  - Peers are `nodes - self`.
  - The file can live in git and be pushed unchanged.
  - Keep a small per-node override file for local-only settings.
  - Migrate the current YAML automatically.

**4.2 Make every operation non-interactive** [V]. P1, effort M.
- *Evidence:* `echo | ober config --dry-run` dies with `termios.error: (25, 'Inappropriate ioctl
  for device')` [V]. `ober config` has no options apart from `--dry-run`. `upgrade` and
  `uninstall` also prompt through `inquirer`. `uninstall` has `-y`; `upgrade` has no equivalent.
- *Change:*
  - `ober apply -f cluster.yaml` (see 2.2).
  - `ober config show|get|set key=value|edit` ($EDITOR, then validate, then apply).
  - `--yes` on everything.
  - The wizard becomes an optional front end that writes the file.
  - Ship a minimal Ansible role, or a documented `for n in ...; do ssh ...` loop, in `deploy/`.

**4.3 Make the wizard non-destructive** [V]. P0, effort S.
- *Evidence:* simulated "press Enter at every prompt" on an existing config.
  - Before: `rgw_prod: [10.1.0.1:7480, 10.1.0.2:7480]`, path `/swift/healthcheck`, 2000 ms.
  - After: `s3_backend_1: []`, path `/`, 1000 ms [V].

  The prompts use `default=""` for servers, `s3_backend_N` for the name, and fixed defaults for
  path and interval (`config.py:566-587`). The regenerated `haproxy.cfg` has the S3 frontend but a
  backend with no `server` lines [V]. Answering "No" to "Configure backends?" also deletes them.
- *Change:* prefill every prompt from the current values, and ask "Keep section X unchanged?"
  first. Before applying, show a diff (see 2.2).

**4.4 Distribute config and detect drift** [R]. P1, effort M.
- *Evidence:* there is no mechanism to copy config, certs or the security lists between nodes. Only
  the blocklist is shared, pulled from `ober watch`.
- *Change:*
  - Option A, simplest for a small team: the cluster file in git, plus `ober push --nodes ...`. It
    runs `rsync`/`scp` of the cluster file and certs over SSH, then `ober apply` **one node at a
    time**, stopping at the first failed health gate.
  - Option B: the Ansible role from 4.2.
  - Either way, `ober status` prints, and exports as a metric, a hash of the cluster-wide config
    and the rendered files (see 7.4), so drift between nodes shows up as an alert.

**4.5 `ober sync` updates files nothing reads** [V]. P0 (it misleads operators), effort S.
- *Evidence:* `sync.py:221-240` writes `routers.lst`, `frontend-http.lst` and `backend-http.lst`
  under `etc/haproxy/`. Grepping `ober/` shows no reference to these files in any generated config.
  The reload that follows (`sync.py:133-143`) is the no-op from 2.1. `config.whitelist_path`
  (`ober/config.py:251-253`) is unused as well.
- *Change:* remove the command, or repurpose the name for 4.4 (cluster sync). The CIDR lists that
  do matter (allowlist, trusted, internal) belong in the cluster file.

**4.6 Several backend groups: only the first one gets traffic** [R]. P2, effort S to M.
- *Evidence:* `config.py:1542` uses `config.backends[0]` for every frontend. The summary admits
  this (`config.py:1050-1054`), while `architecture.md:210` promises "different Ceph clusters per
  VIP".
- *Change:* add `vip.backend: <name>` and render one frontend per backend (or `use_backend` by
  destination). Otherwise, remove "Add another backend group?" from the wizard.

### 5. Backends (Ceph RGW)

**5.1 Stable server names** [R]. P1, effort S.
- *Evidence:* servers are named `srv{i+1}` by list position (`config.py:1561-1564`). Removing one
  RGW server renames all the servers after it. That breaks metric continuity, alerts and any
  runtime-API command that names a server.
- *Change:* derive the name from the host, for example `rgw07_7480`, or allow `name:` per server.

**5.2 Drain an RGW server across all ingress nodes** [R]. P1, effort M.
- *Evidence:* there is no command. Each HAProxy is independent, so draining one RGW node for Ceph
  maintenance means a runtime-API call on every ingress node. There is no `server-state-file`, so
  a drain or maintenance state is lost on the next restart.
- *Change:*
  - `ober backend drain|maint|ready <server> [--all-nodes]`, using
    `set server <be>/<srv> state drain` on the local socket. `--all-nodes` goes over SSH or the
    push channel.
  - `ober backend list`, showing per-server UP/DOWN, sessions and check status.
  - `global server-state-file` plus `load-server-state-from-file global`, and save the state
    before each reload.

**5.3 Health-check and overload tuning** [R]. P2, effort S.
- *Evidence:* servers are rendered as `check inter <ms>` only. There is no `fall`/`rise`,
  `slowstart`, per-server `maxconn` or queue timeout.
- *Change:* expose `fall`, `rise` and `slowstart` (so a recovering RGW isn't flooded), optional
  per-server `maxconn` with `timeout queue` to protect RGW under bursts, and
  `observe layer4 error-limit N on-error mark-down`.

**5.4 Change RGW membership without restarting** [I]. P2, effort M.
- Once 2.1 is in place, a reload is cheap. For zero-reload changes, consider HAProxy's runtime
  `add server` and `del server`, or `server-template` plus a `resolvers` section with DNS or SRV,
  so adding RGW nodes becomes a DNS change.

### 6. Certificates

**6.1 Make certificate expiry visible** [R]. P1, effort S.
- *Evidence:* `ober test` checks only that the PEM contains a certificate and a key
  (`test.py:350-391`). Nothing reads `notAfter`, checks the SAN or checks that the key matches.
- *Change:* show days left in `ober status`, `doctor` and `test` (warn at 21 days, fail at 7). Add
  an `ober_cert_expiry_timestamp_seconds` metric and an alert rule.

**6.2 `ober cert install <pem>` with a hot swap** [I re the runtime API, high confidence]. P1,
effort M.
- *Change:*
  - Validate the key and certificate match, the chain, a SAN covering `security.s3_hostnames`, and
    expiry.
  - Copy atomically to `certs_path`.
  - Update the live HAProxy with the runtime API (`set ssl cert <path> <<` followed by
    `commit ssl cert <path>`). This needs no reload and drops no connections. The admin socket is
    already configured.
  - With `--all-nodes`, use the push channel from 4.4.

**6.3 Issuance and distribution** [R]. P1, effort M.
- *Evidence:* the ACME and Route53 fields exist only as config (see 1.4). The `watchdog` (inotify)
  dependency in `pyproject.toml` is unused. "Auto-reloads when certificates change on disk"
  (`architecture.md:167,256`) isn't implemented.
- *Change:*
  - With N active nodes, use **one** issuer: lego or certbot with DNS-01 on the admin or watch host
    or in CI.
  - Its deploy hook calls `ober cert install --all-nodes`.
  - The `+ACME` feature in the local HAProxy 3.2 build could be evaluated later, but per-node
    issuance behind ECMP is harder to reason about.
  - Remove the Route53 wizard path and the `watchdog` dependency until one of these exists.

**6.4 Several certificates and SNI** [R]. P2, effort S.
- `bind ... ssl crt <single file>` is the only option. Allow a directory or a `crt-list` for
  several hostnames and a dual RSA/ECDSA setup.

### 7. Observability: node view, cluster view and alerting

**7.1 Make `ober status` truthful** [V/R]. P1, effort S.
- *Evidence:*
  - `_get_announced_routes()` returns `[]` with the comment "for now return empty"
    (`status.py:113-117`). When BGP is up, status always prints "Announced Routes: none".
  - The VRRP state comes from a regex over the last 100 journal lines (`status.py:120-154`). It
    shows `VI_n` rather than the VIP, and becomes "unknown" once the logs roll.
  - Human output never shows backend health. `--json` dumps the raw stats array.
  - It prints `HAProxy: None` when HAProxy is missing [V].
- *Change:*
  - `health.py` writes `/run/ober/bgp-state.json` on every transition: announced VIPs, last check,
    reason, timestamp.
  - VIP presence comes from `ip -o -4 addr show`.
  - The keepalived view shows "expected owner X, held by Y" per VIP, from `notify` state files.
  - Backend summary: `rgw: 31/32 UP (srv rgw07 DOWN 12m)`.
  - Optionally, query ExaBGP through its CLI channel for session state.

**7.2 Exit codes that monitoring can use** [V]. P1, effort S.
- *Evidence:* `ober status` exits 0 with every service inactive [V]. `ober test` exits 0 on
  warnings. Doctor's "Root Access" check is critical, so non-root doctor always fails.
- *Change:* add `ober status --check`, which exits 0 OK, 1 WARN or 2 CRIT, Nagios style, and prints
  a one-line summary. Have doctor treat "not root" as a warning for read-only checks.

**7.3 A doctor that finds the real problems** [V/R]. P1, effort S to M.
- *Evidence:* doctor always checks both `ober-bgp` and `ober-ha` (`doctor.py:75-82`), so one is
  always "disabled" and the run never passes [V]. The version test `major >= 3 and minor >= 3`
  (`doctor.py:185`) would call 4.0 "old".
- *Change:* only check the current mode's unit, and add these checks:
  - `haproxy -c` and `exabgp validate`;
  - VIPs present or absent as expected;
  - sysctl values actually applied;
  - watchdog active (`systemctl show -p RuntimeWatchdogUSec`, `wdctl`);
  - `curl` present (the keepalived check needs it);
  - stock `haproxy.service` and `keepalived.service` disabled (see 8.3);
  - **restart pending**: `/proc/<haproxy pid>/exe` shows `(deleted)` after a package upgrade;
  - cert expiry;
  - firewall allows SSH, 443, the stats port from management, VRRP or BGP;
  - chrony synced;
  - maintenance flag set;
  - other mode's unit enabled.

**7.4 Node metrics and shipped alert rules** [R]. P1, effort M.
- *Evidence:* HAProxy promex is on `:8404/metrics`. Nothing exports the HA layer, certs, versions
  or config identity.
- *Change:* add `ober metrics` (a node_exporter textfile collector run from a timer, or a tiny HTTP
  endpoint) with:
  - `ober_vip_announced{vip}` and `ober_vrrp_state{vip}`;
  - `ober_vip_present{vip}`;
  - `ober_maintenance`;
  - `ober_cert_expiry_timestamp_seconds`;
  - `ober_config_info{hash}`;
  - `ober_version_info{ober,haproxy,exabgp,keepalived}`;
  - `ober_restart_pending`;
  - `ober_blocklist_last_sync_timestamp_seconds`.

  Ship `deploy/prometheus/alerts.yml` with these alerts:
  - a node is healthy but not announcing, or the reverse;
  - a VIP is held by no node or by two nodes;
  - backend UP ratio below a threshold;
  - cert expires in under 21 days;
  - config hash differs between nodes;
  - version drift;
  - restart pending more than 7 days.

  Optionally ship a Grafana dashboard.

**7.5 Logs** [V]. P1, effort S.
- With 2.1 done, HAProxy notices and alerts (server state changes, reload results) reach journald.
- *Change:* extend `ober logs --service` with `blocklist` and `watch`, and add `--since`. Document
  that per-request logs go to `security.log_target`.

**7.6 A cluster view** [R]. P2, effort M.
- *Change:* `ober cluster status` reads the node list from the cluster file and gathers each node's
  `ober status --json` over SSH, or scrapes the metrics endpoint. It prints one matrix:
  node / http / HA / VIPs held or announced / backends UP / cert days / config hash / versions /
  maintenance. This answers "which node is unhealthy, and are the VIPs where they should be?" in
  one command.

**7.7 The documented JSON flag doesn't work** [V]. Quick win.
- *Evidence:* `ober status --json` gives "No such option '--json'". Only `ober --json status`
  works. `README.md:153` documents the broken form.
- *Change:* accept `--json` on each command as well as on the group.

**7.8 The stats endpoint listens everywhere without auth** [R]. P1, effort S.
- *Evidence:* `bind *:{stats_port}` (`config.py:1528`). The guard only rejects connections whose
  destination is an internet VIP (`config.py:1488-1496`). The node's own front-side address
  still answers `/stats`, `/metrics` and `/health`.
- *Change:* add `stats_bind` (a management IP, default `127.0.0.1` plus the management address) and
  open it in the firewall only from the monitoring network.

### 8. Bootstrap, firewall and host hygiene

**8.1 Bootstrap's firewall step can lock you out and break HA** [R; ufw defaults V; effect I,
high confidence]. P0, effort S.
- *Evidence:* `bootstrap.py:522-534` runs these steps:
  1. `ufw --force enable`.
  2. Allows only 80/tcp and 443/tcp. Port 80 isn't used: HAProxy binds only 443.
  3. `iptables -A INPUT -p 112 -j ACCEPT` for VRRP. That rule isn't persisted; the
     `iptables-save` at `:551` only prints.

  On this Ubuntu 26.04 host, ufw 0.36.2 is installed with `DEFAULT_INPUT_POLICY="DROP"` and no SSH
  or VRRP rule in `before.rules` [V]. The likely effects:
  - New SSH sessions are blocked after bootstrap. An existing session survives through conntrack.
  - The stats and Prometheus port, inbound BGP, and the watch ports (5514/udp, 8405/tcp) are
    blocked.
  - After a reboot the VRRP accept rule is gone.
  - Re-running bootstrap appends duplicate iptables rules.

  On RHEL, firewalld's default zone keeps SSH, but the stats and BGP ports aren't opened. Tests
  patch this function out (`tests/test_install_paths.py:79,184`).
- *Change:*
  - Never enable a firewall the operator didn't ask for (`--firewall` opt-in).
  - When enabling one, always allow SSH first.
  - Allow 443 on the VIPs, the stats port from a management CIDR, VRRP from the peers (as a
    persistent ufw `before.rules` snippet or a firewalld rich rule), BGP to and from the
    neighbors, and the watch ports on the analysis host.
  - Make it idempotent.
  - Report the result in doctor.

**8.2 Conntrack at 50 GB/s** [I, medium confidence]. P1, effort S.
- A stateful host firewall (ufw or firewalld) tracks every client and RGW connection. At this
  scale `nf_conntrack_max` becomes a capacity cliff: "table full, dropping packet".
- *Change:* use `notrack` for the data-plane ports (raw table) or stateless nftables rules on the
  data VFs. If tracking stays, raise `nf_conntrack_max` and export `nf_conntrack_count`.

**8.3 Disable the distro HAProxy and keepalived units** [V]. P1, effort S.
- *Evidence:* the Ubuntu `haproxy` postinst enables and starts `haproxy.service`
  (`deb-systemd-helper enable`, `deb-systemd-invoke start`) [V]. Bootstrap never disables it, so
  every node runs a second, unrelated HAProxy. `apt upgrade` restarts that one, not `ober-http`, so
  the real proxy keeps the old binary until someone restarts it. Doctor's restart-pending check
  (see 7.3) covers this.
- *Change:* `systemctl disable --now haproxy keepalived` in bootstrap, then check in doctor.

**8.4 The keepalived check depends on curl** [R]. P2, effort S.
- `chk_haproxy` runs `/usr/bin/curl -sf` (`config.py:1653`) with no `--max-time`. Bootstrap
  installs only `haproxy` and `keepalived`. Without curl the check always fails, and failover on
  an HAProxy stall stops working.
- *Change:* install curl and add `--max-time 1`, or ship a tiny `ober healthcheck` entry point.

**8.5 Watchdog** [R]. P2, effort S.
- *Evidence:* `bootstrap.py:503-514` appends to `/etc/systemd/system.conf` and doesn't re-exec
  systemd, so nothing is active until the next reboot. Nothing verifies that the `i6300esb` device
  exists.
- *Change:* use a drop-in in `/etc/systemd/system.conf.d/`, run `systemctl daemon-reexec`, and add a
  doctor check.

**8.6 SELinux on RHEL** [R/I]. P2, effort S.
- `architecture.md:122` says bootstrap disables SELinux. No code does this. HAProxy as `haproxy_t`
  binding 8404 and creating `/run/ober/haproxy.sock` may be denied [I, unverified].
- *Change:* either ship the needed booleans or port labels, or document a manual step and add a
  doctor check.

**8.7 Persist NIC tuning** [R]. P2, effort M.
- The Proxmox guide leaves `ethtool -G/-L` persistence to the admin. Bootstrap could write a
  systemd-networkd `.link` file or a NetworkManager dispatcher script from configured VF names.

### 9. Upgrades and version control

**9.1 Pin ExaBGP and manage it through pipx** [V/I]. P1, effort S.
- *Evidence:* bootstrap runs an unpinned `pip install exabgp` (`bootstrap.py:318-329`), which gives
  5.0.15 today, a major version newer than what the template was written for. Nodes bootstrapped
  at different times run different ExaBGP majors.
- Because ExaBGP isn't installed with `pipx inject`, `pipx reinstall herr-ober` drops it, and then
  `ober-bgp`'s `ExecStart` points at a missing binary [I, high confidence]. The README tells users
  to run exactly that reinstall when upgrading from v0.3.0.
- *Change:* add a `[bgp]` extra in `pyproject.toml` with a tested range, for example
  `exabgp>=4.2,<5` or a validated `5.0.x`. Install `herr-ober[bgp]`. Add a CI job that runs the
  health process against the pinned ExaBGP.

**9.2 `ober upgrade` silently skips ExaBGP on pipx installs** [R]. P1, effort S.
- *Evidence:* `upgrade.py:196-198` and `:258-263` require `<venv>/bin/pip`. The project's own
  comment at `bootstrap.py:323` says pipx venvs lack it. With 9.1, ober and ExaBGP upgrade together
  through `pipx upgrade`.

**9.3 Upgrades must re-render units and configs, and support rolling** [R]. P1, effort M.
- *Evidence:* "`ober upgrade` never rewrites unit files" (`config.py:1126`). Generated configs only
  change when the wizard runs again, which is destructive (see 4.3). After upgrading ober, nodes
  keep the old templates indefinitely.
- *Change:*
  - `ober upgrade` (or `pipx upgrade` followed by `ober apply`) re-renders units and drop-ins, shows
    a diff, and does a `daemon-reload`.
  - Document, and later script, a rolling procedure: per node, maintenance on, upgrade, validate,
    validated restart, health gate, maintenance off, then the next node.
  - `ober upgrade --rolling --nodes ...` can reuse the push channel from 4.4.

**9.4 Choose and pin an HAProxy channel, and check the TLS library** [V/R/I]. P1, effort S to M.
- *Evidence:*
  - The Ubuntu 26.04 distro build is HAProxy 3.2.25 with OpenSSL 3.5.5 and `-OPENSSL_AWSLC` [V].
  - Bootstrap adds `ppa:vbernat/haproxy-3.3` with `check=False` (`bootstrap.py:295-298`). If that
    step fails, the node quietly gets the distro version.
  - Debian and RHEL always get the distro version.
  - The PPA build is very likely OpenSSL as well [I].
  - Nothing ensures the "AWS-LC" that the docs promise.
- *Change:*
  - Pick the supported source per OS and pin it (`apt-mark hold` or `dnf versionlock`) so the
    whole cluster runs one version.
  - Fail bootstrap when the repository can't be added.
  - Have doctor report the TLS library from `haproxy -vv`.
  - Either provide an AWS-LC build or drop the claim.
  - Show version drift in 7.4 and 7.6.

### 10. Code-level issues that raise the risk of breaking the cluster

**10.1 One generic, typed config loader and saver** [R]. P1, effort S to M.
- `_load_from_file` and `save` map the BGP, keepalived, VIP, backend and cert fields by hand
  (`ober/config.py:303-444`), while the newer sections use `_section_from_dict` and `asdict`. One
  generic path with type and enum validation (see 1.6) removes the "add the field in three places"
  trap that CLAUDE.md warns about.

**10.2 State-changing commands ignore failures** [R]. P1, effort S.
- `run_command(..., check=False)` is used for `netplan apply`, `nmcli`, `add-apt-repository`,
  `sysctl --system`, ufw, firewalld and iptables (`bootstrap.py:287-590`, `config.py:1756-1786`).
  This is how the netplan error in 1.3 stayed hidden.
- *Change:* check the results, print stderr, and verify the end state (VIP present, sysctl value,
  firewall rule).

**10.3 Pure renderers** [R]. P2, effort S.
- `_generate_keepalived_config` calls `SystemInfo()` and the default route itself
  (`config.py:1627-1636`), so its output depends on the host it runs on. Passing the node identity
  in (see 4.1) makes renders reproducible, diffable and golden-testable (see 1.5).

**10.4 Documentation that contradicts the code** [R]. P2, effort S. For the docs branch:
- BFD ~150 ms (`README.md:169`, `architecture.md:230`);
- certificate auto-reload through inotify (`architecture.md:167,256,333`);
- SELinux disabled by bootstrap (`:122`);
- "wait for connections to drain" (`:278`);
- several backends per VIP (`:210`);
- `ober sync` validates and updates whitelists (`:285`);
- `ober config` validates BGP, RGW and certificates (`:286`);
- "No access logging" (`:251`, contradicted by `ober watch`);
- package name `ober` (`:315`) and dependency `hostlist` (`:330`);
- `ober status --json` (`README.md:153`);
- the README's keepalived peers given as hostnames (`README.md:99`).

**10.5 Supply-chain risk for something that runs as root on the ingress** [R]. P1, effort S.
- *Evidence:* `issue-auto-fix.yml` runs on **any** opened issue. It feeds the issue body to
  `claude --dangerously-skip-permissions`. If `ISSUE_AUTO_FIX_MERGE=yes`, it merges to `main` after
  `sleep 5` without waiting for CI (lines 178-195). The install one-liner pipes `main/install.sh`
  into `sudo bash` on every new node.
- *Change:*
  - Trigger only on a maintainer-applied label.
  - Require CI to pass and a human review before merging.
  - Serve `install.sh` from a tagged release and pin the herr-ober version it installs.

---

## Quick wins (each about a day or less)

1. Join multi-VIP binds with commas (`config.py:1545,1549`). [1.1]
2. Stop emitting the ExaBGP `bfd {}` block, and fix the test that asserts it. [1.2]
3. netplan `dummy-devices:` instead of `tunnels: mode: dummy`. [1.3]
4. Hide the Route53 option. Make "no S3 frontend generated" an error. [1.4]
5. `monitor fail if { nbsrv(<backend>) lt 1 }` in the stats frontend. [3.2]
6. `WantedBy=ober-http.service` on `ober-bgp` and `ober-ha`, so they come back after an HAProxy
   crash. [3.1]
7. HAProxy unit: `-Ws`, `Type=notify`, `haproxy -c` in `ExecStartPre` and `ExecReload` (as a
   drop-in). Remove the reload-failure restart fallback. [2.1, 2.3]
8. Bootstrap firewall: opt-in only, always allow SSH, persist VRRP, drop port 80. [8.1]
9. Prefill every wizard prompt with the current values (backends especially). [4.3]
10. Remove or rename `ober sync`, which writes unused files. [4.5]
11. Disable the stock `haproxy.service` and `keepalived.service` in bootstrap. [8.3]
12. Pin ExaBGP as a package extra. [9.1]
13. Doctor: check only the current mode's unit, and add restart-pending, cert-expiry and
    VIP-present checks. [7.3]
14. Add `ober status --check` with real exit codes, and accept `--json` per command. [7.2, 7.7]
15. Rise and fall hysteresis plus a maintenance-flag file in `health.py`. [3.3, 3.4]
16. CI: install haproxy, keepalived, netplan and the pinned ExaBGP, and validate a golden 3-node
    render. [1.5]
17. Disable the other mode's unit on `start` and `apply`. [3.5]

---

## Appendix: verification log

The scratch work dir held a venv, a `git archive HEAD` snapshot of the repo, and the debs
extracted with `dpkg -x`. The repo working tree was not modified. Temporary systemd **user** units
`obrv-test-*` were created for the `BindsTo` test and then removed.

| Claim | Command (abridged) | Result |
|---|---|---|
| Test suite | `pytest -q` | 495 passed, 41 skipped |
| Live HAProxy tests | `PATH=<extracted haproxy> pytest tests/test_haproxy_live.py tests/test_haproxy_security.py tests/test_integration.py` | 74 passed, 1 failed (socket path too long; environment only) |
| 1.1 multi-VIP bind | `_generate_haproxy_config` for 2 internal and 1 internet VIP; `haproxy -c` | `unknown keyword '192.168.1.102:8443'`; the comma-joined version gives rc=0 |
| 1.2 BFD | `exabgp validate` (5.0.15) and `exabgp --validate` (4.2.25) on the generated `config.ini` | `section bfd is invalid in neighbor`; validates without the block |
| 1.3 netplan | `netplan generate --root-dir` on the generated YAML | `tunnel mode 'dummy' is not supported` (rc=120); `dummy-devices:` works |
| 1.4 ACME | Generator with `CertConfig(acme_enabled=True, route53_enabled=True)` | No S3 frontend; only `bind *:8404` |
| 1.6 YAML | Bad indentation, then `ober status`; `ha_mode: bgpp` and `servers: "10.1.0.1:7480"` | Traceback; `bgpp` accepted; renders `server srv1 1 check` |
| 2.1 reload/logs | `haproxy -D` plus `kill -USR2` after editing the config; `ls -l /proc/PID/fd/1` | Still serves `v1`; stdout is `/dev/null` |
| 2.1 fix | `haproxy -W -D`, USR2, then USR2 with a broken config | Reloads to `v2`; bad config rejected, old workers keep serving |
| 3.1 BindsTo | User units with the same directives, `kill -9` of the http PID | bgp stays `inactive`; with `WantedBy=http` or `Upholds=`, bgp comes back |
| 3.2 health | Real HAProxy, generated config, unreachable RGWs | `/health` returns 200, backend `DOWN`, S3 returns 503; with `monitor fail` it returns 503 |
| 3.7 VIP placement | `get_vip_owner` and `get_vrrp_router_id` simulations | README example: 2/0/1 VIPs per node; 27% even split for 3 nodes; adding a node moves 3/3 VIPs; hostname peers: 3 claimants, 0 claimants; 26% VRID collision for 10 VIPs |
| 4.2 TTY | `echo \| ober config --dry-run` | `termios.error: (25, 'Inappropriate ioctl for device')` |
| 4.3 wizard | Simulated "Enter at every prompt" through a patched inquirer | Backends `rgw_prod [2 servers]` become `s3_backend_1 []` |
| 4.5 sync | `grep -rn 'routers.lst\|frontend-http\|backend-http' ober/` | No consumer outside `sync.py` |
| 7.2, 7.7 status | `ober status` with all units inactive; `ober status --json` | rc 0; "No such option '--json'" |
| 7.3 doctor | `ober doctor` | Reports both `ober-bgp` and `ober-ha` |
| 8.1 ufw defaults | `/etc/default/ufw`, `/etc/ufw/before.rules` | `DEFAULT_INPUT_POLICY="DROP"`; no SSH or VRRP rule |
| 8.3 stock unit | `dpkg -e haproxy_3.2.25*.deb`; `postinst` | `deb-systemd-helper enable` and `deb-systemd-invoke start haproxy.service`; stock unit uses `-Ws`, `Type=notify`, `-c` on reload |
| 9.1, 9.4 versions | `pip install exabgp`; `haproxy -vv` | ExaBGP 5.0.15; HAProxy 3.2.25 with OpenSSL 3.5.5, `-OPENSSL_AWSLC` |
