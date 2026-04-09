from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Automated-search-selected spread5 variant (v5)."""

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

        self.abs_move = 0.876 * self.abs_move + 0.124 * abs(move)
        self.flow = 0.879 * self.flow + 0.121 * fill_imb
        self.tox = 0.881 * self.tox + 0.119 * (abs(move) * fills)

        if spread >= 4:
            self.streak = min(30, self.streak + 1)
            self.burst = min(10.0, self.burst + 1.355)
        else:
            self.streak = max(0, self.streak - 1)
            self.burst = max(0.0, self.burst - 1.129)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.3 or self.abs_move > 0.895:
            self.cool = 2
            return actions

        if spread < 4.18 or self.streak < 1:
            return actions
        if self.tox > 0.603 or self.abs_move > 0.271:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.491 * move - 0.64 * self.flow - 0.03 * inv + 0.114 * self.burst))
        width = 1 + (1 if self.burst > 5.321 else 0)
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
        if buy_px >= sell_px:
            return actions

        signal = 0.866 * move + 0.65 * self.flow
        quote_buy = inv < 400
        quote_sell = inv > -400
        if signal > 0.552:
            quote_sell = False
        elif signal < -0.552:
            quote_buy = False

        size = 6.553 + 3.529 * self.burst
        if abs(inv) > 20:
            size = min(size, 3.932)

        budget = free_cash * 0.483
        if quote_buy:
            bq = self._buy_qty(buy_px, size, budget)
            if bq >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * 0.483
                if spread >= 9:
                    buy_px2 = _clip_tick(max(1, buy_px - 2))
                    if buy_px2 < ask:
                        bq2 = self._buy_qty(buy_px2, size * 0.405, budget)
                        if bq2 >= 0.01:
                            actions.append(PlaceOrder(Side.BUY, buy_px2, bq2))

        if quote_sell:
            sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sq >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))
                if spread >= 9:
                    sell_px2 = _clip_tick(min(99, sell_px + 2))
                    if bid < sell_px2:
                        sq2 = self._sell_qty(sell_px2, size * 0.405, budget, state.yes_inventory)
                        if sq2 >= 0.01:
                            actions.append(PlaceOrder(Side.SELL, sell_px2, sq2))

        return actions
