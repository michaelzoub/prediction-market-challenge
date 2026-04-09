from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Variant: wider gate, more flow confirmation, moderate size."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak_wide = 0
        self.cooldown = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.94 * self.abs_move + 0.06 * abs(move)
        self.flow = 0.86 * self.flow + 0.14 * fill_imb
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fill_sum)

        if spread >= 4:
            self.streak_wide += 1
        else:
            self.streak_wide = 0

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        if self.tox > 0.95 or self.abs_move > 0.85:
            self.cooldown = 5
            return actions

        if spread < 4 or self.streak_wide < 2:
            return actions
        if self.tox > 0.33 or self.abs_move > 0.58:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        signal = 0.52 * move + 0.48 * self.flow
        if abs(signal) < 0.14:
            return actions

        budget = free_cash * 0.28
        size = 5.8
        if abs(inv) > 95:
            size = 2.0

        if signal > 0 and inv < 120:
            px = min(99, ask - 1)
            if px <= bid:
                return actions
            q = self._safe_buy_qty(px, size, budget)
            if q >= 0.01:
                actions.append(PlaceOrder(Side.BUY, px, q))
            return actions

        if signal < 0 and inv > -120:
            px = max(1, bid + 1)
            if px >= ask:
                return actions
            q = self._safe_sell_qty(px, size, budget, state.yes_inventory)
            if q >= 0.01:
                actions.append(PlaceOrder(Side.SELL, px, q))
            return actions

        return actions
