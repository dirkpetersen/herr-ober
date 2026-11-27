# Hardening plan: pending work

Status as of 2026-11-25. Nothing in this plan has been implemented yet. The work moved to another
server before the first wave of agents produced any commits.

- `REVIEW.md` is the day-2 operability review that every item number below refers to (e.g. 2.1).
  Each item has evidence (`file:line`), priority (P0/P1/P2), effort, and a verification tag.
- `QUESTIONS.md` holds the open decisions and the default answer that work proceeds on.

## Branches and state

| Branch | State |
|---|---|
| `improve/ingress-hardening` | Integration branch (local, not pushed when this was written). Holds the CLAUDE.md updates, `QUESTIONS.md`, `REVIEW.md`, this file and `lab/aws/`. Every fix branch starts here and is merged back here. |
| `docs/claude-md-watch` | CLAUDE.md updates, already merged (fast-forward) into the integration branch. |
| `docs/zensical` | Zensical docs site plus GitHub Pages workflow (commit `e1768e9`). **Pushed**, but the PR is not opened yet (`gh` was missing). The PR body is reproduced at the end of this file. Pages needs a manual step: Settings → Pages → Source: **GitHub Actions**, then run the "Documentation" workflow. |
| `fix/*` | Empty (equal to the integration branch). Safe to delete; recreate them when the work starts. |

## How to run the work

1. Branch each area from `improve/ingress-hardening` as `fix/<area>`. Commit messages end with
   `Co-Authored-By: ...` as usual.
2. All four CI checks must pass on every branch: `ruff check .`, `ruff format --check .`,
   `mypy ober/`, `pytest`. Add or adjust tests for every fix.
3. Validate generated configs with the real binaries. On the old server these were extracted, not
   installed. On the new server install them (Ubuntu: `apt install haproxy keepalived netplan.io`;
   ExaBGP 4.2.x and 5.0.x in two separate venvs), or point the tests at extracted copies.
4. Merge each finished branch into the integration branch, resolve conflicts, re-run the four
   checks, and record its open questions with their defaults in `QUESTIONS.md`.
5. After wave 1, deploy the integration branch to the AWS lab and retest (see the Lab section). Then
   start wave 2 from the updated integration branch.
6. Nothing is pushed or merged to `main` without a human decision.

The areas in wave 1 can run in parallel. Each area owns specific files so that merges stay small.

## Wave 1: P0 items and quick wins

### A. Generated configs and CI validation (`fix/generators`)
Owns the `_generate_*` functions in `ober/commands/config.py`, generator tests, and
`.github/workflows/test.yml`.
- **1.1** Join multi-VIP `bind` addresses with commas (`config.py` ~1545, 1549). Check with `haproxy -c`.
- **1.2** Stop emitting the ExaBGP `bfd {}` block. Fix the test that asserts it (`tests/test_integration.py:76`), and the factual "~150 ms BFD" claims.
- **1.3** netplan `dummy-devices:` instead of `tunnels: mode: dummy`. Make nmcli on RHEL idempotent. Check return codes.
- **1.4 (generator side)** Render fails with a clear error when no S3 frontend would be generated.
- **1.5** Golden 3-node render test for BGP and keepalived (multi-VIP, internal plus internet). Validate with `haproxy -c`, `exabgp validate`, `keepalived -t` and `netplan generate --root-dir`, skipping cleanly when a binary is missing. CI installs these binaries and runs the test.
- **3.2** `/health` fails when no backend server is up: `monitor fail if { nbsrv(<backend>) lt 1 }`.
- **7.8** Stats/metrics/health listener bound to localhost or a management address. Optional basic auth.
- **4.6** (P2, if time allows) Several backend groups: fail loudly instead of silently using only the first.

### B. Systemd units, reload, lifecycle (`fix/units-lifecycle`)
Owns the unit templates in `bootstrap.py`, `service.py` (except the drain logic in `stop`) and
`upgrade.py`.
- **2.1** `ober-http`: master-worker `-Ws`, `Type=notify`, `haproxy -c` in `ExecStartPre` and before reload (model it on the stock Ubuntu unit). Logs reach the journal. Use a drop-in for nodes that are already bootstrapped.
- **2.3** A failed reload never falls back to a hard restart. Keep the running process and exit 1 with the error.
- **2.4** `restart` means a validated reload; a hard restart needs `--hard`.
- **3.1** Keep `BindsTo=` and add `WantedBy=ober-http.service` to `ober-bgp`/`ober-ha`, so they return after an HAProxy crash. Rewrite the "Systemd safety links" section of CLAUDE.md.
- **3.5** `start` disables and stops the other mode's unit.
- **8.3** Bootstrap disables the distro `haproxy.service` and `keepalived.service`.
- **9.1** Pin ExaBGP to 4.2.x as a dependency or extra that survives `pipx reinstall`.
- **9.2** `ober upgrade` handles ExaBGP on pipx installs.

