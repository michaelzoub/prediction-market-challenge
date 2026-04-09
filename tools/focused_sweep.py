#!/usr/bin/env python3
"""Focused sweep around best known params."""

from __future__ import annotations
import json, random, subprocess, sys, tempfile, os


def gen_strategy(p: dict) -> str:
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


def ev(path, n, w):
    r = subprocess.run(
        [sys.executable, "-m", "orderbook_pm_challenge.cli", "run", path,
         "--simulations", str(n), "--steps", "2000", "--workers", str(w), "--json"],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"mean_edge": -999}
    d = json.loads(r.stdout)
    s = [x for x in d["simulation_results"] if not x["failed"]]
    if not s:
        return {"mean_edge": -999}
    n = len(s)
    return {
        "mean_edge": sum(x["total_edge"] for x in s)/n,
        "mean_retail": sum(x["retail_edge"] for x in s)/n,
        "mean_arb": sum(x["arb_edge"] for x in s)/n,
        "mean_fills": sum(x["fill_count"] for x in s)/n,
        "pos": sum(1 for x in s if x["total_edge"] > 0), "n": n,
    }


BEST = {
    "ewma_am": 0.91, "ewma_fl": 0.75, "ewma_tx": 0.97,
    "h_tox": 1.0, "h_am": 0.7, "h_cool": 5,
    "s_tox": 0.74, "s_am": 0.52, "sp_min": 5, "str_min": 2,
    "c_mv": 0.20, "c_fl": 0.416, "c_inv": 0.040,
    "off": 2, "sz": 7.0, "inv_red": 80.0, "sz_red": 2.46,
    "inv_cap": 234.0, "os_tox": 0.32, "bfrac": 0.426,
}


def mutate(p, rng, mag=0.15):
    d = p.copy()
    for k in d:
        if rng.random() < 0.3:
            v = d[k]
            if isinstance(v, int):
                delta = max(1, int(abs(v) * mag))
                d[k] = v + rng.randint(-delta, delta)
            else:
                d[k] = v * (1 + rng.uniform(-mag, mag))
    d["ewma_am"] = max(0.85, min(0.97, d["ewma_am"]))
    d["ewma_fl"] = max(0.70, min(0.92, d["ewma_fl"]))
    d["ewma_tx"] = max(0.88, min(0.99, d["ewma_tx"]))
    d["h_cool"] = max(2, min(8, int(d["h_cool"])))
    d["sp_min"] = max(4, min(6, int(d["sp_min"])))
    d["str_min"] = max(1, min(4, int(d["str_min"])))
    d["off"] = max(1, min(4, int(d["off"])))
    d["bfrac"] = max(0.20, min(0.55, d["bfrac"]))
    d["sz"] = max(3.0, min(12.0, d["sz"]))
    d["sz_red"] = max(0.5, min(5.0, d["sz_red"]))
    d["inv_cap"] = max(100.0, min(400.0, d["inv_cap"]))
    d["os_tox"] = max(0.15, min(0.55, d["os_tox"]))
    d["inv_red"] = max(40.0, min(150.0, d["inv_red"]))
    d["s_tox"] = max(0.30, min(1.0, d["s_tox"]))
    d["s_am"] = max(0.25, min(0.80, d["s_am"]))
    d["c_mv"] = max(0.0, min(0.5, d["c_mv"]))
    d["c_fl"] = max(0.1, min(0.7, d["c_fl"]))
    d["c_inv"] = max(0.01, min(0.08, d["c_inv"]))
    return d


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--triage", type=int, default=40)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--full", type=int, default=200)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=99)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    td = tempfile.mkdtemp(prefix="fsweep_")

    cands = [(0, BEST)]  # always include baseline
    for i in range(1, a.n):
        mag = rng.uniform(0.05, 0.25)
        cands.append((i, mutate(BEST, rng, mag)))

    print(f"=== TRIAGE: {len(cands)} x {a.triage} sims ===")
    res = []
    for idx, (i, p) in enumerate(cands):
        path = os.path.join(td, f"c{i}.py")
        with open(path, "w") as f:
            f.write(gen_strategy(p))
        s = ev(path, a.triage, a.workers)
        me = s["mean_edge"]
        res.append((me, i, p, s, path))
        print(f"  [{idx+1}/{len(cands)}] #{i}: edge={me:.4f}  ret={s.get('mean_retail',0):.4f}  arb={s.get('mean_arb',0):.4f}")

    res.sort(key=lambda x: x[0], reverse=True)
    promoted = res[:a.top]

    print(f"\n=== FULL EVAL: top {a.top} x {a.full} sims ===")
    full = []
    for rank, (te, i, p, _, path) in enumerate(promoted):
        s = ev(path, a.full, a.workers)
        me = s["mean_edge"]
        full.append((me, i, p, s, path))
        print(f"  #{rank+1}: #{i} full={me:.4f} (tri={te:.4f})  ret={s.get('mean_retail',0):.4f} arb={s.get('mean_arb',0):.4f} pos={s.get('pos',0)}/{s.get('n',0)}")

    full.sort(key=lambda x: x[0], reverse=True)
    print(f"\n=== BEST: #{full[0][1]}, edge={full[0][0]:.4f} ===")
    print(json.dumps(full[0][2], indent=2))

    with open(os.path.join(td, "results.json"), "w") as f:
        json.dump([{"i": i, "e": e, "p": p, "s": s} for e, i, p, s, _ in full], f, indent=2)
    print(f"Saved to {td}/results.json")


if __name__ == "__main__":
    main()
