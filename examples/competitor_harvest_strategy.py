from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Competitor-harvest / safety-first strategy.

    The static competitor provides a *persistent* reference ladder that does not
    re-center after jumps. After jumps, arb sweeps the ladder, and the new best
    quotes can gap. This strategy:
    - primarily quotes *at* the competitor best (not inside) with small size,
      trying to pick up occasional retail without being arb's first target;
    - enforces TTL=1 (cancel+replace each step);
    - sits out on jump-like discontinuities in competitor best quotes.
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._prev_comp_mid: int | None = None
        self._prev_comp_spread: int | None = None
        self.jump_mid_move_ticks = 5
        self.jump_spread_widen_ticks = 5
        self.cooldown_base = 6
        self.cooldown_max = 18
        self._cooldown = 0

        self.size = 2.4
        self.inv_cap = 150.0
        self.net_hard = 75.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None:
            return [CancelAll()]

        spread = ask - bid
        if spread <= 0:
            return [CancelAll()]

        mid = (bid + ask) // 2
        frac = self._time_fraction_remaining(state)

        if self._prev_comp_mid is not None:
            mid_move = abs(mid - self._prev_comp_mid)
        else:
            mid_move = 0
        if self._prev_comp_spread is not None:
            spread_move = spread - self._prev_comp_spread
        else:
            spread_move = 0
        self._prev_comp_mid = mid
        self._prev_comp_spread = spread

        if mid_move >= self.jump_mid_move_ticks or spread_move >= self.jump_spread_widen_ticks:
            early_bonus = int(round(5.0 * frac))
            gap_bonus = int(round(min(10.0, 0.9 * mid_move + 0.6 * max(0, spread_move))))
            self._cooldown = max(
                self._cooldown,
                min(self.cooldown_max, self.cooldown_base + early_bonus + gap_bonus),
            )

        if self._cooldown > 0:
            self._cooldown -= 1
            return [CancelAll()]

        net = state.yes_inventory - state.no_inventory
        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        actions: list[object] = [CancelAll()]  # TTL=1
        if can_bid:
            actions.append(PlaceOrder(Side.BUY, bid, self.size))
        if can_ask:
            actions.append(PlaceOrder(Side.SELL, ask, self.size))
        return actions

