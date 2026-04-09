from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Spread>=5 harvester v2: one-sided directional bursts in extreme wide spreads."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.flow = 0.0
        self.wide_streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, size: float, cash: float) -> float:
        return _q(min(size, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, size: float, cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - price)
        return _q(min(size, covered + uncovered))

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
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow = 0.87 * self.flow + 0.13 * fill_imb
        self.tox = 0.91 * self.tox + 0.09 * (abs(move) * fill_sum)

        if spread >= 5:
            self.wide_streak += 1
        else:
            self.wide_streak = 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions
        if self.tox > 0.95:
            self.cool = 4
            return actions

        if spread < 5 or self.wide_streak < 2:
            return actions
        if self.tox > 0.55:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free = max(0.0, state.free_cash)
        signal = 0.55 * move + 0.45 * self.flow
        if abs(signal) < 0.12:
            return actions

        size = 9.0 if abs(signal) > 0.6 else 6.2
        if abs(inv) > 120:
            size = min(size, 2.0)

        if signal > 0 and inv < 220:
            px = _clip_tick(min(ask - 1, bid + 2))
            qty = self._buy_qty(px, size, free * 0.44)
            if qty >= 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, qty))
                return actions

        if signal < 0 and inv > -220:
            px = _clip_tick(max(bid + 1, ask - 2))
            qty = self._sell_qty(px, size, free * 0.44, state.yes_inventory)
            if qty >= 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, qty))
                return actions

        return actions
