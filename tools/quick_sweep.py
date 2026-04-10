#!/usr/bin/env python3
"""Quick focused parameter sweep: generate N variants of v1, triage, report best."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import os


def gen_v1_variant(p: dict) -> str:
    return f"""from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    def __init__(self):
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
        return _q(min(target, max(0.0, yes_inv) + cash / max(0.01, 1.0 - px)))

    def on_step(self, state):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        fs = state.buy_filled_quantity + state.sell_filled_quantity
        fi = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = {p['ewma_am']:.4f} * self.abs_move + {1-p['ewma_am']:.4f} * abs(move)
        self.flow = {p['ewma_fl']:.4f} * self.flow + {1-p['ewma_fl']:.4f} * fi
        self.tox = {p['ewma_tx']:.4f} * self.tox + {1-p['ewma_tx']:.4f} * (abs(move) * fs)
        self.streak = self.streak + 1 if spread >= {p['sp_min']} else 0

        actions = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > {p['h_tox']:.4f} or self.abs_move > {p['h_am']:.4f}:
            self.cool = {p['h_cool']}
            return actions
        if spread < {p['sp_min']} or self.streak < {p['str_min']}:
            return actions
        if self.tox > {p['s_tox']:.4f} or self.abs_move > {p['s_am']:.4f}:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        center = int(round(mid - {p['c_mv']:.4f} * move - {p['c_fl']:.4f} * self.flow - {p['c_inv']:.4f} * inv))
        buy_tick = _clip(center - {p['off']})
        sell_tick = _clip(center + {p['off']})
        if buy_tick >= sell_tick:
            buy_tick = _clip(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip(max(bid + 1, min(ask, sell_tick)))

        size = {p['sz']:.3f}
        if abs(inv) > {p['inv_red']:.1f}:
            size = {p['sz_red']:.3f}

        qb = inv < {p['inv_cap']:.1f}
        qs = inv > {-p['inv_cap']:.1f}
        if self.tox > {p['os_tox']:.4f}:
            qb = inv < 0
            qs = inv > 0
            if inv == 0:
                qb = False
                qs = False

        budget = free_cash * {p['bfrac']:.4f}
        if qb:
            bq = self._buy_qty(buy_px, size, budget)
            if bq >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * {p['bfrac']:.4f}
        if qs:
            sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sq >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))
        return actions
