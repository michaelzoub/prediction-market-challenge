# Strategy Summary & AI Optimization Guide

## Target Goal: +1000 Mean Edge

### Evaluation Protocol

```bash
# Quick triage (40-80 sims)
python3 -m orderbook_pm_challenge.cli run <strategy.py> --simulations 40 --steps 2000 --workers 4 --json

# Full evaluation (200 sims, leaderboard metric)
python3 -m orderbook_pm_challenge.cli run <strategy.py> --simulations 200 --steps 2000 --workers 4 --json
```

Report for each candidate:
- mean edge, mean retail edge, mean arb edge
- fill count, traded quantity, average abs inventory
- max/min edge, positive count / total

---

## Best Strategy Files (Ranked by 200-sim Mean Edge)

| Rank | File | Mean Edge | Retail | Arb | Notes |
|------|------|-----------|--------|-----|-------|
| 1 | `right_tail_spread5_harvester_v7_strategy.py` | **+2.672** | +3.943 | -1.271 | **NEW BEST** — optimized from 2-round parameter search |
| 2 | `right_tail_spread5_harvester_v1_strategy.py` | +1.934 | +3.224 | -1.290 | Previous best; spread>=5 two-sided |
| 3 | `right_tail_spread5_harvester_v4_strategy.py` | +1.125 | — | — | Local search best, signal-based one-sided |
| 4 | `right_tail_snipe_maker_v8_strategy.py` | +1.015 | — | — | Spread>=4 with tight inv control |

### Files/Approaches That Underperformed (Avoid as Base)

- `right_tail_snipe_maker_v12_strategy.py`: -1.733
- `right_tail_spread5_harvester_v6_strategy.py`: -1.664
- `right_tail_snipe_maker_v12_tuned_strategy.py`: -0.357
- Anything with burst sizing > 15 or inv_cap > 400
- One-sided-only strategies (v2, v3) underperform two-sided

---

## Simulator Mechanics (Critical for Optimization)

### Event Loop Per Step (order matters!)

1. **Competitor replenishment** — filled competitor orders spawn replacements
2. **Strategy `on_step(state)`** — participant places passive limit orders (before price changes!)
3. **`process.step()`** — latent score moves (diffusion + jumps); probability updates
4. **Arbitrage sweep** — informed trader sweeps all mispriced orders (arb fills = negative edge)
5. **Retail flow** — random Poisson orders (retail fills = positive edge when correctly placed)
6. **Edge recording** — edge = qty * (probability - price) for buys; qty * (price - probability) for sells

### Key Insight: Orders Are Placed Before Price Moves

Your orders are submitted at step N, then the probability changes, then arb and retail execute.
This means:
- **Retail** fills are typically positive because they execute against your limit at old-probability-implied prices
- **Arb** fills are negative because the arb has seen the NEW probability and sweeps mispriced quotes
- The game is maximizing the retail-fill-to-arb-fill quality ratio

### Parameter Variance Across Simulations

Each simulation randomizes:
- `initial_score`: [-0.75, 0.75]
- `jump_intensity`: [0.0008, 0.003]
- `jump_sigma`: [0.2, 0.6]
- `retail_arrival_rate`: [0.154, 0.352]
- `retail_mean_notional`: [2.64, 6.336]
- `competitor_quote_notional`: [24, 72]
- **`competitor_spread_ticks`: [1, 4]** ← spread>=5 strategies activate when competitor spread is wide enough

When `competitor_spread_ticks` is 1-3, the competitor book is tight. Wide spreads (5+) occur when
the competitor seeds the book with a wider ladder — these are the regimes where we can profitably
quote inside the spread.

---

## What Works (Pattern Analysis)

### 1. Wide-Spread Gating (spread >= 5)
- Only trade when the competitor book has a wide gap
- The `streak >= 2` filter demands persistence (avoids one-tick flickers)
- v7 uses spread >= 5 with streak >= 2

### 2. Multi-Layer Toxicity Gating
- **Fast toxicity** (`tox`): EWMA of |move| × fill_volume
- **Move volatility** (`abs_move`): EWMA of |mid change|
- **Hard gate**: High tox → cooldown (no quotes for N steps)
- **Soft gate**: Medium tox → skip this step's quotes
- **One-sided gate**: Mild tox → only quote against inventory (reduce two-sided exposure)

