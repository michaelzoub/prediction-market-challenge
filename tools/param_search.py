#!/usr/bin/env python3
"""Parameter search tool for strategy optimization.

Generates strategy variants with randomized parameters, tests them,
and reports the best-performing configurations.

Usage:
    python3 tools/param_search.py --base examples/right_tail_spread5_harvester_v1_strategy.py \
        --candidates 30 --triage-sims 40 --promote-top 5 --full-sims 200 --workers 4
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParamSet:
    ewma_abs_move: float = 0.91
    ewma_flow: float = 0.86
    ewma_tox: float = 0.91
    hard_tox: float = 1.0
    hard_abs_move: float = 0.70
    hard_cool: int = 5
    soft_tox: float = 0.55
    soft_abs_move: float = 0.52
    spread_min: int = 5
    streak_min: int = 2
    center_move_weight: float = 0.20
    center_flow_weight: float = 0.35
    center_inv_weight: float = 0.03
    quote_offset: int = 2
    base_size: float = 7.0
    inv_threshold_reduce: float = 80.0
    reduced_size: float = 2.0
    inv_cap: float = 180.0
    onesided_tox: float = 0.32
    budget_frac: float = 0.34

    def mutate(self, rng: random.Random, magnitude: float = 0.3) -> "ParamSet":
        """Return a mutated copy with some parameters randomly perturbed."""
        d = self.__dict__.copy()
        for key in d:
            if rng.random() < 0.4:
                val = d[key]
                if isinstance(val, int):
                    delta = max(1, int(abs(val) * magnitude))
                    d[key] = max(1, val + rng.randint(-delta, delta))
                elif isinstance(val, float):
                    d[key] = max(0.001, val * (1.0 + rng.uniform(-magnitude, magnitude)))
        d["ewma_abs_move"] = max(0.80, min(0.99, d["ewma_abs_move"]))
        d["ewma_flow"] = max(0.70, min(0.99, d["ewma_flow"]))
        d["ewma_tox"] = max(0.80, min(0.99, d["ewma_tox"]))
        d["hard_cool"] = max(1, min(10, d["hard_cool"]))
        d["spread_min"] = max(3, min(8, d["spread_min"]))
        d["streak_min"] = max(1, min(5, d["streak_min"]))
        d["quote_offset"] = max(1, min(5, d["quote_offset"]))
        d["budget_frac"] = max(0.10, min(0.60, d["budget_frac"]))
        d["base_size"] = max(1.0, min(20.0, d["base_size"]))
        d["inv_cap"] = max(50.0, min(500.0, d["inv_cap"]))
        d["onesided_tox"] = max(0.10, min(0.80, d["onesided_tox"]))
        return ParamSet(**d)


def generate_strategy_code(p: ParamSet) -> str:
    return textwrap.dedent(f"""\
        from __future__ import annotations

        from orderbook_pm_challenge.strategy import BaseStrategy
        from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


        def _clip(tick: int) -> int:
            return max(1, min(99, tick))


        def _q(value: float) -> float:
            return max(0.0, round(value, 2))


        class Strategy(BaseStrategy):
            \"\"\"Auto-generated parameter variant.\"\"\"

            def __init__(self) -> None:
                self.prev_mid = 50.0
                self.tox = 0.0
                self.abs_move = 0.0
                self.flow = 0.0
                self.streak = 0
                self.cool = 0

            def _buy_qty(self, tick, target, cash):
                return _q(min(target, cash / max(0.01, tick / 100.0)))

            def _sell_qty(self, tick, target, cash, yes_inv):
                px = tick / 100.0
                covered = max(0.0, yes_inv)
                uncovered = cash / max(0.01, 1.0 - px)
                return _q(min(target, covered + uncovered))

            def on_step(self, state):
                bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
                ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
                if ask <= bid:
                    ask = min(99, bid + 1)
                spread = ask - bid
                mid = 0.5 * (bid + ask)
                move = mid - self.prev_mid
                self.prev_mid = mid

                fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
                fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
                self.abs_move = {p.ewma_abs_move:.4f} * self.abs_move + {1 - p.ewma_abs_move:.4f} * abs(move)
                self.flow = {p.ewma_flow:.4f} * self.flow + {1 - p.ewma_flow:.4f} * fill_imb
                self.tox = {p.ewma_tox:.4f} * self.tox + {1 - p.ewma_tox:.4f} * (abs(move) * fill_sum)
                self.streak = self.streak + 1 if spread >= {p.spread_min} else 0

                actions = [CancelAll()]
                if self.cool > 0:
                    self.cool -= 1
                    return actions

                if self.tox > {p.hard_tox:.4f} or self.abs_move > {p.hard_abs_move:.4f}:
                    self.cool = {p.hard_cool}
                    return actions

                if spread < {p.spread_min} or self.streak < {p.streak_min}:
                    return actions
                if self.tox > {p.soft_tox:.4f} or self.abs_move > {p.soft_abs_move:.4f}:
                    return actions

                inv = state.yes_inventory - state.no_inventory
                free_cash = max(0.0, state.free_cash)
                center = int(round(mid - {p.center_move_weight:.4f} * move - {p.center_flow_weight:.4f} * self.flow - {p.center_inv_weight:.4f} * inv))
                buy_tick = _clip(center - {p.quote_offset})
                sell_tick = _clip(center + {p.quote_offset})
                if buy_tick >= sell_tick:
                    buy_tick = _clip(sell_tick - 1)
                if buy_tick >= sell_tick:
                    return actions

                buy_px = _clip(min(ask - 1, max(bid, buy_tick)))
                sell_px = _clip(max(bid + 1, min(ask, sell_tick)))

                size = {p.base_size:.3f}
                if abs(inv) > {p.inv_threshold_reduce:.1f}:
                    size = {p.reduced_size:.3f}

                quote_buy = inv < {p.inv_cap:.1f}
                quote_sell = inv > {-p.inv_cap:.1f}
                if self.tox > {p.onesided_tox:.4f}:
                    quote_buy = inv < 0
                    quote_sell = inv > 0
                    if inv == 0:
                        quote_buy = False
                        quote_sell = False

                budget = free_cash * {p.budget_frac:.4f}
                if quote_buy:
                    bq = self._buy_qty(buy_px, size, budget)
                    if bq >= 0.01 and buy_px < ask:
                        actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                        free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                        budget = free_cash * {p.budget_frac:.4f}

                if quote_sell:
                    sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
                    if sq >= 0.01 and bid < sell_px:
                        actions.append(PlaceOrder(Side.SELL, sell_px, sq))

                return actions
    """)


def evaluate_strategy(strategy_path: str, n_sims: int, workers: int) -> dict:
    """Run the strategy and return summary stats."""
    result = subprocess.run(
        [
            sys.executable, "-m", "orderbook_pm_challenge.cli", "run",
            strategy_path,
            "--simulations", str(n_sims),
            "--steps", "2000",
            "--workers", str(workers),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        return {"error": result.stderr[:500]}

    data = json.loads(result.stdout)
    successes = [r for r in data["simulation_results"] if not r["failed"]]
    if not successes:
        return {"error": "All simulations failed"}

    n = len(successes)
    edges = [r["total_edge"] for r in successes]
    retail = [r["retail_edge"] for r in successes]
    arb = [r["arb_edge"] for r in successes]
    fills = [r["fill_count"] for r in successes]
    qty = [r["traded_quantity"] for r in successes]
    abs_inv = [r["average_abs_inventory"] for r in successes]

    return {
        "n": n,
        "mean_edge": sum(edges) / n,
        "mean_retail": sum(retail) / n,
        "mean_arb": sum(arb) / n,
        "mean_fills": sum(fills) / n,
        "mean_qty": sum(qty) / n,
        "mean_abs_inv": sum(abs_inv) / n,
        "max_edge": max(edges),
        "min_edge": min(edges),
        "pos_count": sum(1 for e in edges if e > 0),
    }


def main():
    parser = argparse.ArgumentParser(description="Parameter search for strategy optimization")
    parser.add_argument("--candidates", type=int, default=30, help="Number of candidate variants to generate")
    parser.add_argument("--triage-sims", type=int, default=40, help="Simulations for triage phase")
    parser.add_argument("--promote-top", type=int, default=5, help="Number of top candidates to promote")
    parser.add_argument("--full-sims", type=int, default=200, help="Simulations for full evaluation")
    parser.add_argument("--workers", type=int, default=4, help="Worker processes")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output-dir", type=str, default="search_results", help="Output directory")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    base = ParamSet()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    candidates = []
    for i in range(args.candidates):
        mag = rng.uniform(0.1, 0.5)
        params = base.mutate(rng, magnitude=mag)
        candidates.append((i, params))

    print(f"=== TRIAGE PHASE: {len(candidates)} candidates x {args.triage_sims} sims ===")
    triage_results = []

    for idx, (i, params) in enumerate(candidates):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=str(output_dir)) as f:
            f.write(generate_strategy_code(params))
            tmp_path = f.name

        stats = evaluate_strategy(tmp_path, args.triage_sims, args.workers)
        edge = stats.get("mean_edge", -999)
        triage_results.append((edge, i, params, stats, tmp_path))
        print(f"  [{idx+1}/{len(candidates)}] candidate {i}: mean_edge={edge:.4f}  retail={stats.get('mean_retail', 0):.4f}  arb={stats.get('mean_arb', 0):.4f}")

    triage_results.sort(key=lambda x: x[0], reverse=True)
    print(f"\n=== TOP {args.promote_top} from triage ===")
    promoted = triage_results[:args.promote_top]
    for rank, (edge, i, params, stats, path) in enumerate(promoted):
        print(f"  #{rank+1}: candidate {i}, edge={edge:.4f}")
        print(f"    params: size={params.base_size:.2f}, spread>={params.spread_min}, streak>={params.streak_min}, "
              f"budget={params.budget_frac:.3f}, onesided_tox={params.onesided_tox:.3f}")

    print(f"\n=== FULL EVALUATION: top {args.promote_top} x {args.full_sims} sims ===")
    full_results = []
    for rank, (triage_edge, i, params, _, path) in enumerate(promoted):
        stats = evaluate_strategy(path, args.full_sims, args.workers)
        edge = stats.get("mean_edge", -999)
        full_results.append((edge, i, params, stats, path))
        print(f"  #{rank+1}: candidate {i}: full mean_edge={edge:.4f}  (triage was {triage_edge:.4f})")
        print(f"    retail={stats.get('mean_retail', 0):.4f}  arb={stats.get('mean_arb', 0):.4f}  "
              f"fills={stats.get('mean_fills', 0):.1f}  qty={stats.get('mean_qty', 0):.1f}  pos={stats.get('pos_count', 0)}/{stats.get('n', 0)}")

    full_results.sort(key=lambda x: x[0], reverse=True)
    print(f"\n=== FINAL RANKING (full {args.full_sims}-sim) ===")
    for rank, (edge, i, params, stats, path) in enumerate(full_results):
        print(f"  #{rank+1}: candidate {i}, mean_edge={edge:.4f}")
        print(f"    {params.__dict__}")

    best_edge, best_i, best_params, best_stats, best_path = full_results[0]
    result_file = output_dir / "best_result.json"
    with open(result_file, "w") as f:
        json.dump({
            "best_params": best_params.__dict__,
            "best_stats": best_stats,
            "all_results": [
                {"candidate": i, "params": p.__dict__, "stats": s}
                for _, i, p, s, _ in full_results
            ],
        }, f, indent=2)

    best_code = generate_strategy_code(best_params)
    best_strategy_file = output_dir / "best_strategy.py"
    with open(best_strategy_file, "w") as f:
        f.write(best_code)

    print(f"\nBest result saved to {result_file}")
    print(f"Best strategy saved to {best_strategy_file}")
    print(f"Best mean edge: {best_edge:.4f}")


if __name__ == "__main__":
    main()
