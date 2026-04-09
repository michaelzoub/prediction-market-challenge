from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Lottery-tail experiment from broad search (v6)."""

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

        self.abs_move = 0.884 * self.abs_move + 0.116 * abs(move)
        self.flow = 0.908 * self.flow + 0.092 * fill_imb
        self.tox = 0.901 * self.tox + 0.099 * (abs(move) * fills)

        if spread >= 4:
            self.streak = min(50, self.streak + 1)
            self.burst = min(20.0, self.burst + 0.923)
        else:
            self.streak = max(0, self.streak - 2)
            self.burst = max(0.0, self.burst - 0.623)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.318 or self.abs_move > 1.053:
            self.cool = 2
            return actions

        if spread < 4.38 or self.streak < 2:
            return actions
        if self.tox > 0.315 or self.abs_move > 0.556:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        signal = 1.277 * move + 1.452 * self.flow
        quote_buy = inv < 800
        quote_sell = inv > -800

        mode = 1
        if mode == 1:
            if signal > 0.521:
                quote_sell = False
            elif signal < -0.521:
                quote_buy = False

        size_touch = 9.33 + 1.413 * self.burst
        size_inside = 11.007 + 2.92 * self.burst
        if abs(inv) > 220:
            size_touch = min(size_touch, 4.809)
            size_inside = min(size_inside, 8.829)

        budget = free_cash * 0.326

        b_touch = bid
        s_touch = ask
        b_in = _clip_tick(min(ask - 1, bid + 1))
        s_in = _clip_tick(max(bid + 1, ask - 1))
        use_inside = abs(signal) > 0.18 and self.abs_move < 0.699

        if quote_buy:
            q = self._buy_qty(b_touch, size_touch, budget)
            if q >= 0.01 and b_touch < ask:
                actions.append(PlaceOrder(Side.BUY, b_touch, q))
                free_cash = max(0.0, free_cash - (b_touch / 100.0) * q)
                budget = free_cash * 0.326
            if use_inside and b_in > bid and b_in < ask:
                qi = self._buy_qty(b_in, size_inside, budget * 0.138)
                if qi >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, b_in, qi))

        if quote_sell:
            q = self._sell_qty(s_touch, size_touch, budget, state.yes_inventory)
            if q >= 0.01 and bid < s_touch:
                actions.append(PlaceOrder(Side.SELL, s_touch, q))
            if use_inside and s_in < ask and s_in > bid:
                qi = self._sell_qty(s_in, size_inside, budget * 0.138, state.yes_inventory)
                if qi >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, s_in, qi))

        return actions
