from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Spread>=5 harvester (v4) from local full-200 search best."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - price)
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
        fill_imbalance = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.921 * self.abs_move + 0.079 * abs(move)
        self.flow = 0.895 * self.flow + 0.105 * fill_imbalance
        self.tox = 0.883 * self.tox + 0.117 * (abs(move) * fills)
        self.streak = self.streak + 1 if spread >= 5 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.938 or self.abs_move > 0.937:
            self.cool = 2
            return actions
        if spread < 5 or self.streak < 3:
            return actions
        if self.tox > 0.404 or self.abs_move > 0.592:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.021 * move - 0.619 * self.flow - 0.042 * inv))
        buy_tick = _clip_tick(min(ask - 1, max(bid, center - 2)))
        sell_tick = _clip_tick(max(bid + 1, min(ask, center + 2)))
        if buy_tick >= sell_tick:
            return actions

        size = 3.89
        if abs(inv) > 120:
            size = min(size, 1.147)

        signal = 0.842 * move + 0.705 * self.flow
        quote_buy = inv < 260
        quote_sell = inv > -260
        if signal > 0.331:
            quote_sell = False
        elif signal < -0.331:
            quote_buy = False

        if quote_buy:
            buy_qty = self._buy_qty(buy_tick, size, free_cash * 0.598)
            if buy_qty >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, buy_qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * buy_qty)

        if quote_sell:
            sell_qty = self._sell_qty(sell_tick, size, free_cash * 0.598, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, sell_qty))

        return actions