### C. Health and maintenance (`fix/health-maintenance`)
Owns `ober/commands/health.py`, a new `ober/commands/maintenance.py`, the vrrp_script/track part
of `_generate_keepalived_config`, and the drain logic in `ober stop`.
- **3.3** Rise/fall hysteresis in the BGP health loop (configurable), with a startup hold-down. Withdraw on SIGTERM stays. Only ExaBGP commands go to stdout.
- **3.4** `ober maintenance on|off|status` uses a flag file: BGP withdraws, keepalived drops priority (`track_file`). `ober stop` enters maintenance, waits for the withdraw or VIP move, drains connections (runtime API `show info`) up to a timeout, then stops. This replaces the fixed 5 s sleep.
- **3.10** Optional gateway-reachability `vrrp_script`, and a `notify` script that writes VRRP state for status.
- **8.4** Replace the curl-based keepalived check with a Python entry point (exit 0/1, short timeout).

### D. Status, doctor, logs (`fix/status-doctor`)
Owns `status.py`, `doctor.py`, `logs.py` and the `--json` handling in `cli.py`.
- **7.1** Truthful status: announced routes from a state file written by health.py, VRRP state from keepalived (not journal scraping), backend health from the runtime socket, VIP presence from `ip addr`.
- **7.2** `ober status --check` exits 0 OK, 1 WARN, 2 CRIT, with a one-line summary.
- **7.3** Doctor checks:
  - only the current mode's unit;
  - restart pending (`/proc/<pid>/exe` deleted, config newer than the process);
  - cert expiry;
  - VIPs present;
  - stock units disabled;
  - `haproxy -c`;
  - the version test bug (`major >= 3 and minor >= 3`).
- **7.5** `ober logs` covers the journal for `ober-http`, `blocklist` and `watch`, and adds `--since`.
- **7.7** `--json` accepted per command, not only globally.
- **6.1** Certificate expiry shown in status, doctor and test (warn at 21 days, fail at 7).

### E. Wizard, validation, bootstrap host setup (`fix/wizard-bootstrap`)
Owns the prompt functions and the `config` command in `commands/config.py`, `sync.py`, the
firewall and sysctl functions in `bootstrap.py`, and loading in `ober/config.py`.
- **4.2** No TTY needed: `config --dry-run` works without one. Add `ober config --non-interactive` that renders from the existing YAML.
- **4.3** Non-destructive wizard. Every prompt is prefilled, and a test asserts that "Enter everywhere" round-trips the YAML unchanged.
- **1.4 (wizard side)** Hide the Let's Encrypt via Route53 option.
- **4.5** `ober sync` writes files nothing reads. Wire them up or deprecate the command, and record the choice in QUESTIONS.md.
- **8.1** Firewall:
  - opt-in with `--firewall`;
  - SSH always allowed first;
  - VRRP rule persisted;
  - only the needed ports opened;
  - both ufw and firewalld covered.
- **8.2** Conntrack: `notrack` for data-plane ports, or raise `nf_conntrack_max`.
- **1.6** Schema validation for ober.yaml: one clear error for a YAML syntax error, enums, types, IP/port formats, and all problems reported at once. `stop`, `status` and `logs` still work with an invalid config.
- **3.8** Explicit `local_ip` override for keepalived `unicast_src_ip` and the BGP router-id/local-address.

### F. Amazon Linux support plus the AWS lab (`fix/amazon-linux`)
- **Code:**
  - Treat `ID=amzn` (`ID_LIKE=fedora`) as the RHEL family in `ober/system.py`.
  - Accept versions 2023 and 2027.
  - Find out what AL2027 ships for haproxy, keepalived and python3.12+.
  - VIP setup through a systemd-networkd dummy device when nmcli is absent.
  - Don't assume firewalld.
- **Lab:** see below.

## Wave 2 (after wave 1 is merged and retested)

