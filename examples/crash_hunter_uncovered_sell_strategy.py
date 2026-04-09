from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Exploit stale high asks after a high-baseline crash."""

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._baseline_mid: float | None = None
        self._baseline_alpha = 0.015
        self._baseline_learn_steps = 140

        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._prev_mid = 50.0

        self._stable = 0
        self._shock_cooldown = 0
        self._crash_window = 0

        self._vol = 0.0
        self._buy_flow = 0.0
        self._tox = 0.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_baseline(self, mid: float) -> None:
        if self._baseline_mid is None:
            self._baseline_mid = mid
        else:
            self._baseline_mid = (1.0 - self._baseline_alpha) * self._baseline_mid + self._baseline_alpha * mid

    def _safe_uncovered_sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered_cap = free_cash / max(0.01, 1.0 - price)
        return _q(min(target, covered + uncovered_cap))

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
            self._update_baseline(mid)
            self._prev_bid = bid
            self._prev_ask = ask
            return [CancelAll()]

        baseline = self._baseline_mid if self._baseline_mid is not None else mid
        self._update_baseline(mid)

        touch_move = (
            0
            if self._prev_bid is None or self._prev_ask is None
            else abs(bid - self._prev_bid) + abs(ask - self._prev_ask)
        )
        spread_move = 0 if self._prev_bid is None or self._prev_ask is None else spread - (self._prev_ask - self._prev_bid)
        prev_mid = mid if self._prev_bid is None or self._prev_ask is None else 0.5 * (self._prev_bid + self._prev_ask)
        self._prev_bid = bid
        self._prev_ask = ask

        self._stable = self._stable + 1 if touch_move <= 1 else 0
        self._vol = 0.92 * self._vol + 0.08 * abs(move)
        self._buy_flow = 0.90 * self._buy_flow + 0.10 * (state.buy_filled_quantity - state.sell_filled_quantity)
        adverse = 0.0
        if state.sell_filled_quantity > 0.0 and move > 0.0:
            adverse += abs(move) * state.sell_filled_quantity
        self._tox = 0.90 * self._tox + 0.10 * (0.7 * adverse + 0.3 * abs(move) * (state.buy_filled_quantity + state.sell_filled_quantity))

        downward_crash = prev_mid - mid
        high_baseline = baseline >= 72.0
        crash_from_high = downward_crash >= 5.0 and prev_mid >= 72.0
        spread_blowout = spread_move >= 4 and ask >= 78
        if high_baseline and (crash_from_high or spread_blowout):
            self._shock_cooldown = 3
            self._crash_window = 45

        actions: list[object] = [CancelAll()]
        if self._shock_cooldown > 0:
            self._shock_cooldown -= 1
            return actions

        if self._crash_window > 0:
            self._crash_window -= 1
        else:
            return actions

        frac = self._time_fraction_remaining(state)
        if frac > 0.78:
            return actions

        stale_high_ask = float(ask) - baseline
        if baseline < 72.0 or ask < 78 or stale_high_ask < 6.0:
            return actions

        # The exploitable state is a still-high ask after a crash, often with a
        # very wide spread because bids have already been swept.
        if spread < 6 or spread > 20 or self._stable < 2 or self._vol > 0.95:
            return actions
        if self._buy_flow < -2.0 or self._tox > 0.32:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        if inventory < -20_000:
            return actions

        free_cash = max(0.0, state.free_cash)
        if free_cash <= 0.0:
            return actions

        # Quote just inside the public ask so retail buys us first while remaining
        # far above the post-crash fair value.
        sell_tick = ask - 1 if spread >= 8 else ask
        sell_tick = _clip_tick(max(bid + 1, sell_tick))
        if sell_tick <= bid:
            return actions

        # Use the collateral asymmetry directly: near-90 asks are extremely cheap
        # to short and can produce huge edge if retail still buys there.
        size = 3_000.0
        if sell_tick >= 92:
            size = 60_000.0
        elif sell_tick >= 89:
            size = 35_000.0
        elif sell_tick >= 85:
            size = 18_000.0
        elif sell_tick >= 80:
            size = 8_000.0
        if stale_high_ask >= 10.0:
            size *= 1.5
        if state.sell_filled_quantity > 0.0 and self._buy_flow >= 0.0:
            size *= 1.6

        qty = self._safe_uncovered_sell_qty(sell_tick, size, free_cash, state.yes_inventory)
        if qty < 0.01:
            return actions

        return [CancelAll(), PlaceOrder(Side.SELL, sell_tick, qty)]
