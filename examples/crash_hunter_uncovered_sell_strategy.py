from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Exploit stale high asks after downward dislocations.

    Goal:
    - detect regimes where the public ladder remains high relative to a slowly
      moving anchor after a crash
    - post giant uncovered asks because collateral is cheap at high prices
    - let retail buy into those stale asks after the arb sweep leaves them safe

    This is intentionally not a normal market maker. It optimizes for rare,
    massive edge events rather than smooth PnL.
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._baseline_mid: float | None = None
        self._baseline_alpha = 0.02
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
        if downward_crash >= 5.0 or (spread_move >= 3 and bid <= 65):
            self._shock_cooldown = 4
            if ask >= 78 or baseline >= 75.0:
                self._crash_window = 36

        actions: list[object] = [CancelAll()]
        if self._shock_cooldown > 0:
            self._shock_cooldown -= 1
            return actions

        if self._crash_window > 0:
            self._crash_window -= 1
        else:
            return actions

        # This is a score exploit, not a general strategy. Only hunt late enough
        # that the public anchor remains stale but the remaining horizon is shorter.
        frac = self._time_fraction_remaining(state)
        if frac > 0.65:
            return actions

        # Need a very high stale public ask relative to a slower anchor.
        stale_high_ask = float(ask) - baseline
        if ask < 82 or stale_high_ask < 8.0:
            return actions

        # We want the book to have stabilized after the crash and buy flow to be
        # non-negative so retail buys are more plausible than more sell pressure.
        if spread < 2 or spread > 5 or self._stable < 3 or self._vol > 0.55:
            return actions
        if self._buy_flow < -1.5 or self._tox > 0.22:
            return actions

        # Only quote the ask side. Keep a loose cap because uncovered sells are
        # the whole point, but avoid infinite inventory spirals.
        inventory = state.yes_inventory - state.no_inventory
        if inventory < -6_000:
            return actions

        free_cash = max(0.0, state.free_cash)
        if free_cash <= 0.0:
            return actions

        # Quote at the stale public ask or one tick inside when spread allows.
        sell_tick = ask - 1 if spread >= 3 else ask
        sell_tick = _clip_tick(max(bid + 1, sell_tick))
        if sell_tick <= bid:
            return actions

        # Massive size because collateral is cheap exactly where edge-per-share is huge.
        size = 500.0
        if ask >= 88 and stale_high_ask >= 12.0:
            size = 24_000.0
        elif ask >= 85:
            size = 12_000.0
        elif ask >= 82:
            size = 4_500.0

        # If we already saw buyer-driven fills, lean in.
        if state.sell_filled_quantity > 0.0 and self._buy_flow >= 0.0:
            size *= 1.25

        qty = self._safe_uncovered_sell_qty(sell_tick, size, free_cash, state.yes_inventory)
        if qty < 0.01:
            return actions

        return [CancelAll(), PlaceOrder(Side.SELL, sell_tick, qty)]
