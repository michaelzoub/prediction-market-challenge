from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Right-tail snipe v12: Tail-profit maximizer (+1000$ bursts)."""

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

        self.abs_move = 0.88 * self.abs_move + 0.12 * abs(move)
        self.flow = 0.83 * self.flow + 0.17 * fill_imbalance
        self.tox = 0.88 * self.tox + 0.12 * (abs(move) * fill_sum)

        self.streak = self.streak + 1 if spread >= 3 else 0
        if self.streak >= 4:
            self.burst = min(10.0, self.burst + 1.0)
        else:
            self.burst = max(0.0, self.burst - 0.5)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.92 or self.abs_move > 0.72:
            self.cool = 5
            return actions

        if spread < 3 or self.streak < 2:
            return actions
        if self.tox > 0.65 or self.abs_move > 0.45:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.22 * move - 0.42 * self.flow - 0.035 * inventory + 0.1 * self.burst))
        width = 3 if self.burst < 3 else 5
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        base_size = 6.2
        regret = min(2.5, abs(move) + 0.3 * self.burst)
        size = base_size * (1 + 0.4 * regret)
        if abs(inventory) > 25:
            size = min(size, 2.2)

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

        buy_qty = self._buy_qty(buy_px, size, free_cash * 0.32)
        if buy_qty >= 0.01 and buy_px < ask:
            actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
            free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

        sell_qty = self._sell_qty(sell_px, size, free_cash * 0.32, state.yes_inventory)
        if sell_qty >= 0.01 and bid < sell_px:
            actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
