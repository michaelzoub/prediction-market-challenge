from __future__ import annotations

from typing import Sequence

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Toxicity-heavy strategy focused on minimizing arb losses."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox_ewma = 0.0
        self.adverse_run = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        max_qty = free_cash / max(tick / 100.0, 0.01)
        return _q(min(target, max_qty))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered_cap = free_cash / max(1.0 - price, 0.01)
        return _q(min(target, covered + uncovered_cap))

    def on_step(self, state: StepState) -> Sequence[object]:
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        one_sided = abs(buy_fill - sell_fill)

        adverse = 0.0
        if buy_fill > 0 and move < 0:
            adverse += abs(move) * buy_fill
        if sell_fill > 0 and move > 0:
            adverse += abs(move) * sell_fill
        if adverse > 0.2:
            self.adverse_run = min(12, self.adverse_run + 2)
        else:
            self.adverse_run = max(0, self.adverse_run - 1)

        step_tox = 0.65 * adverse + 0.20 * one_sided + 0.15 * abs(move)
        self.tox_ewma = 0.88 * self.tox_ewma + 0.12 * step_tox

        inventory = state.yes_inventory - state.no_inventory

        width = 2
        if self.tox_ewma > 0.20:
            width = 3
        if self.tox_ewma > 0.40:
            width = 5
        if self.adverse_run > 4:
            width = max(width, 6)

        center = int(round(mid - 0.04 * inventory))
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        size = 4.5
        if self.tox_ewma > 0.35:
            size = 2.5
        if abs(inventory) > 80:
            size = min(size, 2.0)

        quote_buy = inventory < 150
        quote_sell = inventory > -150

        # Severe toxicity: only inventory-reducing side.
        if self.tox_ewma > 0.55 or self.adverse_run >= 6:
            quote_buy = inventory < 0
            quote_sell = inventory > 0
            if inventory == 0:
                quote_buy = False
                quote_sell = False

        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)
        budget = free_cash * 0.22

        if quote_buy:
            qty = self._safe_buy_qty(buy_tick, size, budget)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * qty)
                budget = free_cash * 0.22

        if quote_sell:
            qty = self._safe_sell_qty(sell_tick, size, budget, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=qty))

        return actions