- **Apply path:**
  - 2.2 `ober apply` (render → validate → diff → backup → atomic swap → targeted reload → health gate → rollback) and `ober rollback`;
  - 2.5 atomic writes plus history;
  - health.py re-reads VIPs on SIGHUP.
- **Config as code:**
  - 4.1 one cluster file with `nodes:` and explicit VIP owners;
  - 4.4 `ober push` one node at a time with a health gate, and drift detection by config hash;
  - 10.1 one generic typed config loader;
  - 10.3 pure renderers.
- **VIP placement:** 3.7 explicit owners, unique VRIDs, peers resolved to IPs, `preempt_delay`.
- **Backends:** 5.1 stable server names, 5.2 `ober backend drain|maint|ready [--all-nodes]` plus `server-state-file`, 5.3 fall/rise/slowstart/maxconn, 5.4 runtime add/del server.
- **Certificates:** 6.2 `ober cert install` with a runtime-API hot swap, 6.3 one issuer plus a deploy hook (drop the unused `watchdog` dependency until then), 6.4 crt-list/SNI.
- **Observability:** 7.4 `ober metrics` plus `deploy/prometheus/alerts.yml`, 7.6 `ober cluster status`.
- **Upgrades:** 9.3 re-render units on upgrade and a rolling upgrade procedure, 9.4 pin the HAProxy channel, fail when the PPA can't be added, report the TLS library.
- **Robustness:** 10.2 check the results of state-changing commands, 10.5 auto-fix workflow only on a maintainer label with CI plus review before merging, and serve `install.sh` from a tagged release.
- **Lower priority:** 3.6 sub-second detection (FRR bfdd) as a design decision, 3.9 `arp_ignore`/`arp_announce`, 8.5 watchdog drop-in plus re-exec, 8.6 SELinux, 8.7 persist NIC tuning.
- **Docs:** 10.4 stale claims, to be done on or after the `docs/zensical` branch (which already lists most of them).

## Wave 3 (owner request 2026-11-27)

Waves 1 and 2 are merged and released as 0.4.0. Wave 3 is tested on AlmaLinux 10 in the AWS lab,
with herr-ober as the frontend to the AWS S3 bucket `hcic-townhall` (us-west-2).

- **3.6 BFD:** sub-second detection of a dead node or link in BGP mode. ExaBGP has no BFD, so
  each node runs FRR `bfdd` (BFD only, no FRR BGP) toward the BGP neighbors. The router side
  (bird2 in the lab) ties BFD to the BGP session. `bgp.bfd_enabled` becomes real.
- **Rolling upgrade command:** `ober cluster upgrade` runs the 9.3 runbook node by node: cluster
  status OK, maintenance on, wait for drain, upgrade the package, `ober upgrade`, restart when
  needed, doctor, maintenance off, wait for cluster status OK. It stops at the first failure.
- **5.4 runtime servers:** when `ober apply` only adds or removes backend servers, it uses the
  HAProxy runtime API (`add server`, `enable server`, `del server`) instead of a reload, then
  writes `haproxy.cfg` so a later reload gives the same state.
- **TLS to backends:** a backend server can be HTTPS (verify with the system CA, SNI, Host
  header kept), so AWS S3 or RGW with TLS can be a backend. Health check fits S3.
- **Lab on AlmaLinux 10:** 3 ingress nodes plus a helper with bird2 (BGP, ECMP, BFD). Backend:
  `s3.us-west-2.amazonaws.com:443`. Clients sign for the AWS name, which resolves to the VIP
  (SigV4 binds the host name). Tests write only under the prefix `herr-ober-test/` and remove
  only that prefix. Test access is from the helper over SSH; no public endpoint.

## Lab (AWS)

- **Account:** us-west-2, profile `iam-dirk` on the old server. Set `AWS_PROFILE` on the new one.
- **Image:** Amazon Linux 2027 **preview**, arm64, newest `al2027-preview-ami-2027.0.*-kernel-7.2-arm64`. There is no GA image yet.
- **Topology:**
  - 3 ingress nodes, t4g.medium, with source/dest check disabled;
  - 1 helper, t4g.small, running an S3 backend (MinIO or moto, 2 servers), a BGP peer (bird2 or GoBGP) with ECMP, and the S3 test client.
  - VIPs 10.99.0.100-101, outside the VPC CIDR 10.77.0.0/16.
