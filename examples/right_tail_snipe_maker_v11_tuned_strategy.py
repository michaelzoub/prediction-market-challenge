from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Tuned v11 variant from local full-200 search."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.fair = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, base_qty: float, cash: float, inv: float) -> float:
        skew = max(0.0, -inv / 15.0)
        return _q(min(base_qty * (1.0 + skew), cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, base_qty: float, cash: float, yes_inv: float, inv: float) -> float:
        price = tick / 100.0
        skew = max(0.0, inv / 15.0)
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - price)
        return _q(min(base_qty * (1.0 + skew), covered + uncovered))

    def on_step(self, state):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        self.fair = 0.554 * self.fair + 0.446 * mid

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.889 * self.abs_move + 0.111 * abs(move)
        self.flow = 0.89 * self.flow + 0.11 * fill_imb
        self.tox = 0.949 * self.tox + 0.051 * (abs(move) * fill_sum)

        if spread >= 4:
            self.streak = min(10, self.streak + 1)
        else:
            self.streak = max(0, self.streak - 3)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.421 or self.abs_move > 0.467 or abs(self.flow) > 1.331:
            self.cool = 3
            return actions
        if spread < 4.18 or self.streak < 3:
            return actions
        if self.tox > 0.281 or self.abs_move > 0.417:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        if abs(inv) > 30:
            return actions

        center = int(round(self.fair - 0.075 * move - 0.362 * self.flow - 0.027 * inv))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if buy_tick >= sell_tick:
            return actions

        base_size = min(2.455, 2.497 / (1.0 + abs(inv) / 30.0))
        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

        buy_qty = self._buy_qty(buy_px, base_size, free_cash * 0.249, inv)
        if buy_qty >= 0.01 and buy_px < ask:
            actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
            if spread > 5 and buy_tick < buy_px:
                actions.append(PlaceOrder(Side.BUY, buy_tick, buy_qty * 0.187))
            free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

        sell_qty = self._sell_qty(sell_px, base_size, free_cash * 0.249, state.yes_inventory, inv)
        if sell_qty >= 0.01 and bid < sell_px:
            actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))
            if spread > 5 and sell_tick > sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_tick, sell_qty * 0.187))

        return actions
