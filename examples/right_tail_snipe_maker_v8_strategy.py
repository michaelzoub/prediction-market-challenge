from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Right-tail snipe v8 (local-search winner promoted)."""

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

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imbalance = state.buy_filled_quantity - state.sell_filled_quantity

        self.abs_move = 0.909 * self.abs_move + 0.091 * abs(move)
        self.flow = 0.845 * self.flow + 0.155 * fill_imbalance
        self.tox = 0.909 * self.tox + 0.091 * (abs(move) * fill_sum)

        self.streak = self.streak + 1 if spread >= 4 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.986 or self.abs_move > 0.673:
            self.cool = 4
            return actions

        if spread < 4 or self.streak < 2:
            return actions
        if self.tox > 0.585 or self.abs_move > 0.395:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.198 * move - 0.394 * self.flow - 0.03 * inventory))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        size = 5.584
        if abs(inventory) > 30:
            size = min(size, 1.7)

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

        buy_qty = self._buy_qty(buy_px, size, free_cash * 0.245)
        if buy_qty >= 0.01 and buy_px < ask:
            actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
            free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

        sell_qty = self._sell_qty(sell_px, size, free_cash * 0.245, state.yes_inventory)
        if sell_qty >= 0.01 and bid < sell_px:
            actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
