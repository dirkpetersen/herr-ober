# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance (50GB/s+) S3 ingress controller for Ceph RGW clusters on Proxmox VMs. HAProxy 3.3 does SSL offloading (PPA, Debian and RHEL builds use OpenSSL; Amazon Linux 2027's `haproxy-awslc` uses AWS-LC FIPS; `ober doctor` shows which); HA is provided by one of two **mutually exclusive** modes selected via `ha_mode` in config:

- **BGP/ECMP (`ha_mode: bgp`)** - ExaBGP announces shared VIP(s); upstream router ECMPs across nodes.
- **Keepalived (`ha_mode: keepalived`)** - one VIP per node with VRRP failover; DNS round-robin (managed by the user, out of scope) spreads clients.

PyPI package `herr-ober`, CLI command `ober`, Python 3.12+, targets Ubuntu, Debian, RHEL 10+, Amazon Linux 2027. Deep internals (kernel tuning, failure recovery) are in `architecture.md`.

## Development Commands

```bash
pip install -e ".[dev]"                              # dev deps
pytest                                               # all tests (-v --tb=short is default)
pytest tests/test_keepalived.py                      # one file
pytest tests/test_cli.py::test_bootstrap -v          # one test
pytest --cov=ober --cov-report=term-missing          # coverage (min 50%)
ruff check . --fix && ruff format .                  # lint + format (line length 100)
mypy ober/                                           # strict: disallow_untyped_defs
pip install -e ".[docs]" && zensical serve           # docs at :8000; CI runs zensical build --clean --strict
```

The docs site (`zensical.toml`, `docs/`, `.github/workflows/docs.yml`) includes `architecture.md`, `deploy/sentinel/README.md` and the README part between the `operations` snippet markers (`docs/operations.md`) through `pymdownx.snippets`. Keep the markers, and keep links in those parts absolute or inside the included text.

CI (`.github/workflows/test.yml`, Python 3.12, 3.13 and 3.14) runs `ruff check .`, **`ruff format --check .`**, `mypy ober/`, and pytest. Run all four before pushing. Publishing to PyPI is via `publish-pypi.yml`; the version lives in both `pyproject.toml` and `ober/__init__.py`.

`issue-auto-fix.yml` runs Claude Code headlessly when a user with write access adds the label `auto-fix` to an issue (never on a plain new issue), creates a `fix/issue-<n>` branch and PR, and closes the issue. With repo var `ISSUE_AUTO_FIX_MERGE=yes` it only turns on GitHub auto-merge, and only when `main` is protected (required CI checks and a review must be set there). Issue text reaches shell steps only through env vars and files, never `${{ }}`.

## Code Architecture

**CLI (`ober/cli.py`)**: Click group `main` builds a `Context` (verbose/quiet/json_output, `config`, `system: SystemInfo`) and registers subcommands from `ober/commands/` (one file per command; `start`/`stop`/`restart` share `service.py`). Commands use `@click.pass_context` and read the shared object via `ctx.obj` (may be `None`, so they fall back to `SystemInfo()`). The `pass_context` helper in `cli.py` is not used by commands. Most commands also call `OberConfig.load()` themselves instead of using `ctx.obj.config`.

**Config (`ober/config.py`)**: Dataclasses `OberConfig` → `BGPConfig`, `KeepalivedConfig`, `VIPConfig` (list, with `exposure: internal|internet`), `BackendConfig` (list), `CertConfig`, `SecurityConfig`, `WatchConfig`, `SentinelConfig`, `MDEConfig`. Loading and saving are generic over the dataclass fields, so **a new field only needs the dataclass** (with a type annotation: `validate()` checks types from it; add enum/format checks to `_check_values`). `load()` never raises for bad content (so `stop`/`status`/`logs` work); it records syntax errors and unknown keys, and `validate()` returns `(errors, warnings)`. `config`, `start` and `test` refuse an invalid file (`exit_if_invalid`). `local_ip` overrides the detected node IP (`node_ip()`). `save()` writes through `write_atomic()` (tmp file, fsync, rename), which `maintenance.py` and `health.py` also use.

