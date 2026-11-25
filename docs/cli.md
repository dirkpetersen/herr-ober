# CLI reference

```text
ober [GLOBAL OPTIONS] COMMAND [ARGS]...
```

`ober --help` and `ober COMMAND --help` print the same information. Exit codes are `0` for
success and `1` for errors.

| Command | Purpose | Root |
|---------|---------|------|
| [`bootstrap`](#ober-bootstrap) | Install and set up everything on a node | yes |
| [`config`](#ober-config) | Interactive configuration wizard (BGP or keepalived mode) | yes, except `--dry-run` |
| [`sync`](#ober-sync) | Update external system whitelists | yes |
| [`status`](#ober-status) | Show the current state | no |
| [`start`, `stop`, `restart`](#ober-start-stop-restart) | Service management | yes |
| [`logs`](#ober-logs) | View service logs | no |
| [`doctor`](#ober-doctor) | Diagnostic checks | no |
| [`test`](#ober-test) | Test connectivity and configuration without starting services | no |
| [`upgrade`](#ober-upgrade) | Check for and install HAProxy and ExaBGP updates | yes, except `--check-only` |
| [`uninstall`](#ober-uninstall) | Clean removal | yes |
| [`watch`](#ober-watch) | Security log analysis daemon (analysis host) | - |
| [`blocklist`](#ober-blocklist) | Manage the shared IP blocklist | - |
| [`health`](#ober-health) | Health check process spawned by ExaBGP (not run by users) | - |

## Global options

| Option | Description |
|--------|-------------|
| `--version` | Show the ober version plus the installed HAProxy and ExaBGP versions |
| `--json` | JSON output for scripting |
| `-q`, `--quiet` | Minimal output |
| `-v`, `--verbose` | Verbose output |

Global options go before the command, for example `ober --json status`. JSON output is
supported by `status`, `doctor`, `test`, `upgrade`, `watch status` and the `blocklist`
commands.

## ober bootstrap

```text
ober bootstrap [OPTIONS] [PATH]
```

Installs HAProxy, keepalived and ExaBGP, applies kernel tuning, writes the systemd units and
initial configuration templates, configures the watchdog and opens the firewall ports. The
individual steps are listed in [Getting started](getting-started.md#3-bootstrap).

When `ober` runs from a venv (for example pipx), ExaBGP and the configuration go into that
venv, or into the path of an existing install pointer. Otherwise `PATH` is required, and a new
venv is created at `PATH/venv`.

| Option | Description |
|--------|-------------|
| `PATH` | Installation path (required when `ober` is not running in a venv) |
| `-y`, `--yes` | Skip confirmation prompts |

## ober config

```text
ober config [OPTIONS]
```

Interactive configuration wizard: HA mode, BGP or keepalived settings, VIPs, backends,
certificates and other settings. It can be run multiple times (idempotent). See
[Configuration](configuration.md).

| Option | Description |
|--------|-------------|
| `--dry-run` | Validate and preview changes without applying (no root needed) |

## ober sync

```text
ober sync [OPTIONS]
```

Updates the external system whitelists. Each option accepts Slurm hostlists or IP addresses,
comma-separated. The hosts are expanded and resolved to IPs, written one per line to
`<install_path>/etc/haproxy/<category>.lst`, and HAProxy is reloaded if it is running.
Without options, `ober sync` prompts for all categories; with one option it updates only that
category.

| Option | Description |
|--------|-------------|
| `--routers TEXT` | Switches and routers (`routers.lst`) |
| `--frontend-http TEXT` | Frontend systems allowed HTTP access (`frontend-http.lst`) |
| `--backend-http TEXT` | Backend S3/Ceph systems (`backend-http.lst`) |

```bash
ober sync --routers "switch[01-04]"
ober sync --frontend-http "weka[001-100]"
ober sync --backend-http "rgw[01-08].internal"
```

## ober status

```text
ober status
```

Shows the systemd state of `ober-http` and of the service for the current HA mode, the
HAProxy and ExaBGP or keepalived versions and whether their configuration files exist, the
VRRP state in keepalived mode (read from the journal), HAProxy stats from the stats port, and
the configured VIPs and backends. Use `ober --json status` for monitoring integration. The
HAProxy health endpoint can also be queried directly:

```bash
curl http://127.0.0.1:8404/health
```

## ober start, stop, restart

```text
ober start
ober stop [--force]
ober restart [--reload-only]
```

`ober start` enables and starts `ober-http` first, then `ober-bgp` (BGP mode) or `ober-ha`
(keepalived mode). If `watch.url` is set, it also enables the blocklist sync timer
`ober-blocklist.timer`.

`ober stop` shuts down gracefully: it stops the HA service first (BGP routes are withdrawn, or
the VIPs are released), waits 5 seconds for connections to drain, stops `ober-http`, and stops
`ober-blocklist.timer`.

`ober restart` restarts the services; `--reload-only` reloads only the HAProxy configuration
without downtime.

| Option | Description |
|--------|-------------|
| `stop --force` | Stop immediately, without the graceful shutdown |
| `restart --reload-only` | Only reload the HAProxy config (zero-downtime) |

## ober logs

```text
ober logs [OPTIONS]
```

Shows the systemd journal of the Ober services.

| Option | Default | Description |
|--------|---------|-------------|
| `-f`, `--follow` | off | Follow the log output in real time |
| `-n`, `--lines INTEGER` | `50` | Number of lines to show |
| `--service [http\|bgp\|ha\|all]` | `all` | `ober-http`, `ober-bgp`, `ober-ha`, or all three |

## ober doctor

```text
ober doctor
```

Runs diagnostic checks, before bootstrap (prerequisites) and after it (installation and
configuration): OS and Python version, root, HAProxy, ExaBGP and keepalived, the
configuration, the `ober-http`, `ober-bgp` and `ober-ha` services, and network tools.

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
ober upgrade [--check-only]
```

Checks for HAProxy and ExaBGP updates, shows what would be updated and asks for confirmation
before installing. Upgrade `ober` itself with pipx (`pipx upgrade herr-ober`, see
[Getting started](getting-started.md#2-install)).

| Option | Description |
|--------|-------------|
| `--check-only` | Only check for updates, don't install (no root needed) |

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
(`announce` or `withdraw route <vip>/32 next-hop self`) to stdout; logs go to stderr. On
SIGTERM or SIGINT it withdraws all routes before exiting.

| Option | Default | Description |
|--------|---------|-------------|
| `VIP` | all configured VIPs | VIP to announce or withdraw |
| `--interval FLOAT` | `1.0` | Health check interval in seconds |
| `--timeout FLOAT` | `2.0` | Health check timeout in seconds |
