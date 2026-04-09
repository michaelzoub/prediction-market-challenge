from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Very sparse high-conviction bursts only in super-wide windows."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.abs_move = 0.0
        self.tox = 0.0
        self.flow = 0.0
        self.wide_streak = 0
        self.pause = 0

    def _safe_buy(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _safe_sell(self, tick: int, qty: float, cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - px)
        return _q(min(qty, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.94 * self.abs_move + 0.06 * abs(move)
        self.tox = 0.92 * self.tox + 0.08 * (abs(move) * fills)
        self.flow = 0.85 * self.flow + 0.15 * imb

        if spread >= 4:
            self.wide_streak += 1
        else:
            self.wide_streak = 0

        actions: list[object] = [CancelAll()]
        if self.pause > 0:
            self.pause -= 1
            return actions

        if self.tox > 0.55 or self.abs_move > 0.65:
            self.pause = 8
            return actions

        if spread < 4 or self.wide_streak < 10:
            return actions
        if self.tox > 0.20 or self.abs_move > 0.25:
            return actions

        signal = 0.80 * move + 0.20 * self.flow
        if abs(signal) < 0.55:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        budget = free_cash * 0.35

        if signal > 0 and inv < 60:
            px = _clip_tick(bid + 1)
            if px >= ask:
                return actions
            qty = self._safe_buy(px, 14.0, budget)
            if qty >= 0.01:
                actions.append(PlaceOrder(Side.BUY, px, qty))
            return actions

        if signal < 0 and inv > -60:
            px = _clip_tick(ask - 1)
            if px <= bid:
                return actions
            qty = self._safe_sell(px, 14.0, budget, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(Side.SELL, px, qty))
            return actions

        return actions
