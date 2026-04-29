# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ABSOLUTE RULES (never override)

1. **Paper trading is the default.** Real orders require ALL THREE gates open:
   - `AURAMAUR_LIVE=true` environment variable
   - `execution.live=true` in config
   - `dry_run=False` per-order flag
   The single source of truth is `Settings.is_live` in `config/settings.py`, which also fails closed when `KILL_SWITCH` exists.
2. **Kill switch**: If a `KILL_SWITCH` file exists in CWD, halt ALL trading immediately. Use `auramaur kill` / `auramaur unkill` to toggle.
3. **On-chain redemption has its own fourth gate**: `AURAMAUR_ENABLE_REDEMPTION=true`. Live trading does NOT auto-enable redemption.
4. **Never bypass risk checks.** All 15 checks in `auramaur/risk/checks.py` must pass via `RiskManager.evaluate()` before any order.
5. **Never hardcode API keys.** All secrets come from environment variables. The repo loads `.env` from the project root regardless of CWD (`config/settings.py` resolves it absolutely).
6. **Never read `.env` files.** They contain secrets. Use `.env.example` for reference.
7. **Never force-push to main.**

## Common commands

```bash
# Install (uv-managed)
uv sync

# Run the bot in paper mode (default — kill_switch absent, gates closed)
uv run auramaur run --agent
uv run auramaur run --exchange polymarket   # isolate to one exchange

# Read-only Rich dashboard against the live SQLite DB
uv run auramaur dashboard

# Quick utilities
uv run auramaur status
uv run auramaur scan "<query>"        # search Polymarket via Gamma
uv run auramaur backtest --days 30    # backtest against resolved signals
uv run auramaur backtest --compare    # A/B conservative vs aggressive
uv run auramaur redeem-check          # list redeemable Polymarket positions (read-only)
uv run auramaur redeem                # build+sign redemption Safe txs (DRY-RUN by default)
uv run auramaur redeem --submit       # broadcast — requires all 4 gates open

# Tests (pytest-asyncio auto mode)
uv run pytest                          # full suite
uv run pytest tests/test_risk_checks.py
uv run pytest tests/test_triple_gate.py::test_name -v
uv run pytest -k "kelly or kalshi"     # by keyword
uv run pytest --cov=auramaur           # coverage

# Lint
uv run ruff check .
uv run ruff format .
```

## High-level architecture

Pipeline shape: **discovery → data aggregation → analysis (NLP) → signal detection → risk → allocation → execution → reconciliation → calibration**. Reading the code in that order is the fastest path to a mental model.

### Single-gateway invariants

- `auramaur/exchange/client.py::PolymarketClient.place_order` is the ONLY path that hits a real CLOB. It re-checks `KILL_SWITCH` and `is_live` on every order; it routes to `auramaur/exchange/paper.py::PaperTrader.execute` if any gate is closed.
- `auramaur/risk/manager.py::RiskManager.evaluate` runs all 15 checks plus regime-aware Kelly sizing (`risk/kelly.py`, `risk/regime.py`). No code path approves a trade without going through this method.
- `auramaur/risk/portfolio.py::PortfolioTracker` is the single read model for positions/PnL/drawdown/category exposure used by the risk gate.

### Swappable analysis layer

`auramaur/strategy/protocols.py::MarketAnalyzer` is a `typing.Protocol`. Three implementations exist:

- `nlp/strategic.py` — batched strategic analyzer (default, `analysis.mode = "strategic"`)
- `nlp/tool_use_analyzer.py` — Claude with web_search/web_fetch tools, refines top-edge markets
- `strategy/agent_analyzer.py` — full agentic mode (`auramaur run --agent` or `analysis.mode = "agent"`)

Everything downstream consumes `TradeCandidate(market, signal)` identically. When adding a new analyzer, implement the protocol — don't fork the engine.

`auramaur/exchange/protocols.py` defines parallel `MarketDiscovery` / `ExchangeClient` protocols. Polymarket, Kalshi, Crypto.com, IBKR, and `PaperTrader` all implement these — multi-exchange support and arbitrage rely on this abstraction.

### Orchestration

- `auramaur/bot.py::AuramaurBot` wires every component and runs concurrent tasks (engine cycle, news reactor, market maker, arbitrage scanner, resolution tracker, portfolio sync). It auto-acquires a DB slot via fcntl (`auramaur.db`, `auramaur_2.db`, … up to 19) so multiple bot instances can run side-by-side without colliding. Each instance's filter (`--exchange`) determines what it actually trades.
- `auramaur/strategy/engine.py::TradingEngine` runs one cycle: discovery → aggregator → analyzer → `detect_edge` → risk → allocator → router → exchange. It uses cross-instance file locks under `$TMPDIR/auramaur_claims/` to prevent two bots from claiming the same market.
- `auramaur/cli.py` is Click-based; entry point is `[project.scripts] auramaur = "auramaur.cli:main"` in `pyproject.toml`.

### Config loading

- `config/defaults.yaml` is the authoritative source for all numeric defaults (risk thresholds, Kelly fraction, intervals, intensity presets). DO NOT duplicate these values into code or docs — read from there.
- `config/settings.py::Settings` (pydantic-settings) merges `defaults.yaml` + `.env` + environment. `.env` is resolved relative to repo root, not CWD.
- `nlp.api_intensity` (`low` / `medium` / `full_blast`) is a preset that overrides individual NLP knobs only when they're at their `medium` defaults — explicit values win.

### Data sources

17+ async sources under `auramaur/data_sources/` (NewsAPI, Reddit, Twitter, FRED, RSS, web search, GDELT, Google Trends, Bluesky, Manifold, Metaculus, USGS, CoinGecko, HackerNews, ESPN, market data, polymarket context). Most are gated by API key presence; some are category-gated (`DataSource.categories`) so they only fire on relevant markets. The `Aggregator` fans out queries in parallel.

### Persistence & migrations

- SQLite via `aiosqlite`, WAL mode. Schema and migrations live in `auramaur/db/models.py` (`SCHEMA_VERSION`); migrations run automatically on connect.
- Paper and live state are tagged with `is_paper` per row. Dashboard/status filter by current mode so paper P&L doesn't leak into live views.

## Code style

- Python 3.11+, async-first (asyncio); `pytest-asyncio` is in `auto` mode.
- Type hints everywhere; Pydantic v2 models for all structured data and the `Settings`.
- structlog (JSON) for logging. Use string event names (`"risk.decision"`, `"order.live"`) and structured kwargs — never f-string-format the event.
- Ruff target `py311`, line length 100.
- Tests required for any change to risk checks, the triple-gate, paper trader, or anything that prepares an order.

## Risk defaults

The authoritative numbers live in `config/defaults.yaml`. Notable current values: `max_drawdown_pct=15`, `max_stake_per_market=25`, `daily_loss_limit=200`, `max_open_positions=500`, `min_edge_pct=3.5`, `kelly.fraction=0.30`, `confidence_floor="LOW"`, `category_exposure_cap_pct=60`, `second_opinion_divergence_max=0.25`. If you change defaults, update `defaults.yaml` AND the corresponding pydantic model defaults in `config/settings.py` together — the YAML overlays on the model.