- **Network:** a dedicated VPC; SSH only from the operator's IP; everything allowed inside the security group; IMDSv2 required.
- **Tags:** every resource carries `Project=herr-ober-lab`.
- **Current state:**
  - Nothing is created in AWS (checked by tag).
  - `lab/aws/lib.sh` and `lab/aws/up.sh` exist. `up.sh` is idempotent and creates the VPC, IGW, subnet, routes, SG, key pair (`~/.ssh/herr-ober-lab.pem`), the 4 instances, `inventory` and `ssh_config`. It then calls `provision.sh`.
  - Still to write: `provision.sh`, `deploy.sh <ref>` (build a wheel, pipx install on all nodes), `teardown.sh` (delete by tag), `README.md`, `FINDINGS.md`.
- **Test plan:**
  1. Reproduce the P0 findings on real nodes against the baseline.
  2. S3 PUT/GET end to end through the VIPs via ECMP, then fail over a node and time it.
  3. Keepalived mode: VRRP states and priorities. A VPC ignores gratuitous ARP, so only state changes can be tested, not real traffic moving.
  4. Reboot a node.
- **Cost:** about $0.12/hour while running. Tear it down when idle.

## Appendix: `docs/zensical` PR text

Title: *Add Zensical documentation site published to GitHub Pages*. Open it with
`gh pr create --base main --head docs/zensical --title "..." --body-file <this section saved to a file>`,
or paste it into https://github.com/dirkpetersen/herr-ober/compare/main...docs/zensical?expand=1.
The body follows, verbatim:

    ## Summary

    Adds a documentation site built with [Zensical](https://zensical.org) and published to GitHub Pages at https://dirkpetersen.github.io/herr-ober/.

    - `zensical.toml` at the repo root (native Zensical format, as generated by `zensical new`; `docs/` is the docs_dir). It sets `site_url`, `repo_url`, nav, theme, and the Markdown extensions.
    - New pages: Home, Getting started, Configuration, CLI reference, Development. The `ober.yaml` reference comes from the dataclasses in `ober/config.py`. The CLI reference comes from the actual click options (`ober <cmd> --help`).
    - **Architecture** and **Sentinel and Defender** pages include `architecture.md` and `deploy/sentinel/README.md` with `pymdownx.snippets`, so the root files stay the single source and keep working on GitHub.
    - `.github/workflows/docs.yml` builds with `zensical build --clean --strict` and deploys.
    - `pyproject.toml` gets a `docs` extra (`zensical>=0.1.3`), and the `Documentation` URL now points to the site.

    ### Site structure

    ```
    Home                      docs/index.md            (from README)
    Getting started           docs/getting-started.md  (README quick start + bootstrap steps from bootstrap.py)
    Configuration             docs/configuration.md    (wizard, HA modes, install path, files, ober.yaml reference, examples)
    CLI reference             docs/cli.md              (all commands incl. watch/blocklist/health)
    Architecture              docs/architecture.md  -> includes architecture.md
    Proxmox deployment        docs/proxmox-deployment.md
    Security
      Security monitoring     docs/security-monitoring.md
      Sentinel and Defender   docs/sentinel.md      -> includes deploy/sentinel/README.md
    Development               docs/development.md
    ```

    ### Deployment: actions/deploy-pages, not a gh-pages branch

    Zensical's publishing guide recommends this approach: the workflow that `zensical new` generates uploads `site/` with `actions/upload-pages-artifact` and deploys it with `actions/deploy-pages`. There is **no `gh-pages` branch**. The workflow here is based on that template, with three changes:

    - It is split into `build` and `deploy` jobs.
    - Pull requests that touch the docs get a strict build only, with no deploy.
    - Only the deploy job gets `pages: write` / `id-token: write`.

    It runs on pushes to `main` that touch `docs/**`, `zensical.toml`, `README.md`, `architecture.md`, `deploy/sentinel/README.md`, `pyproject.toml` or the workflow, and it can also be run manually (`workflow_dispatch`).

    ### Manual step required: enable Pages

    The repository does not have Pages enabled yet. This PR does not change repository settings. Before or after merging:

    1. Go to **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**.
    2. Then run the workflow: **Actions → Documentation → Run workflow** on `main`. Any docs change merged to `main` also runs it.

    Until step 1 is done, the deploy job fails at `actions/configure-pages` with "Get Pages site failed".

    ### Other changes

    - **Rendering fixes.** Python-Markdown (which Zensical uses) needs a blank line before a list that follows a paragraph, and 4-space indentation for nested lists and list-item code blocks. `architecture.md` and `docs/proxmox-deployment.md` were adjusted accordingly. This is whitespace only and looks the same on GitHub. Before the fix, the Proxmox host steps and the architecture command lists rendered as run-on paragraphs. `sane_lists` is enabled so a bulleted list followed by a numbered list stays two lists.
    - **Absolute links in `architecture.md`.** Its three relative links (`README.md#quick-start`, `docs/proxmox-deployment.md`) are now absolute GitHub URLs, so they work both on GitHub and when the file is included in the site.
    - **`docs/security-monitoring.md`.** Now links to the Sentinel page inside the site.
    - **README fixes.** It links to the site. `ober status --json` became `ober --json status`: `--json` is a global option, and the old form fails with "No such option". `cd ober` after cloning became `cd herr-ober`.
    - **CLAUDE.md.** One line added for previewing the docs.

    ### Build locally

    ```bash
    pip install -e ".[docs]"
    zensical serve                     # http://localhost:8000/herr-ober/
    zensical build --clean --strict    # what CI runs; output in site/ (git-ignored)
    ```

    ## Content gaps found (not fixed in this PR)

    **Stale statements in `architecture.md`.** These are included verbatim and need a decision from the maintainer:

    - It says HAProxy has "no access logging" and "no rate limiting". The `security:` / `ober watch` features contradict this: JSON request logs, auto-ban, and per-IP connection limits on Internet VIPs.
    - It says "Auto-reloads when certificates change on disk (watchdog library)". Nothing in `ober/` imports `watchdog`.
    - It says "Certs provided via `--cert <path>`". There is no such option.
    - It says "`ober bootstrap` will disable SELinux". There is no SELinux code.
    - It says the RHEL dummy interface is set up by `ober bootstrap`. `ober config` does that, and only in BGP mode.
    - It says `~/.ober/login` holds BGP passwords. No code reads a BGP password.
    - It says the wizard "allows skipping already-configured sections". Only the certificate section can be skipped.
    - It lists the wizard order as "BGP → VIP → …". HA mode is asked first.
    - Command details are out of date:
      - `ober health <vip>`: the VIP is optional and ExaBGP starts it without one.
      - `ober logs --service http|bgp`: `ha` and `all` also exist.
      - `ober test`: described as BGP-only, but it also tests keepalived.
    - Package details are out of date:
      - "PyPI name: `ober`": the name is `herr-ober`.
      - Runtime dependency "`hostlist`": it is now `python-hostlist`.

    **Behavior I found while documenting.** The docs describe the actual behavior:

    - **Route53 certificate option.** The wizard's "Let's Encrypt via Route53 DNS-01" option only saves `certs.acme_*` / `certs.route53_*`. Nothing issues a certificate, and the S3 frontends are only generated when `certs.path` is set.
    - **Backend groups.** The S3 frontends always route to the **first** backend group. Architecture.md describes "different Ceph clusters per VIP".
    - **`vips[].interface`.** It is saved but not used. The dummy interface is always `lo-vip`, and keepalived uses `keepalived.interface`.
    - **Announced routes in `ober status`.** The function that reports them is a stub and always returns an empty list.
    - **Keepalived peers as hostnames.** The README example `node2,node3` gives each node a different node list, so VIP ownership is inconsistent across nodes, and the hostnames go into `unicast_peer` unchanged. The new docs tell users to enter IPs.
    - **Hostlists.** BGP neighbors are comma-separated IPs only. CLAUDE.md says node/router/peer inputs accept Slurm hostlists, but only keepalived peers and `ober sync` do.
    - **`ober-watch.service`.** Bootstrap and config write it, but no command enables it.

    ## Verification

    - In a fresh venv, `pip install ".[docs]"` (zensical 0.1.3) and then `zensical build --clean --strict` reports "No issues found".
    - Zensical does not validate links inside snippet-included content. A separate check of the built HTML found 0 broken internal links or anchors out of 533, and 0 unrendered Markdown constructs (raw list markers, `!!!`, `===`, `--8<--`, `**`).
    - `zensical serve` serves the site and redirects `/` to `/herr-ober/`.
    - `ruff check .` and `ruff format --check .` pass.

    🤖 Generated with [Claude Code](https://claude.com/claude-code)
