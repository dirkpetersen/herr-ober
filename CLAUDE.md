# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance (50GB/s+) S3 ingress controller for Ceph RGW clusters on Proxmox VMs. HAProxy 3.3 (AWS-LC) does SSL offloading; HA is provided by one of two **mutually exclusive** modes selected via `ha_mode` in config:

- **BGP/ECMP (`ha_mode: bgp`)** - ExaBGP announces shared VIP(s); upstream router ECMPs across nodes.
- **Keepalived (`ha_mode: keepalived`)** - one VIP per node with VRRP failover; DNS round-robin (managed by the user, out of scope) spreads clients.

PyPI package `herr-ober`, CLI command `ober`, Python 3.12+, targets Ubuntu, Debian, RHEL 10+. Deep internals (kernel tuning, failure recovery) are in `architecture.md`.

## Development Commands

```bash
pip install -e ".[dev]"                              # dev deps
pytest                                               # all tests (-v --tb=short is default)
pytest tests/test_keepalived.py                      # one file
pytest tests/test_cli.py::test_bootstrap -v          # one test
pytest --cov=ober --cov-report=term-missing          # coverage (min 50%)
ruff check . --fix && ruff format .                  # lint + format (line length 100)
mypy ober/                                           # strict: disallow_untyped_defs
```

CI (`.github/workflows/test.yml`, Python 3.12 + 3.13) runs `ruff check .`, **`ruff format --check .`**, `mypy ober/`, and pytest. Run all four before pushing. Publishing to PyPI is via `publish-pypi.yml`; the version lives in both `pyproject.toml` and `ober/__init__.py`.

`issue-auto-fix.yml` runs Claude Code headlessly on every newly opened GitHub issue, creates a `fix/issue-<n>` branch and PR, and closes the issue (auto-merge when repo var `ISSUE_AUTO_FIX_MERGE=yes`).

## Code Architecture

**CLI (`ober/cli.py`)**: Click group `main` builds a `Context` (verbose/quiet/json_output, `config`, `system: SystemInfo`) and registers subcommands from `ober/commands/` (one file per command; `start`/`stop`/`restart` share `service.py`). Commands use `@click.pass_context` and read the shared object via `ctx.obj` (may be `None`, so they fall back to `SystemInfo()`). The `pass_context` helper in `cli.py` is not used by commands. Most commands also call `OberConfig.load()` themselves instead of using `ctx.obj.config`.

**Config (`ober/config.py`)**: Dataclasses `OberConfig` → `BGPConfig`, `KeepalivedConfig`, `VIPConfig` (list), `BackendConfig` (list), `CertConfig`. `_load_from_file()` and `save()` map each field by hand, so **a new config field must be added in the dataclass, `_load_from_file`, and `save`**. All file paths are properties derived from `install_path` (`config_path`, `haproxy_config_path`, `bgp_config_path`, `keepalived_config_path`, `certs_path`, `whitelist_path`, all under `<install_path>/etc/`). Secrets go to `~/.ober/login` (mode 600) via `load_secrets()`/`save_secrets()`.

**System (`ober/system.py`)**: `SystemInfo` detects OS family (DEBIAN/RHEL), version, root, hostname, local IP. `ServiceInfo.from_service_name()` wraps systemd. Shell calls go through `run_command()`.

**Lifecycle split**:
- `bootstrap.py` runs once per node as root. It installs `haproxy` + `keepalived` from OS packages (Ubuntu adds `ppa:vbernat/haproxy-3.3`), installs **ExaBGP via pip into the venv** (`python -m pip`, since pipx venvs lack `pip`), writes sysctl tuning, watchdog, firewall, and writes **all three** systemd units (`ober-http`, `ober-bgp`, `ober-ha`) from string templates at the top of the file.
- `config.py` is the interactive wizard (`inquirer`). HA mode is asked first and gates the BGP vs keepalived questions. `_apply_configuration()` saves YAML and generates `haproxy.cfg` plus either the ExaBGP config **and** the `lo-vip` dummy interface (netplan on Debian, nmcli on RHEL), or `keepalived.conf` (keepalived manages VIPs itself).
- `service.py` enables/starts `ober-http`, then only the service for the current `ha_mode`.

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

`ober health` is **not run by users**. The generated ExaBGP config spawns it as `<venv>/bin/python -m ober.commands.health` (no VIP argument, so it covers all configured VIPs). It polls `http://127.0.0.1:<stats_port>/health` (HAProxy `monitor-uri`, default port 8404) and writes ExaBGP text-encoder commands to **stdout** (`announce|withdraw route <vip>/32 next-hop self`). Logs go to stderr. On SIGTERM/SIGINT it withdraws all routes before exiting. Never print anything else to stdout in that module.

### Keepalived mode

`_generate_keepalived_config()` in `commands/config.py` emits one `vrrp_instance` per VIP. Nodes are IPs (`[local_ip] + keepalived.peers`):
- `get_vip_owner()` sorts the nodes and picks the owner by **md5** of the VIP (deterministic across nodes; never use Python's salted `hash()`). The owner gets priority 100, others 90. All instances start as `BACKUP` with preempt.
- `get_vrrp_router_id()` gives `virtual_router_id` = md5(VIP) % 255 + 1.
- Unicast is the default (`unicast_src_ip` + `unicast_peer`); multicast is optional. No VRRP auth.
- `chk_haproxy` track_script curls `/health` (weight -50, fall/rise 2), plus `track_interface` weight -50.

### Systemd safety links

`ober-bgp.service` and `ober-ha.service` both have `BindsTo=ober-http.service`. If HAProxy dies, BGP routes are withdrawn or the VIP fails over immediately. Keep this coupling when editing the unit templates.

## Conventions

- Strict mypy: full type annotations, Google-style docstrings.
- Exit codes 0/1. Root-only commands check `system.is_root` and `ctx.exit(1)`. `config --dry-run` works without root. Destructive ops (`uninstall`) confirm first.
- Node/router/peer inputs accept Slurm hostlists. `sync.py` uses the `python-hostlist` package (import name `hostlist`). The PyPI package literally named `hostlist` is unrelated, so don't depend on it. `_parse_hostlist` in `commands/config.py` is its own regex and handles one `[a-b]` range per item.

## Testing

Tests never touch the real system. Fixtures in `tests/conftest.py`: `cli_runner`, `temp_dir`, `temp_config`, `sample_config`, `mock_system_info` / `mock_root_system_info`, `mock_run_command`, `mock_check_command_exists`. Because commands import names directly, **patch at the use site**, e.g. `ober.cli.SystemInfo`, `ober.commands.service.OberConfig.load`, `ober.commands.service.run_command`, `ober.commands.status.ServiceInfo`, not `ober.system.*`. Integration tests use `moto[server]` as a mock S3 backend.
