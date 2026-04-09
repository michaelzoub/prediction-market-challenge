from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Retail-first "sniper" strategy (passive limits only).

    Core idea:
    - Arb sweeps *before* retail each step. Any stale quote is punished immediately.
    - So we only quote when the book looks extremely stable (tight spread + low midpoint movement),
      which makes next-step adverse selection less likely.
    - In those windows, we step inside the static competitor by 1 tick to win queue priority
      and capture more retail flow, aiming for a "tall bar" of small-positive outcomes.
    """

    def __init__(self) -> None:
        # Stability proxy: EWMA of absolute midpoint changes (in ticks)
        self._prev_mid: int | None = None
        self._vol_ewma = 0.0
        self.vol_alpha = 0.12

        # Strict engagement: only quote in very stable conditions
        self.engage_spread_max = 4
        self.engage_vol_max = 0.55
        self.engage_mid_dist_max = 14
        self.engage_time_frac_min = 0.12

        # Aggression while engaged
        self.step_inside = 1
        self.base_size = 10.0
        self.requote_every = 3  # reduce time spent stale between cancels

        # Risk controls
        self.inv_cap = 140.0
        self.net_hard = 80.0  # go one-sided when offside

        # Slow start: wait a bit for the competitor ladder to "reveal" best quotes
        self.min_step = 10

        self._total_steps: int | None = None

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_vol(self, mid: int) -> None:
        dm = 0.0 if self._prev_mid is None else abs(mid - self._prev_mid)
        self._prev_mid = mid
        self._vol_ewma = (1.0 - self.vol_alpha) * self._vol_ewma + self.vol_alpha * float(dm)

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None:
            return [CancelAll()]

        if state.step < self.min_step:
            return [CancelAll()]

        spread = ask - bid
        if spread <= 0:
            return [CancelAll()]

        mid = (bid + ask) // 2
        self._update_vol(mid)

        frac = self._time_fraction_remaining(state)
        mid_dist = abs(mid - 50)

        engaged = (
            spread <= self.engage_spread_max
            and self._vol_ewma <= self.engage_vol_max
            and mid_dist <= self.engage_mid_dist_max
            and frac >= self.engage_time_frac_min
        )
        if not engaged:
            return [CancelAll()]

        # Requote cadence: cancel/refresh periodically to reduce arb staleness.
        if state.step % self.requote_every != 0:
            return [CancelAll()]

        net = state.yes_inventory - state.no_inventory

        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap

        # Hard one-sided if inventory drifts too far.
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        # Step inside only if it doesn't collapse the spread.
        bid_tick = bid + self.step_inside if spread >= 3 else bid
        ask_tick = ask - self.step_inside if spread >= 3 else ask
        if bid_tick >= ask_tick:
            return [CancelAll()]

        # Size: fixed and fairly large, but scaled down slightly when mid is farther from 50.
        size = self.base_size * (1.0 - 0.35 * min(1.0, mid_dist / 20.0))
        size = max(0.8, round(size, 4))

        actions: list[object] = [CancelAll()]
        if can_bid:
            actions.append(PlaceOrder(Side.BUY, bid_tick, size))
        if can_ask:
            actions.append(PlaceOrder(Side.SELL, ask_tick, size))
        return actions