File paths are properties derived from `install_path` (`config_path`, `haproxy_config_path`, `bgp_config_path`, `keepalived_config_path`, `certs_path`, `blocklist_path`, `admin_socket_path`, `watch_data_dir`). Secrets, including Entra ID client secrets for Sentinel/MDE, go to `~/.ober/login` (mode 600) via `load_secrets()`/`save_secrets()`.

**System (`ober/system.py`)**: `SystemInfo` detects OS family (DEBIAN/RHEL; Amazon Linux, `ID=amzn`, is RHEL with `os_id == "amzn"` and only version 2027; 2023 ships python3 3.9), version, root, hostname, local IP. `ServiceInfo.from_service_name()` wraps systemd. Shell calls go through `run_command()`.

**Lifecycle split**:
- `bootstrap.py` runs once per node as root. It installs `haproxy` + `keepalived` + `frr` (only bfdd is used) from OS packages (Ubuntu adds `ppa:vbernat/haproxy-3.3`), installs **ExaBGP via pip into the venv** (`python -m pip`, since pipx venvs lack `pip`), pinned to `EXABGP_SPEC` (`exabgp>=4.2,<5`, the same range as the `[bgp]` extra), disables the distro `haproxy`/`keepalived`/`frr` units, writes sysctl tuning (incl. conntrack limits), the watchdog drop-in `/etc/systemd/system.conf.d/90-herr-ober-watchdog.conf`, on SELinux hosts the `herr_ober` policy module (`SELINUX_MODULE`, built with `checkmodule`; it labels `<install>/var/run` `ober_state_t`; fallback: permissive `keepalived_t`), the firewall only with `--firewall` (ufw or firewalld: SSH first, 443/tcp, VRRP, BFD 3784/udp; an already active firewall gets all but SSH), and writes **all three** HA-path systemd units (`ober-http`, `ober-bgp`, `ober-ha`, plus `ober-bfd` when FRR is installed) from string templates at the top of the file, plus the `ober-watch`/`ober-blocklist` units from `watch/units.py`. The watch and blocklist units are enabled only when needed.
- `config.py` is the interactive wizard (`inquirer`). HA mode is asked first and gates the BGP vs keepalived questions. `_apply_configuration()` saves YAML and generates `haproxy.cfg` plus either the ExaBGP config **and** the `lo-vip` dummy interface (netplan on Debian, nmcli on RHEL, and systemd-networkd files in `/etc/systemd/network/60-lo-vip.*` when that tool is missing, e.g. Amazon Linux), or `keepalived.conf` (keepalived manages VIPs itself, so a `lo-vip` left from BGP mode is removed with `_remove_vip_interface` from `uninstall.py`; the same happens when `vips` is empty). Generated files are only rewritten when their text changes. VIPs must be one IPv4 address, bare or `/32`; renderers use `_vip_host()` (bare IP) or `_vip_cidr()` (`ip/32`).
- `service.py`: `start` disables and stops the other mode's unit, then enables/starts `ober-http` and the unit for the current `ha_mode`. `restart` runs `haproxy -c` first and then `systemctl reload` (keepalived: `keepalived -t` and HUP). A failed check or reload exits 1 and never falls back to a restart. `restart --hard` is the full restart.
- `apply.py`: `ober apply` renders from ober.yaml (`_apply_configuration`), shows the diff, runs `haproxy -c` (and `keepalived -t`), reloads, and waits for 3 good `/health` checks (skipped when `/health` was already failing). Any failure writes the old files back and reloads again; `ober.yaml` keeps the edit. Each success stores the applied state (all of `<install>/etc` except certs and the blocklist) in `<install>/var/history/<timestamp>`; `ober rollback [ID]` restores the one before the newest. When servers added to or removed from a backend (IP addresses only; dynamic servers do not inherit `default-server`, so `server_plan()` repeats its options) are the only `haproxy.cfg` change, `_activate` runs them over the runtime API (`add server`/`enable health`/`enable server`; `set server ... state maint`, drain, `del server`) instead of a reload; any unexpected answer falls back to the reload. `lo-vip` network files are not covered. `_write_if_changed` writes through `write_atomic`.
- `backend.py`: `ober backend list|drain|maint|ready <server>` on the local admin socket. Server names come from `server_name()` in `config.py` (`rgw1:7480` -> `rgw1_7480`). `save_server_state()` writes `server_state_path` (HAProxy `server-state-file`) after each change and before `ober restart`/`stop`, so a drain survives a reload. `--all-nodes` then runs the same command on each other node over SSH (`cluster._ssh`), tries all, and exits 1 if any failed.
- `cluster.py`: `ober cluster status` reads `nodes:` (or, in keepalived mode, `local_ip` plus the peers), runs `ssh -o BatchMode=yes NODE sudo -n ober --json status` in parallel (this node in-process via `collect_status`), and adds cluster checks: each VIP announced or MASTER on at least one node (keepalived: exactly one), and drift of `config_hash` (sha256 of haproxy.cfg), HAProxy and ober versions. Exit 0/1/2 like `status --check`.
- `ober push` (also `cluster.py`) pipes this node's `ober.yaml` to `ssh NODE sudo -n ober apply -f -` for each other node, one at a time, and stops at the first failure. It refuses an `ober.yaml` that sets a `PER_NODE_KEYS` value. `ober cluster upgrade` runs the README "Rolling upgrade" runbook per node (other nodes over SSH, this node last with `sh -c`), stops at the first failure and leaves that node in maintenance. It uses `ober upgrade -y`. Per-node values live in `etc/ober.local.yaml`: `OberConfig.load()` deep-merges it over `ober.yaml` (`_merge`) and `save()` writes its keys back there (`_split`). `ober apply -f` validates the new file with this node's overlay and install path; on failure it restores the old `ober.yaml` too.
- `metrics.py`: `ober metrics [--output F]` renders `collect_status()` as Prometheus text (gauges only; `--output` writes atomically for the node_exporter textfile collector). The alert rules in `deploy/prometheus/alerts.yml` use these names, so keep them in sync; check with `promtool check rules` and `promtool test rules deploy/prometheus/alerts_test.yml` (podman image `prom/prometheus`).
- TLS backends: `backends[].tls`/`tls_verify`/`tls_ca_file` add `ssl verify required ca-file <file or @system-ca>` to each server line (`_server_tls` in `commands/config.py`); a server given by name also gets `sni str(host) check-sni host` and runtime DNS through `resolvers sys`. Host is never rewritten (SigV4). `health_check_expect` adds `http-check expect`.
- `cert.py`: `ober cert install PEM|-` checks key/cert match (`ssl.load_cert_chain`), expiry and SAN cover of `security.s3_hostnames` (warns without `*.host`), writes `certs.path` (or `<dir>/<name>`) with mode 600, then `set ssl cert` + `commit ssl cert` over the admin socket (`HAProxyRuntime.send(payload=...)`); unknown names fall back to `systemctl reload`. `--all-nodes` and `backend --all-nodes` share `cluster.run_on_other_nodes`. Renewal is outside ober: `deploy/lego/` (lego DNS-01 timer on one host, deploy hook pipes the PEM to `ober cert install --all-nodes -`; `tests/test_lego_hook.py`).
- **BFD (BGP mode)**: ExaBGP has no BFD. `_generate_bfdd_config()` writes `/etc/frr/bfdd.conf` (`BFDD_CONFIG_PATH`; bfdd reads only `<sysconfdir>/<daemon>.conf`, so not under `install_path`; `tests/conftest.py` redirects it) with one single-hop `peer <neighbor> local-address <bgp.local_address or node_ip>` per neighbor, timers `bgp.bfd_interval_ms`/`bfd_multiplier`. Only bfdd runs (unit `ober-bfd`, `BindsTo=` and `WantedBy=ober-bgp.service`), never the distro `frr.service` (it starts zebra). `OberConfig.bfd_active()` = bgp mode + `bfd_enabled` (default false) + neighbors + an FRR `bfdd` binary (`BFDD_PATHS`), so an old `bfd_enabled: true` does nothing until `frr` is installed. `sync_bfd_unit()` (from `_apply_configuration` as root, `ober start`, rollback) enables the unit, starts or restarts it only while ober-bgp runs (starting it alone would pull ober-bgp up), and disables it when inactive. Status/doctor read `vtysh -d bfdd -c 'show bfd peers json'`; a down peer is WARN; metric `ober_bfd_peer_up{peer}`, alert `OberBfdPeerDown`. Bootstrap installs `frr` and `--firewall` opens 3784/udp.
- `upgrade.py` re-renders the unit files through `bootstrap._create_systemd_services` (it returns the changed units), so nodes bootstrapped earlier get new templates. A changed `ober-http.service` needs one `ober restart --hard`.

