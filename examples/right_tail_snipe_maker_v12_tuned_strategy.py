from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """v12 tuned: preserve burst logic, reduce arb exposure."""

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

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imbalance = state.buy_filled_quantity - state.sell_filled_quantity

        self.abs_move = 0.90 * self.abs_move + 0.10 * abs(move)
        self.flow = 0.87 * self.flow + 0.13 * fill_imbalance
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fill_sum)

        self.streak = self.streak + 1 if spread >= 4 else 0
        if self.streak >= 4:
            self.burst = min(6.0, self.burst + 0.75)
        else:
            self.burst = max(0.0, self.burst - 0.5)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.70 or self.abs_move > 0.60:
            self.cool = 5
            return actions

        if spread < 4 or self.streak < 2:
            return actions
        if self.tox > 0.42 or self.abs_move > 0.42:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        if abs(inventory) > 40:
            return actions

        center = int(round(mid - 0.20 * move - 0.35 * self.flow - 0.03 * inventory + 0.05 * self.burst))
        width = 2 if self.burst < 2 else 3
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        base_size = 3.6
        regret = min(1.4, abs(move) + 0.12 * self.burst)
        size = base_size * (1.0 + 0.25 * regret)
        if abs(inventory) > 20:
            size = min(size, 1.9)

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

        buy_qty = self._buy_qty(buy_px, size, free_cash * 0.18)
        if buy_qty >= 0.01 and buy_px < ask:
            actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
            free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

        sell_qty = self._sell_qty(sell_px, size, free_cash * 0.18, state.yes_inventory)
        if sell_qty >= 0.01 and bid < sell_px:
            actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
