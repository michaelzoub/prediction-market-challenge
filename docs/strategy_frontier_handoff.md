# Strategy Frontier Handoff

This note is a practical handoff for future strategy iteration on the orderbook
prediction market challenge.

Use it as the default briefing for any AI optimizer working on the repository.

## Scoring reminder

The simulator's primary metric is mean participant edge across simulations, not
terminal PnL. The user-facing stretch target is to reach `+$1000` of profit, but
within this repository the operational optimization target is:

- maximize mean edge on the official evaluation run
- while keeping arbitrage losses controlled

Current best known frontier is around `+1.93` mean edge on the 200x2000 setup,
so `+1000` should be treated as a stretch objective rather than a near-term
parameter tweak.

## Standard evaluation command

```bash
python3 -m orderbook_pm_challenge.cli run <file> --simulations 200 --steps 2000 --workers 4 --json
```

For triage:

```bash
python3 -m orderbook_pm_challenge.cli run <file> --simulations 40 --steps 2000 --workers 4 --json
```

## Best strategy files currently present in this repo

These files exist in `examples/` and should be treated as the main starting
points for new work.

### 1. `examples/right_tail_spread5_harvester_v1_strategy.py`

Known benchmark from prior runs:

- mean edge: `+1.934133` (current best overall from shared notes)
- mean retail edge: `+3.223993`
- mean arb edge: `-1.289860`

Why it matters:

- best known total edge so far
- trades mostly in wide-book windows only
- expresses the strongest current fit to the fill-quality vs fill-rate trade-off

Implementation traits:

- only activates when `spread >= 5`
- requires a short persistence streak before quoting
- uses rolling toxicity proxies from:
  - absolute mid move
  - fill imbalance
  - move magnitude times fill volume
- hard cooldown after severe toxicity
- soft gate that suppresses trading when toxicity is elevated
- center skew combines move, flow, and inventory
- becomes one-sided under moderate toxicity
- uses strict inventory caps and smaller size when inventory grows

Takeaway:

This is the baseline to beat. New candidates should usually inherit its
spread-5 gating and safety logic before trying to add more intelligence.

### 2. `examples/right_tail_spread5_harvester_v4_strategy.py`

Known benchmark from prior runs:

- mean edge: `+1.125415`

Why it matters:

- weaker than v1, but still a useful conservative reference
- demonstrates a more explicit directional gating style

Implementation traits:

- only activates when `spread >= 5`
- requires a longer streak than v1 before quoting
- uses a compact directional signal from move plus flow
- suppresses one side entirely when directional signal is strong
- uses smaller base sizing than v1
- keeps the same general toxicity/cooldown pattern

Takeaway:

Useful as a secondary comparison point when testing whether stronger direction
filtering helps or hurts conditional fill quality.

### 3. `examples/right_tail_snipe_maker_v8_strategy.py`

Known benchmark from prior runs:

- mean edge: `+1.015129`

Why it matters:

- respectable backup baseline
- shows what happens when entry expands beyond the stricter spread-5 regime

Implementation traits:

- activates at `spread >= 4`
- still uses toxicity and cooldown logic
- more aggressive entry frequency than the spread-5 harvesters
- inventory reduction cuts size quickly once position grows

Takeaway:

Good contrast case for the core lesson that more fills do not automatically
mean better edge. It tends to trade more than the best spread-5 approach.

## Important benchmark notes from prior research

The following files were referenced in prior benchmark summaries, but they are
not present in the current workspace. Keep these results in mind when planning
future work.

### High-value external benchmark notes

- `examples/research_spread5_hmm_hybrid_strategy.py`
  - mean edge: `+1.922359`
  - mean retail edge: `+3.159451`
  - mean arb edge: `-1.237092`
  - note: nearly tied with the current best; likely the strongest missing file
- `examples/research_spread5_hmm_hybrid_v2_strategy.py`
  - mean edge: `+1.241237`
  - mean retail edge: `+2.033992`
  - mean arb edge: `-0.792755`
  - note: more conservative, lower arb bleed
- `examples/research_vpin_style_spread5_strategy.py`
  - mean edge: `+0.263539`
  - mean retail edge: `+0.511521`
  - mean arb edge: `-0.247982`
  - note: valid research result, but not frontier competitive on its own

Interpretation:

- VPIN-style toxicity filtering seems directionally correct and still useful
- pure toxicity replacement strategies underperform the best execution style
- the most promising next step is a hybrid where VPIN-like filters gate an
  already strong spread-5 harvester, instead of replacing its core quoting logic

## Strategies and versions to avoid as starting points

Known weak baselines from prior runs:

- `right_tail_snipe_maker_v12_strategy.py`: `-1.733405`
- `right_tail_snipe_maker_v12_tuned_strategy.py`: `-0.356983`
- `right_tail_spread5_harvester_v6_strategy.py`: `-1.664160`
- `right_tail_spread5_harvester_v7_strategy.py`: `-0.862787`

Common failure patterns:

- over-aggressive burst sizing
- too much trading frequency
- loose entry gates that invite arbitrageur sweeps
- excessive inventory drift
- skew-sign mistakes that quote in the wrong direction

## Practical research takeaways

### 1. Toxicity filtering is still relevant

VPIN-style ideas remain useful in this simulator. Good working proxy:

- rolling buy/sell fill imbalance
- multiplied or combined with absolute move magnitude

Use toxicity in two layers:

- hard gate: cancel and stop trading when risk is extreme
- quote discount: widen or become one-sided when toxicity rises

### 2. Fill probability vs post-fill return is the core trade-off

More aggressive quoting usually increases fills, but often worsens post-fill
drift from adverse selection.

In challenge terms:

- retail edge may go up
- arb edge often gets more negative by even more

So the objective is not "maximize fills." The objective is:

- maximize conditional fill quality

### 3. Prediction-market flow is almost all informed-ish

Compared with many equity microstructure settings, this market behaves like a
higher-toxicity venue:

- lower noise flow share
- more jump and stale-quote risk
- large punishment for being wrong around information shocks

That makes safety controls more important than elegant formulas.

### 4. Best implementation patterns are simple and explicit

Across the better strategies and research notes, the repeated motifs are:

- strict inventory caps
- market-selection gates based on spread and persistence
- hard cooldown after toxicity spikes
- one-sided fallback under stress
- explicit safety overrides

## What appears to work

- enter mostly in wide-spread regimes, especially `spread >= 5`
- require spread persistence instead of reacting to one-step openings
- keep strong toxicity gating
- heavily penalize inventory accumulation
- skew around a center driven by move, flow, and inventory
- use small or moderate size unless conditions are unusually clean
- prefer one-sided survival logic over symmetric quoting in stressed states

## What appears to fail

- chasing fills in narrower spreads
- large base order sizes
- long periods of always-on two-sided quoting
- weak cooldowns after adverse moves
- making the strategy more complex without preserving the safety shell

## Optimizer target goals

### Objective

Maximize mean edge on the 200x2000 run.

Stretch business target:

- `+$1000` profit / edge

Operational leaderboard ladder:

1. beat current best: `> +1.934133`
2. reach `+5`
3. reach `+20`
4. reach `+100`
5. reach `+1000`

The first milestone is close enough for parameter and structure search. The
later milestones likely require discovering a materially better execution regime
rather than only tuning constants.

## Hard evaluation protocol for future AI runs

### Triage stage

- test many candidates on `40-80` simulations
- keep the strategy source file for every candidate worth revisiting

### Promotion stage

- run full `200` simulation tests on top candidates

### Required metrics to report

For every promoted candidate, record:

- mean edge
- mean retail edge
- mean arb edge
- fill count
- traded quantity
- average absolute inventory
- max edge
- min edge
- positive-run count

## Highest-priority search directions

Start from `right_tail_spread5_harvester_v1_strategy.py`.

Then explore:

1. VPIN-gated v1 hybrid
   - preserve v1 execution style
   - add toxicity veto only when wide-spread entry is otherwise allowed
   - test hard and soft toxicity thresholds separately
2. HMM or regime filter hybrid
   - use regime inference only as a market-selection layer
   - do not let a regime model force frequent trading
3. One-sided logic refinement
   - when toxic or directional, allow only inventory-reducing or
     direction-confirming quotes
4. Center skew tuning
   - rebalance weights on move, flow, and inventory
   - check carefully for sign errors
5. Size schedule tuning
   - vary base size, inventory shrink points, and per-side budget fraction
6. Second-level quote experiments
   - only as an optional layer under very clean conditions
   - disable quickly under toxicity

## Recommended optimizer brief

Use this prompt as a starting point for another AI:

> Work from `examples/right_tail_spread5_harvester_v1_strategy.py` as the base.
> Optimize for mean edge on the 200x2000 evaluation, not fill count. Preserve
> strict spread>=5 gating, cooldowns, and inventory safety unless a change
> clearly improves full-run edge. Prioritize VPIN-style toxicity gating,
> selective one-sided quoting, center-skew tuning, and safer size schedules.
> Triage on 40-80 simulations, then promote the strongest candidates to 200
> simulations. Always report mean edge, retail edge, arb edge, fills, traded
> quantity, average absolute inventory, max, min, and positive-run count. The
> current benchmark to beat is +1.934133 mean edge, with a long-term stretch
> target of +1000.
