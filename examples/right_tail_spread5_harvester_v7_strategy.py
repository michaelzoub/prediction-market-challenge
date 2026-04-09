from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Constrained variant of v6 with hard inventory clipping."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0
        self.burst = 0.0

    def _buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - price)
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

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.886 * self.abs_move + 0.114 * abs(move)
        self.flow = 0.856 * self.flow + 0.144 * fill_imb
        self.tox = 0.924 * self.tox + 0.076 * (abs(move) * fills)

        if spread >= 5:
            self.streak = min(40, self.streak + 1)
            self.burst = min(8.0, self.burst + 0.9)
        else:
            self.streak = max(0, self.streak - 2)
            self.burst = max(0.0, self.burst - 0.9)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.0 or self.abs_move > 0.85:
            self.cool = 2
            return actions

        if spread < 4.6 or self.streak < 2:
            return actions
        if self.tox > 0.58 or self.abs_move > 0.50:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        if abs(inv) > 35:
            # If inventory drifts, only quote inventory-reducing side.
            reduce_buy = inv < 0
            reduce_sell = inv > 0
        else:
            reduce_buy = True
            reduce_sell = True

        center = int(round(mid + 0.06 * move - 0.18 * self.flow - 0.065 * inv))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
        if buy_px >= sell_px:
            return actions

        signal = 0.75 * move + 0.67 * self.flow
        quote_buy = reduce_buy and inv < 90
        quote_sell = reduce_sell and inv > -90
        if signal > 0.7:
            quote_sell = False
        elif signal < -0.7:
            quote_buy = False

        size = 6.0 + 1.8 * self.burst
        if abs(inv) > 20:
            size = min(size, 3.5)

        budget = free_cash * 0.32
        if quote_buy:
            buy_qty = self._buy_qty(buy_px, size, budget)
            if buy_qty >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)
                budget = free_cash * 0.32

        if quote_sell:
            sell_qty = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
