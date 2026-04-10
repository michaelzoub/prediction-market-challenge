#!/usr/bin/env python3
"""Rank all strategy files in examples/ by mean edge across multiple seed starts."""

import json
import subprocess
import sys
import os
import glob


def evaluate(path: str, n_sims: int, workers: int, seed_start: int = 0) -> dict:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "orderbook_pm_challenge.cli", "run", path,
             "--simulations", str(n_sims), "--steps", "2000",
             "--workers", str(workers), "--seed-start", str(seed_start), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return {"mean_edge": -999, "error": r.stderr[:200]}
        data = json.loads(r.stdout)
        s = [x for x in data["simulation_results"] if not x["failed"]]
        if not s:
            return {"mean_edge": -999}
        n = len(s)
        return {
            "mean_edge": sum(x["total_edge"] for x in s) / n,
            "mean_retail": sum(x["retail_edge"] for x in s) / n,
            "mean_arb": sum(x["arb_edge"] for x in s) / n,
            "mean_fills": sum(x["fill_count"] for x in s) / n,
            "mean_qty": sum(x["traded_quantity"] for x in s) / n,
            "pos": sum(1 for x in s if x["total_edge"] > 0),
            "n": n,
        }
    except Exception as e:
        return {"mean_edge": -999, "error": str(e)[:200]}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--triage-sims", type=int, default=40)
    ap.add_argument("--full-sims", type=int, default=200)
    ap.add_argument("--promote-top", type=int, default=10)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seeds", type=str, default="0,200,400,600,800",
                    help="Comma-separated seed starts for full eval")
    a = ap.parse_args()

    strategies = sorted(glob.glob("examples/*strategy*.py"))
    seeds = [int(x) for x in a.seeds.split(",")]

    print(f"=== TRIAGE: {len(strategies)} strategies x {a.triage_sims} sims (seed=0) ===\n")

    triage = []
    for i, path in enumerate(strategies):
        name = os.path.basename(path)
        stats = evaluate(path, a.triage_sims, a.workers, seed_start=0)
        me = stats["mean_edge"]
        triage.append((me, path, name, stats))
        print(f"  [{i+1:2d}/{len(strategies)}] {me:+8.4f}  {name}")

    triage.sort(key=lambda x: x[0], reverse=True)

    print(f"\n{'='*80}")
    print(f"=== TRIAGE RANKING (top {a.promote_top} promoted) ===\n")
    for rank, (me, path, name, stats) in enumerate(triage[:a.promote_top]):
        print(f"  #{rank+1:2d}  {me:+8.4f}  ret={stats.get('mean_retail',0):+7.4f}  "
              f"arb={stats.get('mean_arb',0):+7.4f}  fills={stats.get('mean_fills',0):5.1f}  {name}")

    promoted = triage[:a.promote_top]

    print(f"\n{'='*80}")
    print(f"=== FULL EVALUATION: top {a.promote_top} x {a.full_sims} sims x {len(seeds)} seeds ===\n")

    full_results = []
    for rank, (triage_edge, path, name, _) in enumerate(promoted):
        seed_edges = []
        seed_details = []
        for seed in seeds:
            stats = evaluate(path, a.full_sims, a.workers, seed_start=seed)
            me = stats["mean_edge"]
            seed_edges.append(me)
            seed_details.append((seed, stats))

        avg_edge = sum(seed_edges) / len(seed_edges)
        best_seed_idx = seed_edges.index(max(seed_edges))
        best_seed = seeds[best_seed_idx]
        best_edge = max(seed_edges)
        worst_edge = min(seed_edges)

        full_results.append({
            "name": name, "path": path,
            "avg_edge": avg_edge,
            "best_edge": best_edge, "best_seed": best_seed,
            "worst_edge": worst_edge,
            "seed_edges": dict(zip(seeds, seed_edges)),
            "seed_details": seed_details,
            "triage_edge": triage_edge,
        })

        seed_str = "  ".join(f"s{s}={e:+7.4f}" for s, e in zip(seeds, seed_edges))
        print(f"  #{rank+1:2d}  avg={avg_edge:+8.4f}  best={best_edge:+8.4f}(s{best_seed})  worst={worst_edge:+8.4f}  {name}")
        print(f"       {seed_str}")

    full_results.sort(key=lambda x: x["avg_edge"], reverse=True)

    print(f"\n{'='*80}")
    print(f"=== FINAL RANKING (by avg edge across {len(seeds)} seed starts) ===\n")
    print(f"{'Rank':>4}  {'Avg Edge':>10}  {'Best Edge':>10}  {'Best Seed':>10}  {'Worst Edge':>11}  Strategy")
    print(f"{'----':>4}  {'--------':>10}  {'---------':>10}  {'---------':>10}  {'----------':>11}  --------")
    for rank, r in enumerate(full_results):
        print(f"  #{rank+1:2d}  {r['avg_edge']:+10.4f}  {r['best_edge']:+10.4f}  {r['best_seed']:>10}  {r['worst_edge']:+11.4f}  {r['name']}")

    print(f"\n{'='*80}")
    print(f"=== DETAILED SEED BREAKDOWN (top 5) ===\n")
    for rank, r in enumerate(full_results[:5]):
        print(f"  #{rank+1} {r['name']}")
        for seed, (s, stats) in zip(seeds, r["seed_details"]):
            me = stats["mean_edge"]
            print(f"     seed={s:4d}: edge={me:+8.4f}  ret={stats.get('mean_retail',0):+7.4f}  "
                  f"arb={stats.get('mean_arb',0):+7.4f}  fills={stats.get('mean_fills',0):5.1f}  "
                  f"pos={stats.get('pos',0)}/{stats.get('n',0)}")
        print()

    with open("tools/ranking_results.json", "w") as f:
        json.dump(full_results, f, indent=2)
    print(f"Full results saved to tools/ranking_results.json")


if __name__ == "__main__":
    main()
