from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Retail-only ultrastable strategy (aims for "tall bar" behavior).

    This intentionally trades *rarely*:
    - It only engages when spread is tight and midpoint movement is extremely small.
    - When engaged, it steps inside by 1 tick for queue priority and posts larger size.
    - TTL=1 (cancel+replace every step) to reduce arb staleness risk.
    - Hard cooldown on jump-like discontinuities (competitor best quote gaps).
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._prev_mid: int | None = None
        self._vol_ewma = 0.0
        self.vol_alpha = 0.15

        self._prev_comp_mid: int | None = None
        self.jump_mid_move_ticks = 4
        self.cooldown = 0
        self.cooldown_steps = 8

        self.engage_spread_max = 2
        self.engage_vol_max = 0.22
        self.engage_mid_dist_max = 7
        self.engage_time_frac_min = 0.28

        self.step_inside = 1
        self.size = 16.0

        self.inv_cap = 140.0
        self.net_hard = 70.0

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

        spread = ask - bid
        if spread <= 0:
            return [CancelAll()]

        mid = (bid + ask) // 2
        self._update_vol(mid)

        if self._prev_comp_mid is not None and abs(mid - self._prev_comp_mid) >= self.jump_mid_move_ticks:
            self.cooldown = self.cooldown_steps
        self._prev_comp_mid = mid

        if self.cooldown > 0:
            self.cooldown -= 1
            return [CancelAll()]

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

        net = state.yes_inventory - state.no_inventory
        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        # step inside only if it doesn't lock/cross
        bid_tick = bid + self.step_inside if spread >= 3 else bid
        ask_tick = ask - self.step_inside if spread >= 3 else ask
        if bid_tick >= ask_tick:
            return [CancelAll()]

        size = self.size * (1.0 - 0.35 * min(1.0, mid_dist / 20.0))
        size = max(0.8, round(size, 4))

        actions: list[object] = [CancelAll()]
        if can_bid:
            actions.append(PlaceOrder(Side.BUY, bid_tick, size))
        if can_ask:
            actions.append(PlaceOrder(Side.SELL, ask_tick, size))
        return actions