### Install path resolution

`OberConfig()`'s default `install_path` (`_get_default_install_path()` in `ober/config.py`) resolves in this order:
1. `OBER_INSTALL_PATH` env var.
2. The pointer file `/etc/ober/install_path` (`INSTALL_POINTER_PATH`), written by bootstrap and removed by uninstall.
3. `sys.prefix` when running in a venv.
4. `<tempdir>/ober` (a fallback meant for tests).

This is how every later command, including the ExaBGP-spawned health process, finds a custom install. An autouse fixture in `tests/conftest.py` redirects the pointer so tests never read the real `/etc/ober`.

Bootstrap in a venv (pipx, the recommended setup) defaults to the existing pointer, then `sys.prefix`. Outside a venv, `sudo ober bootstrap <path>` is required. With a pipx venv, `venv_path` is saved separately (`_venv_path_override`) and the `bin/` and `venv/` dirs are not created.

`install.sh` installs pipx globally (`PIPX_HOME=/opt/pipx`, `PIPX_BIN_DIR=/usr/local/bin`), because the systemd units use `ProtectHome=true` and can't see `/root`. Bootstrap falls back to `ProtectHome=read-only` when the install or venv path is under `/root` or `/home`.

### BGP mode health check

`ober health` is **not run by users**. The generated ExaBGP config spawns it as `<venv>/bin/python -m ober.commands.health` (no VIP argument, so it covers all configured VIPs). It polls `http://127.0.0.1:<stats_port>/health` (HAProxy `monitor-uri`, default port 8404) and writes ExaBGP text-encoder commands to **stdout** (`announce|withdraw route <vip>/32 next-hop self`). Logs go to stderr. On SIGTERM/SIGINT it withdraws all routes before exiting. On SIGHUP it re-reads the VIPs from ober.yaml, withdraws removed ones and (while healthy) announces new ones; the VIP list is not in the ExaBGP config, so `ober apply`/`rollback` send `pkill -HUP -f ober[.]commands[.]health` in BGP mode. Never print anything else to stdout in that module.

