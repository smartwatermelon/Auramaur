# Auramaur

An autonomous trading bot for prediction markets (Polymarket and Kalshi),
built in six days by a hydrologist using Claude Code.

It makes (small) money.

## What it does

Connects to Polymarket and Kalshi. Scans markets, gathers news from RSS / web
search / Reddit / NewsAPI / FRED / Manifold / Metaculus, asks Claude to
estimate the probability of each outcome, compares against the market price,
and trades when there's edge after fees.

- **NLP analysis** with calibrated probability estimation (Platt scaling on
  resolution feedback) and adversarial second opinions
- **Risk management** with 15 independent checks per trade, geometric Kelly
  position sizing, drawdown limits, and category exposure caps
- **Multi-exchange** order routing with per-exchange fee adjustment
- **Position reconciliation** against on-chain CLOB trade history
- **Resolution tracking** that closes the loop into the calibration system

## Safety constraints (hard-coded)

1. Paper trading is the default. Live orders require **all three gates**:
   `AURAMAUR_LIVE=true`, `execution.live=true`, and per-order `dry_run=False`.
2. A `KILL_SWITCH` file in the working directory halts all trading.
3. Every order passes through 15 risk checks. None can be bypassed.
4. No API keys in code — all secrets come from environment variables.

## Quickstart

```bash
# Install (use --extra dev to also install pytest-asyncio so the test
# suite actually exercises async code; without it, async tests are
# silently treated as no-ops)
uv sync --extra dev

# Configure (copy and fill in)
cp .env.example .env

# Run in paper mode (default)
auramaur run --agent

# Run tests
uv run pytest
```

To go live: set `AURAMAUR_LIVE=true` in your environment, set
`execution.live: true` in `config/defaults.yaml`, and accept that you are
trading real money on prediction markets where most participants are now bots
running on the same handful of foundation models as yours.

## Architecture

```
auramaur/
├── exchange/         Polymarket CLOB, Kalshi, Crypto.com, paper trader
├── data_sources/     News, RSS, Reddit, FRED, Manifold, Metaculus
├── nlp/              Claude analyzer, calibration, prompts
├── risk/             15-check pipeline, Kelly sizer, portfolio model
├── strategy/         Engine, signal detection, market selector, resolution
├── broker/           Allocator, syncer, reconciler, redeemer, PnL tracker
├── monitoring/       Display, attribution
└── db/               SQLite schema
```

The single gateway for all orders is `exchange/client.py`. Paper trading
interception happens in `exchange/paper.py`. The risk manager in
`risk/manager.py` is the only path through which trades can be approved.

## Status

Trades real money on Polymarket and Kalshi as of April 2026. P&L is
modest. Calibration is live and learning from each market resolution.

## License

MIT.
