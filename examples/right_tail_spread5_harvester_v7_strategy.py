from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Spread>=5 harvester v7 — optimized from 2-round parameter search (best: +2.68 / 200 sims)."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, target: float, cash: float) -> float:
        return _q(min(target, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, target: float, cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        return _q(min(target, max(0.0, yes_inv) + cash / max(0.01, 1.0 - px)))

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
        self.abs_move = 0.91 * self.abs_move + 0.09 * abs(move)
        self.flow = 0.80 * self.flow + 0.20 * fill_imb
        self.tox = 0.99 * self.tox + 0.01 * (abs(move) * fill_sum)
        self.streak = self.streak + 1 if spread >= 5 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.17 or self.abs_move > 0.70:
            self.cool = 5
            return actions
        if spread < 5 or self.streak < 2:
            return actions
        if self.tox > 0.74 or self.abs_move > 0.52:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        center = int(round(mid - 0.20 * move - 0.416 * self.flow - 0.04 * inv))
        buy_tick = _clip(center - 2)
        sell_tick = _clip(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip(max(bid + 1, min(ask, sell_tick)))

        size = 7.0
        if abs(inv) > 80:
            size = 2.46

        quote_buy = inv < 234
        quote_sell = inv > -234
        if self.tox > 0.336:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.426
        if quote_buy:
            bq = self._buy_qty(buy_px, size, budget)
            if bq >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * 0.426

        if quote_sell:
            sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sq >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))

        return actions
