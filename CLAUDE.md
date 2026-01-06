# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance S3 ingress controller for Ceph RGW clusters. Uses HAProxy 3.3 (AWS-LC) for SSL offloading and ExaBGP for Layer 3 HA via BGP/ECMP.

- **PyPI package:** `herr-ober` (CLI command: `ober`)
- **Python:** 3.12+ required
- **Supported OS:** Ubuntu, Debian, RHEL 10+
- **Target:** Proxmox VMs achieving 50GB/s+ throughput

## Development Commands

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run single test file
pytest tests/test_cli.py

# Run single test
pytest tests/test_cli.py::test_bootstrap -v

# Run with coverage
pytest --cov=ober --cov-report=term-missing

# Lint
ruff check .

# Auto-fix lint issues
ruff check . --fix

# Format
ruff format .

# Type check
mypy ober/
```

## Architecture

"Shared Nothing" cluster - each node operates independently. Nodes announce a shared VIP via BGP; upstream router uses ECMP to distribute traffic.

Per-node components:
- **HAProxy 3.3 (AWS-LC)** - SSL termination, ACLs, proxies to Ceph RGW backends
- **ExaBGP** - Announces VIP(s) to upstream router via BGP
- **ober CLI** - Python controller managing everything

Critical relationship: `ober-bgp.service` has `BindsTo=ober-http.service`. If HAProxy dies, BGP withdraws immediately.

## Code Architecture

**CLI Layer** (`ober/cli.py`):
- Click-based CLI with `@click.group()` main entry point
- `Context` class holds shared state (verbose, quiet, json_output, config)
- Commands registered via `main.add_command()` from `ober/commands/`

**Command Modules** (`ober/commands/`):
- Each subcommand in separate file: `bootstrap.py`, `config.py`, `status.py`, etc.
- Commands use `@pass_context` decorator to access shared `Context`
- Service commands (`start`, `stop`, `restart`) all in `service.py`

**Configuration** (`ober/config.py`):
- Dataclass-based: `OberConfig` contains `BGPConfig`, `VIPConfig`, `BackendConfig`, `CertConfig`
- `OberConfig.load()` searches default paths, `OberConfig.save()` writes YAML
- Properties compute derived paths (`config_path`, `haproxy_config_path`, etc.)
- Secrets handled separately via `load_secrets()`/`save_secrets()` for `~/.ober/login`

**System Utilities** (`ober/system.py`):
- `SystemInfo` dataclass auto-detects OS family (DEBIAN/RHEL), version, local IP
- `ServiceInfo` wraps systemd service queries
- Helper functions: `get_haproxy_version()`, `get_exabgp_version()`, `run_command()`

## Key Implementation Notes

### Code Style
- Type annotations required throughout (strict mypy config)
- Google-style docstrings
- Linting/formatting via ruff (line length 100)

### CLI Behavior
- Exit codes: 0 success, 1 error
- Uses `click` framework, `rich` for output, `python-inquirer` for prompts
- Destructive ops (`uninstall`) require confirmation

### Configuration
- Format: YAML at `<install-path>/etc/ober.yaml`
- Secrets stored separately in `~/.ober/login` (permissions 600)
- Supports Slurm hostlists for node/router lists

### Testing Strategy
- Unit tests with mocked system calls
- Integration tests use `moto[server]` for mock S3 backends
- BGP-related code unit tested with mocked ExaBGP
- Minimum coverage: 50%

### Testing Patterns
Key fixtures in `tests/conftest.py`:
- `cli_runner` - Click CLI test runner for testing commands
- `temp_dir` / `temp_config` - Temporary test environments with isolated directories
- `mock_system_info` - Mock `SystemInfo` to simulate different OS environments (Ubuntu/RHEL)
- `mock_root_system_info` - Same as above but with `is_root = True` for testing privileged operations
- `mock_run_command` - Mock system command execution to avoid actual shell calls
- `mock_check_command_exists` - Mock command availability checks
- `sample_config` - Pre-configured `OberConfig` with BGP, VIPs, backends, and certs for testing

Pattern: All tests use these fixtures to avoid real system calls. Mock the system detection, command execution, and file operations consistently.

### Health Check Mechanism
**CRITICAL:** The `ober health <vip>` command is NOT run directly by users. It's spawned by ExaBGP as a process.

How it works:
1. ExaBGP starts `ober health <vip>` as a subprocess (configured in `bgp/config.ini`)
2. The health command continuously polls HAProxy's health endpoint (`http://127.0.0.1:8404/health`)
3. It outputs BGP commands to **stdout** using ExaBGP's text encoder format:
   - `announce route <vip>/32 next-hop self` - when HAProxy is healthy
   - `withdraw route <vip>/32 next-hop self` - when HAProxy fails
4. ExaBGP reads these commands from stdout and updates BGP routes accordingly
5. On SIGTERM/SIGINT, the process gracefully withdraws all routes before exiting

The health check is the bridge between HAProxy's operational state and BGP route announcements. If HAProxy fails, routes are withdrawn within ~1-2 seconds.

### Path Resolution Logic
**IMPORTANT:** Ober auto-detects whether it's running in a virtual environment (venv/pipx) vs a custom installation. This affects ALL config and certificate paths.

Detection logic (`ober/commands/bootstrap.py`):
```python
def _is_in_venv():
    return sys.prefix != sys.base_prefix

def _get_current_venv_path():
    if _is_in_venv():
        return Path(sys.prefix)
    return None
```

Behavior:
- **If in venv (pipx recommended):** Automatically uses `sys.prefix` as install path
  - Example: `~/.local/pipx/venvs/herr-ober/` becomes the base for all config/certs
  - Bootstrap command: `sudo ober bootstrap` (no path required)
- **If NOT in venv:** Requires explicit install path
  - Bootstrap command: `sudo ober bootstrap /opt/ober`
  - All paths derived from this explicit location

All config paths are computed as properties in `OberConfig`:
- `config_path = install_path / "etc" / "ober.yaml"`
- `haproxy_config_path = install_path / "etc" / "haproxy" / "haproxy.cfg"`
- `bgp_config_path = install_path / "etc" / "bgp" / "config.ini"`
- `cert_dir = install_path / "etc" / "certs"`

**There are NO hardcoded default paths.** This ensures predictable behavior across different installation methods.

### Key Paths
With pipx (recommended):
- `~/.local/pipx/venvs/herr-ober/etc/ober.yaml` - Main config
- `~/.local/pipx/venvs/herr-ober/etc/haproxy/haproxy.cfg` - HAProxy config
- `~/.local/pipx/venvs/herr-ober/etc/bgp/config.ini` - ExaBGP config
- `~/.local/pipx/venvs/herr-ober/etc/certs/` - SSL certificates

With custom installation (prompted during bootstrap):
- `<install-path>/etc/ober.yaml` - Main config
- `<install-path>/etc/haproxy/haproxy.cfg` - HAProxy config
- `<install-path>/etc/bgp/config.ini` - ExaBGP config
- `<install-path>/etc/certs/` - SSL certificates

**Note:** There are NO hardcoded default paths. When not in a venv, bootstrap requires an explicit path:
```bash
sudo ober bootstrap /path/to/install
```

### Systemd Services
- `ober-http.service` - HAProxy
- `ober-bgp.service` - ExaBGP (bound to ober-http)
