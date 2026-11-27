# Development

```bash
# Clone and install dev dependencies
git clone https://github.com/dirkpetersen/herr-ober.git
cd herr-ober
pip install -e ".[dev]"

# Run tests
pytest

# Lint and format
ruff check .
ruff format .

# Type check
mypy ober/
```

CI (`.github/workflows/test.yml`, Python 3.12, 3.13 and 3.14) runs `ruff check .`,
`ruff format --check .`, `mypy ober/` and `pytest`. Run all four before pushing. Tests never
touch the real system; integration tests use `moto[server]` as a mock S3 backend. The minimum
coverage is 50% (`pytest --cov=ober --cov-report=term-missing`).

## Documentation

This site is built with [Zensical](https://zensical.org) from `zensical.toml` and the
Markdown files in `docs/`. Two pages include files from outside `docs/`:
[Architecture](architecture.md) shows `architecture.md` from the repository root, and
[Sentinel and Defender](sentinel.md) shows `deploy/sentinel/README.md`. Edit those files, not
the stubs in `docs/`.

```bash
pip install -e ".[docs]"
zensical serve                 # preview at http://localhost:8000 with live reload
zensical build --clean --strict  # build into site/, fail on warnings
```

The workflow `.github/workflows/docs.yml` builds the site on every push to `main` that touches
the docs and deploys it to GitHub Pages.