### Keepalived mode

`_generate_keepalived_config()` in `commands/config.py` emits one `vrrp_instance` per VIP. Nodes are IPs (`[local_ip, *keepalived_peers(config, local_ip)]`; `keepalived_peers()` drops duplicates and the node itself, so one ober.yaml can list all nodes):
- `get_vip_owner()` uses `vips[].owner` when set (`validate()` requires `local_ip`/`node_ip()` or a peer; in BGP mode it only warns). Else it sorts the nodes and picks the owner by **md5** of the VIP (deterministic across nodes; never use Python's salted `hash()`). The owner gets priority 100, others 90. All instances start as `BACKUP` with preempt. `vrrp_startup_delay 30` makes a started node wait 30 s before it takes VIPs back; never use `preempt_delay`, which also makes backups keep a failing master for 30 s (seen in the AWS lab). `ober status` (`_expected_owner`) calls the same function, so status and the config agree.
- `vrrp_router_ids()` gives each VIP a unique `virtual_router_id`: over the sorted VIPs, md5(VIP) % 255 + 1 (`get_vrrp_router_id()`), or the next free ID on a collision.
- Unicast is the default (`unicast_src_ip` + `unicast_peer`); multicast is optional. No VRRP auth.
- `chk_haproxy` track_script curls `/health` (weight -50, fall 2, rise 15 with `init_fail`: down in about 4 s, back after 30 s healthy). No `track_interface`: keepalived already goes to FAULT when the instance interface is down. `global_defs` sets `enable_script_security` and `script_user root`.

### Security monitoring (`ober watch`, `ober/watch/`)

The full user guide is in `docs/security-monitoring.md`. Work is split so the data path never waits on analysis:
- **Data path (proxy nodes)**: `_generate_haproxy_config()` emits separate frontends for internal and internet VIPs. The internet frontend gets cheap early rejects, an allowlist, an admin-API deny, per-IP auto-ban counters, and conn limits. Both frontends reject sources in the `blocklist_path` ACL file. HAProxy sends one JSON log line per request over UDP syslog to `security.log_target`. **Controls here must be native HAProxy with per-request cost only.** The log field contract is `LOG_FIELDS` in `watch/models.py`, and the logs must never contain signatures, object keys, or query secrets (`tests/test_haproxy_live.py` checks this).
- **Analysis host**: `ober watch run` (`watch/daemon.py`) runs an asyncio receive loop: parse (`parser.py`, a hot path with no regex), then streaming rules (`rules.py`, LRU-bounded state), summaries, and the blocklist. Anything that blocks (the disk `store.py`, the Sentinel/MDE sinks) runs on worker threads behind bounded queues, and items are dropped and counted when a queue is full. Raw logs stay on local disk. Only findings and per-access-key summaries go to Sentinel. `server.py` serves the blocklist on `:8405`.
- **Proxy nodes pull** the blocklist through `ober blocklist sync` (systemd `ober-blocklist.timer`, `watch/sync.py`). It applies diffs live through the HAProxy runtime socket (`watch/runtime.py`) and falls back to updating only the file.
- The Sentinel/MDE clients are hand-rolled HTTP with no Azure SDK (`auth.py`, `sentinel.py`, `mde.py`). Azure-side DCR, table, and analytics-rule definitions are in `deploy/sentinel/`.
- Units come from `watch/units.py` (`write_units`), and bootstrap and `ober config` both call it. These units keep `ProtectHome=read-only` so root can read `~/.ober/login`. `ober upgrade` never rewrites unit files, so `config` adds a `RuntimeDirectory` drop-in to `ober-http.service` for nodes bootstrapped before 0.4.

### Systemd safety links

- `ober-http.service` runs HAProxy in master-worker mode (`-Ws`, `Type=notify`), like the distro unit. `ExecStartPre` and the first `ExecReload` run `haproxy -c`, so a bad config fails the start or reload and the running workers keep serving. `ExecReload` then sends `USR2`, which reloads (in `-D` mode it does nothing). Stdout and stderr go to the journal.
- `ober-bgp.service` and `ober-ha.service` both have `BindsTo=ober-http.service`. If HAProxy stops or crashes, systemd stops the HA unit at once: BGP routes are withdrawn or the VIP fails over.
- Both HA units also have `WantedBy=multi-user.target ober-http.service`. Enabling one creates a link in `ober-http.service.wants/`, so when systemd restarts a crashed HAProxy (`Restart=always`), the HA unit starts again. Without this it stays inactive. A deliberate `systemctl stop ober-http` still stops both. This is why `ober start` disables the other mode's unit: its link would start it too.
- Keep all of this when editing the unit templates. Health still gates the announcements.

## Conventions

- Strict mypy: full type annotations, Google-style docstrings.
- Exit codes 0/1. Root-only commands check `system.is_root` and `ctx.exit(1)`. `config --dry-run` works without root. Destructive ops (`uninstall`) confirm first.
- Slurm hostlists are accepted only by the wizard's keepalived peers prompt. The wizard's BGP neighbors, VIPs and backend servers are split on commas, so they take no hostlists. `ober sync` is deprecated and only prints where its lists now live in ober.yaml. Herr Ober has no hostlist dependency (`python-hostlist` was removed); the PyPI package literally named `hostlist` is unrelated, so don't add it. `_parse_hostlist` in `commands/config.py` is its own regex and handles one `[a-b]` range per item.

## Testing

Tests never touch the real system. Fixtures in `tests/conftest.py`: `cli_runner`, `temp_dir`, `temp_config`, `sample_config`, `mock_system_info` / `mock_root_system_info`, `mock_run_command`, `mock_check_command_exists`. Because commands import names directly, **patch at the use site**, e.g. `ober.cli.SystemInfo`, `ober.commands.service.OberConfig.load`, `ober.commands.service.run_command`, `ober.commands.status.ServiceInfo`, not `ober.system.*`. Integration tests use `moto[server]` as a mock S3 backend. `tests/test_haproxy_live.py` starts a real HAProxy on localhost (127.0.0.1 acts as the internet VIP, 127.0.0.2 as internal) and is skipped unless `haproxy` and `openssl` are installed.
