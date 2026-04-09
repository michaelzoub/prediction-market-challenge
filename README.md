# Orderbook Prediction Market Challenge

This repository contains a local-first trading challenge built around a single
FIFO limit order book for a binary `YES` contract.

Competitors implement a Python strategy that manages passive orders in a market
driven by:

- a latent fair-value process
- an informed arbitrageur
- uninformed retail market orders
- a static hidden-liquidity competitor

The contract settles to `1` if the latent score finishes above `0`, otherwise
it settles to `0`.

## Included Files

- `docs/orderbook_prediction_market_challenge.md`
  Challenge specification and rule summary.
- `docs/strategy_frontier_handoff.md`
  Practical strategy ranking, research takeaways, and optimizer brief.
- `orderbook_pm_challenge/`
  Local simulator, strategy API, CLI, and scoring implementation.
- `examples/starter_strategy.py`
  Minimal example strategy that uses the public participant API.
- `tests/`
  Coverage for book mechanics, simulation behavior, sandbox, and parallelism.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Install dependencies and create virtualenv
uv sync --dev

# Optional: install scientific computing libraries (numpy, scipy, pandas)
uv sync --extra scientific
```

## Run It

```bash
# Quick smoke run
uv run orderbook-pm run examples/starter_strategy.py --simulations 5 --steps 100

# Full JSON output
uv run orderbook-pm run examples/starter_strategy.py --json

# Parallel execution (4 workers)
uv run orderbook-pm run examples/starter_strategy.py --workers 4

# Sandboxed execution (restricted imports/builtins, nsjail if available)
uv run orderbook-pm run examples/starter_strategy.py --sandbox

# Both
uv run orderbook-pm run examples/starter_strategy.py --sandbox --workers 4

# Run tests
uv run pytest
```

## Main Entry Points

- `orderbook_pm_challenge/market.py`
- `orderbook_pm_challenge/engine.py`
- `orderbook_pm_challenge/runner.py`
- `orderbook_pm_challenge/cli.py`