"""


def eval_strategy(path: str, n_sims: int, workers: int) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "orderbook_pm_challenge.cli", "run", path,
         "--simulations", str(n_sims), "--steps", "2000", "--workers", str(workers), "--json"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        return {"mean_edge": -999, "error": result.stderr[:200]}
    data = json.loads(result.stdout)
    s = [r for r in data["simulation_results"] if not r["failed"]]
    if not s:
        return {"mean_edge": -999}
    n = len(s)
    return {
        "mean_edge": sum(r["total_edge"] for r in s) / n,
        "mean_retail": sum(r["retail_edge"] for r in s) / n,
        "mean_arb": sum(r["arb_edge"] for r in s) / n,
        "mean_fills": sum(r["fill_count"] for r in s) / n,
        "mean_qty": sum(r["traded_quantity"] for r in s) / n,
        "pos": sum(1 for r in s if r["total_edge"] > 0),
        "n": n,
    }


BASE_PARAMS = {
    "ewma_am": 0.91, "ewma_fl": 0.86, "ewma_tx": 0.91,
    "h_tox": 1.0, "h_am": 0.70, "h_cool": 5,
    "s_tox": 0.55, "s_am": 0.52,
    "sp_min": 5, "str_min": 2,
    "c_mv": 0.20, "c_fl": 0.35, "c_inv": 0.03,
    "off": 2, "sz": 7.0, "inv_red": 80.0, "sz_red": 2.0,
    "inv_cap": 180.0, "os_tox": 0.32, "bfrac": 0.34,
}


def mutate(params: dict, rng: random.Random, mag: float = 0.25) -> dict:
    p = params.copy()
    for k in p:
        if rng.random() < 0.35:
            v = p[k]
            if isinstance(v, int):
                delta = max(1, int(abs(v) * mag))
                p[k] = v + rng.randint(-delta, delta)
            else:
                p[k] = v * (1.0 + rng.uniform(-mag, mag))

    p["ewma_am"] = max(0.82, min(0.97, p["ewma_am"]))
    p["ewma_fl"] = max(0.75, min(0.95, p["ewma_fl"]))
    p["ewma_tx"] = max(0.82, min(0.97, p["ewma_tx"]))
    p["h_cool"] = max(2, min(8, int(p["h_cool"])))
    p["sp_min"] = max(4, min(7, int(p["sp_min"])))
    p["str_min"] = max(1, min(4, int(p["str_min"])))
    p["off"] = max(1, min(4, int(p["off"])))
    p["bfrac"] = max(0.15, min(0.55, p["bfrac"]))
    p["sz"] = max(2.0, min(15.0, p["sz"]))
    p["sz_red"] = max(0.5, min(5.0, p["sz_red"]))
    p["inv_cap"] = max(60.0, min(400.0, p["inv_cap"]))
    p["os_tox"] = max(0.10, min(0.65, p["os_tox"]))
    p["inv_red"] = max(30.0, min(200.0, p["inv_red"]))
    p["h_tox"] = max(0.5, min(2.0, p["h_tox"]))
    p["h_am"] = max(0.3, min(1.2, p["h_am"]))
    p["s_tox"] = max(0.2, min(1.0, p["s_tox"]))
    p["s_am"] = max(0.2, min(1.0, p["s_am"]))
    p["c_mv"] = max(0.0, min(0.6, p["c_mv"]))
    p["c_fl"] = max(0.0, min(0.8, p["c_fl"]))
    p["c_inv"] = max(0.0, min(0.10, p["c_inv"]))
    return p


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--triage", type=int, default=40)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--full", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    tmpdir = tempfile.mkdtemp(prefix="sweep_")

    candidates = []
    for i in range(args.n):
        mag = rng.uniform(0.10, 0.45)
        p = mutate(BASE_PARAMS, rng, mag)
        candidates.append((i, p))

    print(f"=== TRIAGE: {len(candidates)} candidates x {args.triage} sims ===")
    results = []
    for idx, (i, p) in enumerate(candidates):
        path = os.path.join(tmpdir, f"c{i}.py")
        with open(path, "w") as f:
            f.write(gen_v1_variant(p))
        stats = eval_strategy(path, args.triage, args.workers)
        me = stats["mean_edge"]
        results.append((me, i, p, stats, path))
        print(f"  [{idx+1}/{len(candidates)}] #{i}: edge={me:.4f}  ret={stats.get('mean_retail',0):.4f}  arb={stats.get('mean_arb',0):.4f}  fills={stats.get('mean_fills',0):.1f}")

    results.sort(key=lambda x: x[0], reverse=True)
    promoted = results[:args.top]

    print(f"\n=== TOP {args.top} ===")
    for rank, (e, i, p, s, path) in enumerate(promoted):
        print(f"  #{rank+1}: candidate {i}, triage edge={e:.4f}")

    print(f"\n=== FULL EVAL: top {args.top} x {args.full} sims ===")
    full = []
    for rank, (te, i, p, _, path) in enumerate(promoted):
        stats = eval_strategy(path, args.full, args.workers)
        me = stats["mean_edge"]
        full.append((me, i, p, stats, path))
        print(f"  #{rank+1}: #{i} full={me:.4f} (triage={te:.4f})  ret={stats.get('mean_retail',0):.4f} arb={stats.get('mean_arb',0):.4f} fills={stats.get('mean_fills',0):.1f} pos={stats.get('pos',0)}/{stats.get('n',0)}")

    full.sort(key=lambda x: x[0], reverse=True)
    print(f"\n=== BEST: candidate {full[0][1]}, edge={full[0][0]:.4f} ===")
    print(f"  Params: {json.dumps(full[0][2], indent=2)}")

    with open(os.path.join(tmpdir, "results.json"), "w") as f:
        json.dump([{"candidate": i, "edge": e, "params": p, "stats": s} for e, i, p, s, _ in full], f, indent=2)
    print(f"\nResults saved to {tmpdir}/results.json")


if __name__ == "__main__":
    main()