### 3. Center Pricing with Microstructure Adjustment
- Center = mid - α×move - β×flow - γ×inventory
- Place buy_tick = center - offset, sell_tick = center + offset
- Clamp to stay inside competitor bid/ask

### 4. Inventory Control
- Hard caps prevent excessive position buildup
- Size reduction when |inventory| > threshold
- One-sided quoting under mild toxicity

### 5. Budget Fraction
- Only risk a fraction of free_cash per step
- v7 uses 42.6% budget fraction (higher than v1's 34%)

---

## What Fails (Anti-Patterns)

1. **Over-aggressive burst sizing** (size > 15) → huge arb losses on adverse moves
2. **Too frequent trading** (spread >= 3 or no streak filter) → arb exposure in tight markets
3. **Loose inventory gates** (inv_cap > 400) → snowball losses
4. **Large inventory drift** without mean-reversion → settlement risk
5. **One-sided-only strategies** → miss half the retail flow
6. **Second level quotes in volatile markets** → extra arb exposure

---

## Key Differences: v7 vs v1 (Why v7 is Better)

| Parameter | v1 | v7 | Effect |
|-----------|----|----|--------|
| `ewma_flow` | 0.86 | 0.80 | **Faster flow response** — reacts quicker to fill imbalance |
| `ewma_tox` | 0.91 | 0.99 | **Much slower tox** — more patient, fewer false cooldowns |
| `soft_tox` | 0.55 | 0.74 | **Higher threshold** — quotes more aggressively in moderate tox |
| `hard_tox` | 1.0 | 1.17 | **Higher threshold** — fewer hard cooldowns |
| `center_flow_weight` | 0.35 | 0.416 | **Stronger flow lean** — adjusts center more for imbalance |
| `center_inv_weight` | 0.03 | 0.04 | **Slightly stronger inv mean-reversion** |
| `reduced_size` | 2.0 | 2.46 | **Slightly larger in high-inv regime** |
| `inv_cap` | 180 | 234 | **Higher cap** — allows more position accumulation |
| `budget_frac` | 0.34 | 0.426 | **More capital per step** — bigger effective size |

The dominant improvements are: slower tox EWMA (fewer false shutdowns), higher tox thresholds
(trade through more volatility), and larger budget fraction (bigger effective quotes).

---

## Milestone Targets for AI Optimizer

| Phase | Target | Status | Notes |
|-------|--------|--------|-------|
| 1 | Beat +1.934 (v1) | ✅ DONE | v7: +2.672 (+38% improvement) |
| 2 | Reach +5 | 🎯 Next | Likely needs structural changes, not just param tuning |
| 3 | Reach +20 | ⬜ | May need regime-conditional logic or multi-strategy |
| 4 | Reach +100 | ⬜ | Fundamentally different approach required |
| 5 | Reach +1000 | ⬜ | Requires exploiting simulator structure deeply |

---

## Search Directions to Prioritize (Ordered by Expected Impact)

### Tier 1: Likely Improvements (parameter tuning + small structural)
1. **Extended parameter search** around v7 params (tools/quick_sweep.py, tools/focused_sweep.py)
2. **Adaptive spread threshold**: allow spread >= 4 when other signals are very favorable
3. **Time-of-simulation conditioning**: different behavior early vs. late game
4. **Tox EWMA tuning**: the slow-tox insight suggests even slower EWMAs might help

### Tier 2: Structural Enhancements
5. **Regime detection**: identify high-retail vs. high-arb regimes from fill patterns
6. **Multi-level quotes**: 2-3 levels with different sizes, placed conditionally
7. **Asymmetric offset**: buy offset != sell offset based on flow direction
8. **Dynamic sizing**: size proportional to spread width (wider spread = more room = bigger size)
9. **End-game strategy**: different behavior in last 200-400 steps

### Tier 3: Fundamental New Approaches
10. **Inventory settlement play**: deliberately build directional inventory when probability is extreme (< 0.1 or > 0.9) and settlement edge is large
11. **Competitor exploitation**: when competitor refills are predictable, anticipate the flow
12. **Volatility arbitrage**: in high-jump regimes, wider quotes; in low-vol, tighter quotes
13. **Probability estimation from mid**: infer true probability from book state, lean toward expected settlement

### Tier 4: Meta-Strategies
14. **Ensemble**: run multiple sub-strategies, aggregate decisions
15. **Bayesian parameter adaptation**: update strategy params online based on observed fill quality
16. **Genetic algorithm over strategy code**: not just params but code structure

---

## Tools Provided

### `tools/param_search.py`
Full-featured parameter search tool. Generates N random variants of the v1 architecture,
triages with small sim counts, promotes top K to full evaluation.

```bash
python3 tools/param_search.py --candidates 30 --triage-sims 40 --promote-top 5 --full-sims 200 --workers 4
```

### `tools/quick_sweep.py`
Faster, simpler sweep focused on the v1 parameter space. Generates, triages, and evaluates.

```bash
python3 tools/quick_sweep.py --n 40 --triage 40 --top 5 --full 200 --workers 4 --seed 42
```

### `tools/focused_sweep.py`
Focused search around a known-good parameter set (from sweep round 2 best).

```bash
python3 tools/focused_sweep.py --n 40 --triage 40 --top 5 --full 200 --workers 4 --seed 99
```

All tools output JSON results and can be chained:
1. Run `quick_sweep.py` to explore broadly
2. Take best params, update BEST dict in `focused_sweep.py`
3. Run `focused_sweep.py` for fine-tuning
4. Promote winner to a named strategy file
5. Validate with full 200-sim CLI run

---

## Architecture of the Spread-5 Harvester Strategy

```
on_step(state):
  1. UPDATE SIGNALS
     - mid = (bid + ask) / 2
     - move = mid - prev_mid
     - abs_move = EWMA(|move|)
     - flow = EWMA(buy_filled - sell_filled)
     - tox = EWMA(|move| × total_fills)
     - streak = consecutive steps with spread >= threshold

  2. SAFETY GATES
     - If cooling down → CancelAll, return
     - If hard tox/vol exceeded → CancelAll + cooldown, return
     - If spread too narrow or streak too short → CancelAll, return
     - If soft tox/vol exceeded → CancelAll, return

  3. COMPUTE QUOTES
     - center = mid - α×move - β×flow - γ×inventory
     - buy_px = clip(center - offset, inside competitor spread)
     - sell_px = clip(center + offset, inside competitor spread)

  4. SIZE & DIRECTION
     - base_size (reduced if inventory high)
     - quote_buy/sell based on inventory caps
     - one-sided only under moderate tox
     - budget cap: free_cash × budget_fraction

  5. PLACE ORDERS
     - PlaceOrder(BUY, buy_px, qty) if allowed
     - PlaceOrder(SELL, sell_px, qty) if allowed
```

---

## Edge Economics

For a single fill:
- **BUY fill edge** = qty × (post_probability - fill_price)
- **SELL fill edge** = qty × (fill_price - post_probability)

Retail fills tend to be positive because:
- We post at mid ± offset
- Retail hits our resting order at this price
- Post-probability hasn't moved much (retail is noise)
- So edge ≈ qty × |offset_from_probability|

Arb fills tend to be negative because:
- Arb sees the NEW probability (after process.step())
- If probability jumped, arb sweeps our quote at the OLD implied price
- Edge = qty × (new_prob - our_price), which is negative when arb buys our ask below new prob

**The fundamental trade-off**: wider offsets = fewer arb fills but also fewer retail fills.
The sweet spot is inside the competitor spread but not at exact mid.

---

## Concrete Next Steps for AI

1. **Run `tools/focused_sweep.py` with different seeds (--seed 300, 400, 500...)** to find even better params near v7
2. **Try spread >= 4 variants**: modify v7 to allow spread >= 4 with extra safety (higher tox thresholds, smaller size)
3. **Add adaptive sizing**: `size = base_size * (spread / 5.0)` — scale with available room
4. **End-game aggressiveness**: increase size or relax gates in last 300 steps when remaining risk is bounded
5. **Build a meta-optimizer**: evolutionary search over strategy code, not just parameters
