# CLI reference

```text
ober [GLOBAL OPTIONS] COMMAND [ARGS]...
```

`ober --help` and `ober COMMAND --help` print the same information. Exit codes are `0` for
success and `1` for errors.

| Command | Purpose | Root |
|---------|---------|------|
| [`bootstrap`](#ober-bootstrap) | Install and set up everything on a node | yes |
| [`config`](#ober-config) | Configuration wizard, or render `ober.yaml` without prompts | yes, except `--dry-run` |
| [`apply`, `rollback`](#ober-apply-rollback) | Apply `ober.yaml` with check, reload and health gate; go back | yes |
| [`push`](#ober-push) | `ober apply` on the other nodes, one at a time | SSH user needs `sudo -n ober` |
| [`cluster status`](#ober-cluster-status) | All nodes: VIP placement and drift | SSH user needs `sudo -n ober` |
| [`cluster upgrade`](#ober-cluster-upgrade) | Rolling upgrade, one node at a time | SSH user needs `sudo -n` |
| [`status`](#ober-status) | Show the current state | no |
| [`metrics`](#ober-metrics) | Node state in the Prometheus text format | for backend state |
| [`maintenance`](#ober-maintenance) | Take this node out of rotation | yes |
| [`backend`](#ober-backend) | Show, drain, maint or ready RGW servers | yes |
| [`cert install`](#ober-cert-install) | Check a certificate and swap it in without a reload | yes |
| [`start`, `stop`, `restart`](#ober-start-stop-restart) | Service management | yes |
| [`logs`](#ober-logs) | View service logs | no |
| [`doctor`](#ober-doctor) | Diagnostic checks | no |
| [`test`](#ober-test) | Test connectivity and configuration without starting services | no |
| [`upgrade`](#ober-upgrade) | Re-render the units, install HAProxy and ExaBGP updates | yes, except `--check-only` |
| [`uninstall`](#ober-uninstall) | Clean removal | yes |
| [`watch`](#ober-watch) | Security log analysis daemon (analysis host) | - |
| [`blocklist`](#ober-blocklist) | Manage the shared IP blocklist | - |
| [`health`](#ober-health) | Health check process spawned by ExaBGP (not run by users) | - |
| [`sync`](#ober-sync) | Deprecated, changes nothing | - |

## Global options

| Option | Description |
|--------|-------------|
| `--version` | Show the ober version plus the installed HAProxy and ExaBGP versions |
| `--json` | JSON output for scripting |
| `-q`, `--quiet` | Minimal output |
| `-v`, `--verbose` | Verbose output |

Global options go before the command, for example `ober --json status`. Every command also
accepts `--json` after its name (`ober status --json`).

## ober bootstrap

```text
ober bootstrap [OPTIONS] [PATH]
```

Installs HAProxy, keepalived and ExaBGP, applies kernel tuning, writes the systemd units and
initial configuration templates and configures the watchdog. It changes the firewall only with
`--firewall`. The individual steps are listed in [Getting started](getting-started.md#3-bootstrap).

When `ober` runs from a venv (for example pipx), ExaBGP and the configuration go into that
venv, or into the path of an existing install pointer. Otherwise `PATH` is required, and a new
venv is created at `PATH/venv`.

| Option | Description |
|--------|-------------|
| `PATH` | Installation path (required when `ober` is not running in a venv) |
| `-y`, `--yes` | Skip confirmation prompts |
| `--firewall` | Enable ufw or firewalld and allow only SSH, 443/tcp and VRRP |

## ober config

```text
ober config [OPTIONS]
```

Interactive configuration wizard: HA mode, BGP or keepalived settings, VIPs, backends,
certificates and other settings. Every prompt is prefilled from `ober.yaml`. It does not
reload anything. See [Configuration](configuration.md#the-configuration-wizard).

| Option | Description |
|--------|-------------|
| `--dry-run` | Validate and show a summary without applying (no root needed; without a TTY it uses `ober.yaml` as is) |
| `--non-interactive` | No prompts: validate the existing `ober.yaml` and render all configs from it |

## ober apply, rollback

```text
ober apply [--timeout SECONDS] [--diff | --no-diff] [-f FILE]
ober rollback [--list] [--timeout SECONDS] [SNAPSHOT]
```

`ober apply` renders the configs from `ober.yaml`, shows the diff, runs `haproxy -c` (and
`keepalived -t`), reloads and waits for 3 good `/health` checks. Any failure writes the old
files back and reloads again. `ober rollback` restores a stored state (default: the one before
the newest) with the same checks. When the only change in `haproxy.cfg` is servers added to or
removed from a backend (with IP addresses), `apply` uses the HAProxy runtime API instead of a
reload: `add server`, `enable health`, `enable server`, or `set server ... state maint`, a drain
of up to `--timeout` seconds and `del server`. Any unexpected answer falls back to the reload.
See
[Operations](operations.md#change-the-configuration-apply-and-rollback).

| Option | Default | Description |
|--------|---------|-------------|
| `apply --timeout` | `30` | Seconds to wait for `/health` |
| `apply --diff/--no-diff` | `--diff` | Show the changes |
| `apply -f FILE` | | Install this `ober.yaml` first (`-` = stdin); a failure restores the old one too |
| `rollback --list` | | List the snapshots, newest last |
| `rollback --timeout` | `30` | Seconds to wait for `/health` |

## ober push

```text
ober push [--nodes LIST] [--timeout SECONDS] [-y]
```

Runs `ssh NODE sudo -n ober apply -f -` with this node's `ober.yaml` on each other node, one
at a time, and stops at the first node that fails. Apply on this node first. It refuses an
`ober.yaml` that sets `local_ip`, `bgp.router_id` or `bgp.local_address` (put them in
`ober.local.yaml`). Certificates are not copied. See
[Operations](operations.md#one-oberyaml-for-the-whole-cluster).

| Option | Default | Description |
|--------|---------|-------------|
| `--nodes` | `nodes` in `ober.yaml` | Comma-separated nodes |
| `--timeout` | `30` | Seconds each node waits for `/health` |
| `-y`, `--yes` | | Do not ask for confirmation |

## ober cluster status

```text
ober cluster status [--nodes LIST] [--timeout SECONDS]
```

Reads every node with `ssh -o BatchMode=yes NODE sudo -n ober --json status` (this node
directly) and checks that each VIP is announced or MASTER on at least one node (keepalived:
exactly one), and that the HAProxy config hash, the HAProxy version and the ober version are
the same on all nodes. Exit 0 (OK), 1 (WARN) or 2 (CRIT), like `ober status --check`.

| Option | Default | Description |
|--------|---------|-------------|
| `--nodes` | `nodes` in `ober.yaml` (keepalived: `local_ip` plus the peers) | Comma-separated nodes |
| `--timeout` | `10` | SSH connect timeout in seconds |

## ober cluster upgrade

```text
ober cluster upgrade [--nodes LIST] [--command CMD] [--timeout SECONDS] [--dry-run] [-y]
```

Runs the [rolling upgrade](operations.md#rolling-upgrade) on each node, this node last. It
starts only when `ober cluster status` is OK. Per node: `ober maintenance on`, wait until the
node announces no route and is MASTER for no VIP, `sudo -n sh -c CMD`, `ober upgrade -y`,
`ober restart --hard` (BGP mode, or when `ober upgrade` asks for a restart), `ober doctor`,
`ober maintenance off`, and wait until `ober cluster status` is OK. It stops at the first
failure and leaves that node in maintenance.

| Option | Default | Description |
|--------|---------|-------------|
| `--nodes` | `nodes` in `ober.yaml` | Comma-separated nodes |
| `--command` | `PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade herr-ober` | Package upgrade command, run as root |
| `--timeout` | `300` | Seconds to wait for the drain and for cluster OK |
| `--dry-run` | | Show the steps, change nothing |
| `-y`, `--yes` | | Do not ask for confirmation |

## ober sync

Deprecated: it changes nothing, because the files it wrote were never read by HAProxy. It
prints where the lists now live in `ober.yaml`:

| Old option | `ober.yaml` field |
|------------|-------------------|
| `--routers` | `bgp.neighbors` (BGP mode) or `keepalived.peers` (keepalived mode) |
| `--frontend-http` | `security.internal_networks` (internal VIPs) or `security.allowlist` (Internet VIPs) |
| `--backend-http` | `backends[].servers` |

## ober status

```text
ober status [--check]
```

Shows the systemd state of the services, the VIPs (present, announced or VRRP state), backend
health, certificate expiry (warning under 21 days, critical under 7) and the versions. It works
with a missing or invalid `ober.yaml`. Use `ober --json status` for scripts.

| Option | Description |
|--------|-------------|
| `--check` | Print a one-line summary and exit 0 (OK), 1 (WARN) or 2 (CRIT) |

## ober metrics

```text
ober metrics [--output FILE]
```

Prints the node state in the Prometheus text format (gauges only): VIP announced or VRRP
MASTER, VIP present, maintenance, backend servers UP, certificate expiry, the config hash and
the versions. Run it as root to include the backend state. `--output` writes the file
atomically for the node_exporter textfile collector (name ending in `.prom`). The alert rules
are in `deploy/prometheus/alerts.yml`; see [Operations](operations.md#metrics-and-alerts).

## ober maintenance

```text
ober maintenance on|off|status
```

`on` takes this node out of rotation while HAProxy keeps serving: in BGP mode the health
process withdraws all routes, in keepalived mode the VRRP priority drops by 100 and the VIPs
move. `status` shows the flag and what the node still announces or owns.

## ober backend

```text
ober backend list
ober backend drain|maint|ready SERVER [--all-nodes]
```

Works on the local HAProxy admin socket. `SERVER` is `host:port` or the HAProxy server name
(`rgw07:7480` is `rgw07_7480`). The state is saved to the HAProxy server-state file, so it
survives a reload.

| Command | Description |
|---------|-------------|
| `list` | State, check result and sessions of each server |
| `drain SERVER` | No new requests; running ones finish |
| `maint SERVER` | Out at once: no health checks, no new connections |
| `ready SERVER` | Back in service after `drain` or `maint` |
| `--all-nodes` | Also on every other node in `nodes:` over SSH; exit 1 if one failed |

## ober cert install

```text
ober cert install PEM [--all-nodes] [--name NAME]
```

Checks that the key matches the certificate, the expiry and that the SANs cover
`security.s3_hostnames` (a warning without `*.<host>`). Then it writes the file to `certs.path`
(mode 600) and swaps it into HAProxy over the admin socket, without a reload. `PEM` is a file
with certificate and key, or `-` for stdin. See [Operations](operations.md#certificates).

| Option | Description |
|--------|-------------|
| `--all-nodes` | Also on every other node in `nodes:`, over SSH |
| `--name NAME` | File name in a `certs.path` directory (default: the PEM's name) |

## ober start, stop, restart

```text
ober start
ober stop [--force]
ober restart [--hard]
```

`ober start` disables and stops the unit of the other HA mode, then enables and starts
`ober-http` and `ober-bgp` (BGP mode) or `ober-ha` (keepalived mode). If `watch.url` is set,
it also enables the blocklist sync timer `ober-blocklist.timer`.

`ober stop` shuts down gracefully: it turns maintenance on, waits until the node is out of
rotation, waits up to `drain_timeout` seconds (default 30) for open connections to close, and
then stops `ober-http`. `BindsTo=` stops the HA unit with it.

`ober restart` runs `haproxy -c` and then reloads HAProxy without downtime (in keepalived
mode also `keepalived -t` and a reload of keepalived). A failed check or reload exits 1 and
keeps the running processes.

| Option | Description |
|--------|-------------|
| `stop --force` | Stop immediately, without the graceful shutdown |
| `restart --hard` | Full restart: drops all connections and the BGP session or VIPs. Needed for a changed `ober-http.service` or a new ExaBGP |

## ober logs

```text
ober logs [OPTIONS]
```

Shows the systemd journal of the Ober services.

| Option | Default | Description |
|--------|---------|-------------|
| `-f`, `--follow` | off | Follow the log output in real time |
| `-n`, `--lines INTEGER` | `50` (all with `--since`) | Number of lines to show |
| `--service [http\|bgp\|bfd\|ha\|blocklist\|watch\|all]` | `all` | One service, or all |
| `--since TEXT` | | Entries since this time, for example `'1 hour ago'` |

## ober doctor

```text
ober doctor
```

Runs diagnostic checks, before bootstrap (prerequisites) and after it (installation and
configuration): OS and Python version, root, HAProxy (and its TLS library), ExaBGP or
keepalived, the configuration, the `ober-http` unit and the unit of the current HA mode, that
the other mode's unit and the distro `haproxy` and `keepalived` units are off, a pending
restart, the HAProxy syntax, the VIPs and the certificate.

## ober test

```text
ober test
```

Validates the configuration and tests connectivity without starting services:

- the HAProxy configuration;
- BGP mode: TCP port 179 on every BGP neighbor;
- keepalived mode: `keepalived -t` on the generated file, and a ping to every peer;
- a TCP connection to every backend server;
- the certificate in `certs.path`, if set.

## ober upgrade

```text
ober upgrade [--check-only] [-y]
```

Re-renders the systemd units (so nodes bootstrapped earlier get the new templates), checks for
HAProxy and ExaBGP updates, shows what would be updated and asks for confirmation before
installing. A changed `ober-http.service` or a new ExaBGP needs one `ober restart --hard`.
Upgrade `ober` itself with pipx first (`pipx upgrade herr-ober`). For a running cluster, see
the [rolling upgrade](operations.md#rolling-upgrade).

| Option | Description |
|--------|-------------|
| `--check-only` | Only check for updates, don't install (no root needed) |
| `-y`, `--yes` | Install updates without asking |

## ober uninstall

```text
ober uninstall [OPTIONS]
```

Removes the systemd services (`ober-http`, `ober-bgp`, `ober-ha`, `ober-watch`,
`ober-blocklist`), the kernel tuning file, the VIP interface configuration, the installation
directory, the install pointer, `~/.ober` (secrets), the `/usr/local/bin/ober` symlink and the
pipx package, if present. It lists what it will remove and asks for confirmation first. Run it
before retrying a bootstrap that failed midway.

| Option | Description |
|--------|-------------|
| `-y`, `--yes` | Skip the confirmation prompt |
| `--keep-config` | Keep the configuration: `<install_path>/etc/`, the install pointer and `~/.ober` stay |

## ober watch

```text
ober watch run [--store-raw | --no-store-raw]
ober watch status
```

Security log analysis on the analysis host: receives the HAProxy JSON logs over UDP syslog,
runs the rules, keeps the blocklist and ships findings to Sentinel and Defender for Endpoint.
See [Security monitoring](security-monitoring.md).

| Command | Description |
|---------|-------------|
| `watch run` | Run the analysis daemon in the foreground (in production, under systemd: `ober bootstrap` and `ober config` write `ober-watch.service`) |
| `watch run --store-raw/--no-store-raw` | Keep raw request logs on disk (default: on) |
| `watch status` | Show the counters of the running daemon |

## ober blocklist

```text
ober blocklist sync
ober blocklist list
ober blocklist add TARGET [--reason TEXT] [--ttl SECONDS]
ober blocklist remove TARGET
```

Manages the shared IP blocklist that `ober watch` keeps. Proxy nodes pull it and apply it to
the local HAProxy.

| Command | Description |
|---------|-------------|
| `blocklist sync` | Pull the blocklist from `ober watch` and apply it to the local HAProxy (run by `ober-blocklist.timer`) |
| `blocklist list` | List active blocks |
| `blocklist add TARGET` | Block an IP or CIDR. `--reason` (default `manual`), `--ttl` in seconds (omit for never) |
| `blocklist remove TARGET` | Unblock an IP or CIDR |

Reading needs `OBER_WATCH_TOKEN`; `add` and `remove` need `OBER_WATCH_ADMIN_TOKEN`
(see [Secrets](security-monitoring.md#6-secrets)).

## ober health

```text
ober health [OPTIONS] [VIP]
```

Not run by users. The generated ExaBGP configuration spawns it as
`<venv>/bin/python -m ober.commands.health`, without a VIP, so it covers all configured VIPs.
It polls `http://127.0.0.1:<stats_port>/health` and writes ExaBGP text-encoder commands
(`announce` or `withdraw route <vip>/32 next-hop self`) to stdout; logs go to stderr. Routes
are announced after `bgp.health_rise` good probes and withdrawn after `bgp.health_fall` failed
probes, or at once in maintenance mode. On SIGTERM or SIGINT it withdraws all routes before
exiting. On SIGHUP it reads the VIPs from `ober.yaml` again. The keepalived track script runs
it with `--once`.

| Option | Default | Description |
|--------|---------|-------------|
| `VIP` | all configured VIPs | VIP to announce or withdraw |
| `--interval FLOAT` | `1.0` | Health check interval in seconds |
| `--timeout FLOAT` | `2.0` | Health check timeout in seconds |
| `--once` | | Probe one time and exit 0 (healthy) or 1 |
| `--port INTEGER` | `stats_port` | HAProxy stats port |
