# CLAUDE.md - Agent Instructions for Schwab MCP

## Project Overview

Python 3.12+ MCP server exposing the Schwab trading API to LLM agents via the Model Context Protocol (FastMCP). Supports local CLI and Cloud Run deployment with Discord-based approval workflows for trading operations.

## Quick Reference

```bash
# Install dependencies (development)
uv sync --group dev --group ta

# Run tests
uv run pytest

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run pyright

# Run server locally
schwab-mcp server --client-id $SCHWAB_CLIENT_ID --client-secret $SCHWAB_CLIENT_SECRET
```

## Project Structure

```
schwab-mcp/
├── src/schwab_mcp/
│   ├── tools/              # MCP tools (the main API surface)
│   │   ├── account.py      # Account/position tools
│   │   ├── quotes.py       # Quote retrieval
│   │   ├── options.py      # Option chain queries
│   │   ├── orders.py       # Order placement/management
│   │   ├── history.py      # Price history
│   │   ├── transactions.py # Transaction history
│   │   ├── stored_options.py # Query stored option data
│   │   ├── order_helpers.py  # Order building utilities
│   │   ├── technical/      # Technical analysis tools (SMA, EMA, RSI, MACD, etc.)
│   │   ├── _registration.py # Tool registration system
│   │   ├── _protocols.py   # Protocol definitions
│   │   └── utils.py        # Async call wrapper, error handling
│   ├── admin/              # Admin web app
│   ├── approvals/          # Approval workflow (Discord)
│   ├── db/                 # Cloud SQL database layer
│   ├── remote/             # Cloud Run remote server (Starlette + OAuth)
│   ├── auth.py             # OAuth2 authentication
│   ├── cli.py              # CLI commands (auth, server, remote-server)
│   ├── context.py          # FastMCP context with typed accessors
│   ├── server.py           # MCP server setup
│   ├── resources.py        # Static reference data
│   └── tokens.py           # Token management & storage
├── tests/                  # pytest test suite (25 modules)
├── docs/                   # Documentation
├── sql/                    # SQL scripts
├── .github/workflows/
│   ├── ci.yml              # Lint + type check + tests
│   └── container.yml       # Multi-arch container builds
├── pyproject.toml          # Dependencies & tool configs
├── Containerfile           # OCI container (preferred)
└── uv.lock                 # Locked dependencies
```

## Code Conventions

- **Async throughout**: All tool implementations are `async def`
- **Type annotations**: Use `from __future__ import annotations`, `Annotated` for parameter docs
- **Naming**: `snake_case` functions, `PascalCase` classes, `SCREAMING_SNAKE_CASE` constants
- **Docstrings**: Brief one-liner on all public functions (used as MCP tool descriptions)
- **Error handling**: Custom `SchwabAPIError`, use `raise ... from ...` for chaining
- **Tool registration**: Standalone async functions registered via `register_tool(server, func, write=bool)`
- **Context injection**: `SchwabContext` parameter injected automatically by registration wrapper
- **Organization**: One tool category per file, shared utils in `utils.py`

## Testing

- **Framework**: pytest with coverage
- **Pattern**: Mock clients via `conftest.py` fixtures (`fake_call_factory`, `ctx_factory`)
- **Run**: `uv run pytest` (coverage report included by default)
- **Config**: `testpaths = ["tests"]`, `pythonpath = ["src"]`, branch coverage enabled

## CI Pipeline

1. `ruff check .` — linting
2. `pyright` — type checking
3. `pytest` — tests with coverage

Runs on push to main and all PRs.

## Dependencies

- Package manager: **uv**
- Build system: **hatchling**
- Key deps: `mcp`, `schwab-py` (git dep), `click`, `httpx`, `discord.py`, `anyio`
- Dev deps: `ruff`, `pyright`, `pytest`, `pytest-cov`
