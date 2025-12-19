# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Herr Ober ("Head Waiter") is a high-performance S3 ingress controller for Ceph RGW clusters. Uses HAProxy 3.3 (AWS-LC) for SSL offloading and ExaBGP for Layer 3 HA via BGP/ECMP.

- **PyPI package:** `ober`
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

## Project Structure

```
ober/
├── ober/              # Main package (flat layout)
│   ├── __init__.py
│   ├── cli.py         # Click commands
│   └── ...
├── tests/
├── pyproject.toml
└── ...
```

## CLI Commands

Single `ober` command with subcommands. See README.md for usage details.

Core commands: `bootstrap`, `config`, `sync`, `status`, `start/stop/restart`, `health`, `logs`, `doctor`, `test`, `upgrade`, `uninstall`

Global flags: `--version`, `--json`, `-q/--quiet`, `-v/--verbose`

## Key Implementation Notes

### Code Style
- Type annotations required throughout
- Google-style docstrings
- Linting/formatting via ruff (no pre-commit hooks)

### CLI Behavior
- Exit codes: 0 success, 1 error
- Uses `click` framework, `rich` for output, `python-inquirer` for prompts
- Destructive ops (`uninstall`) require confirmation
- Error messages should suggest fixes

### Configuration
- Format: YAML at `<install-path>/etc/ober.yaml`
- Secrets stored separately in `~/.ober/login` (permissions 600)
- Supports Slurm hostlists for node/router lists

### Testing Strategy
- Unit tests with mocked system calls
- Integration tests use `moto[server]` for mock S3 backends
- BGP-related code unit tested with mocked ExaBGP (can't test real BGP on single host)
- Minimum coverage: 50%

### Key Paths (default installation)
- `/opt/ober/etc/ober.yaml` - Main config
- `/opt/ober/etc/haproxy/haproxy.cfg` - HAProxy config
- `/opt/ober/etc/bgp/config.ini` - ExaBGP config
- `/opt/ober/etc/certs/` - SSL certificates

### Systemd Services
- `ober-http.service` - HAProxy
- `ober-bgp.service` - ExaBGP (bound to ober-http)

## Dependencies

Runtime: `click`, `python-inquirer`, `rich`, `hostlist`, `requests`, `pyyaml`, `watchdog`

Dev: `pytest`, `pytest-cov`, `moto[server]`, `boto3`, `ruff`, `mypy`
