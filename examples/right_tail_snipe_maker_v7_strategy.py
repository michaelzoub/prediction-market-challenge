from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Random-search winner focused on spread>=4 right-tail capture."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - px)
        return _q(min(qty, covered + uncovered))

    def on_step(self, state):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        filled_qty = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.907 * self.abs_move + 0.093 * abs(move)
        self.flow = 0.864 * self.flow + 0.136 * fill_imb
        self.tox = 0.937 * self.tox + 0.063 * (abs(move) * filled_qty)
        self.streak = self.streak + 1 if spread >= 4 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.124 or self.abs_move > 0.63:
            self.cool = 6
            return actions

        if spread < 4 or self.streak < 4:
            return actions
        if self.tox > 0.47 or self.abs_move > 0.53:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.136 * move - 0.556 * self.flow - 0.026 * inv))
        width = 2
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        quote_buy = inv < 150
        quote_sell = inv > -150
        size = 6.082
        if abs(inv) > 30:
            size = min(size, 1.4)

        budget = free_cash * 0.248
        if quote_buy:
            px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
            qty = self._buy(px, size, budget)
            if qty >= 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, qty))
                free_cash = max(0.0, free_cash - (px / 100.0) * qty)
                budget = free_cash * 0.248

        if quote_sell:
            px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
            qty = self._sell(px, size, budget, state.yes_inventory)
            if qty >= 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, qty))

        return actions
