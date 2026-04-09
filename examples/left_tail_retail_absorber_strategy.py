from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Exploit rare low-probability retail-sell cascades for huge right tails.

    First-principles thesis:
    - Retail sell quantity scales like notional / p_t in the simulator.
    - When true probability gets very small, even modest retail sell notional can
      become a very large market sell quantity.
    - If the competitor bid is stale and we can post a large bid just inside it
      after the public book stabilizes, a single simulation can generate a large
      positive edge from absorbing that flow.
    - Most of the time we should do nothing; the strategy is a deliberate
      right-tail lottery with bounded arb exposure.
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._baseline_mid: float | None = None
        self._baseline_alpha = 0.025
        self._baseline_learn_steps = 120

        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._prev_mid = 50.0
        self._stable = 0
        self._shock_timer = 0
        self._extreme_window = 0

        self._flow = 0.0
        self._vol = 0.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_baseline(self, mid: int) -> None:
        if self._baseline_mid is None:
            self._baseline_mid = float(mid)
        else:
            self._baseline_mid = (1.0 - self._baseline_alpha) * self._baseline_mid + self._baseline_alpha * float(mid)

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None or ask <= bid:
            return [CancelAll()]

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self._prev_mid
        self._prev_mid = mid

        if state.step < self._baseline_learn_steps:
            self._update_baseline(int(round(mid)))
            self._prev_bid = bid
            self._prev_ask = ask
            return [CancelAll()]

        baseline = self._baseline_mid if self._baseline_mid is not None else mid
        self._update_baseline(int(round(mid)))

        touch_move = (
            0
            if self._prev_bid is None or self._prev_ask is None
            else abs(bid - self._prev_bid) + abs(ask - self._prev_ask)
        )
        spread_move = 0 if self._prev_bid is None or self._prev_ask is None else spread - (self._prev_ask - self._prev_bid)
        self._prev_bid = bid
        self._prev_ask = ask

        self._stable = self._stable + 1 if touch_move <= 1 else 0
        self._flow = 0.90 * self._flow + 0.10 * (state.buy_filled_quantity - state.sell_filled_quantity)
        self._vol = 0.91 * self._vol + 0.09 * abs(move)

        if touch_move >= 5 or spread_move >= 3:
            self._shock_timer = 6
            if bid <= 18 or mid <= 24:
                self._extreme_window = 40

        if self._shock_timer > 0:
            self._shock_timer -= 1
            return [CancelAll()]

        if self._extreme_window > 0:
            self._extreme_window -= 1

        frac = self._time_fraction_remaining(state)
        if frac > 0.55:
            return [CancelAll()]

        # Focus almost entirely on the low-probability side of the state space.
        low_probability_regime = bid <= 16 or mid <= 22 or baseline <= 26.0
        if not low_probability_regime:
            return [CancelAll()]

        # Need a wide public spread and local stabilization after the move.
        if spread < 4 or self._stable < 3 or self._vol > 0.55:
            return [CancelAll()]

        dislocation = baseline - float(bid)
        if dislocation < 8.0:
            return [CancelAll()]

        # Inventory / capital gates.
        inventory = state.yes_inventory - state.no_inventory
        if inventory > 3_500:
            return [CancelAll()]

        free_cash = max(0.0, state.free_cash)
        if free_cash <= 0.0:
            return [CancelAll()]

        # Quote one tick inside only when there is enough room; otherwise join bid.
        buy_tick = bid + 1 if spread >= 5 else bid
        buy_tick = _clip_tick(min(ask - 1, buy_tick))
        if buy_tick >= ask:
            return [CancelAll()]

        # Size aggressively only in the rare extreme window.
        size = 40.0
        if self._extreme_window > 0 and bid <= 12:
            size = 3_200.0
        elif self._extreme_window > 0 or bid <= 14:
            size = 1_200.0
        elif bid <= 16:
            size = 320.0

        # Increase size when prior flow was sell-heavy: that is the side we want.
        if self._flow < -3.0:
            size *= 1.35

        max_qty = free_cash / max(0.01, buy_tick / 100.0)
        qty = _q(min(size, max_qty))
        if qty < 0.01:
            return [CancelAll()]

        return [CancelAll(), PlaceOrder(Side.BUY, buy_tick, qty)]
