from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """HMM-filtered spread5 hybrid v2 from local search."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.p_toxic = 0.231
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
        self.abs_move = 0.918 * self.abs_move + 0.082 * abs(move)
        self.flow = 0.932 * self.flow + 0.068 * fill_imbalance
        self.tox = 0.904 * self.tox + 0.096 * (abs(move) * fills)

        adverse = 0.0
        if state.buy_filled_quantity > 0 and move < 0:
            adverse += abs(move) * state.buy_filled_quantity
        if state.sell_filled_quantity > 0 and move > 0:
            adverse += abs(move) * state.sell_filled_quantity
        p_like = min(1.0, 0.974 * adverse + 0.273 * abs(move) + 0.255 * abs(fill_imbalance))
        self.p_toxic = (1 - 0.055) * self.p_toxic + 0.055 * p_like

        self.streak = self.streak + 1 if spread >= 4 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions
        if self.tox > 1.287 or self.abs_move > 0.888:
            self.cool = 2
            return actions

        if spread < 4.52 or self.streak < 4:
            return actions
        if self.tox > 0.547 or self.abs_move > 0.498:
            return actions
        if self.p_toxic > 0.383:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(mid - 0.131 * move - 0.276 * self.flow - 0.056 * inv - 0.243 * (0.5 - self.p_toxic)))
        width = 2
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

        signal = 1.031 * move + 0.378 * self.flow
        quote_buy = inv < 160
        quote_sell = inv > -160
        if signal > 0.473:
            quote_sell = False
        elif signal < -0.473:
            quote_buy = False

        size = 2.997 * (1 - 0.547 * self.p_toxic)
        size = max(2.616, size)
        if abs(inv) > 50:
            size = min(size, 2.284)

        budget = free_cash * 0.625
        if quote_buy:
            buy_qty = self._buy_qty(buy_px, size, budget)
            if buy_qty >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)
                budget = free_cash * 0.625
                if spread >= 7:
                    buy_px2 = _clip_tick(max(1, buy_px - 1))
                    if buy_px2 < ask:
                        buy_qty2 = self._buy_qty(buy_px2, size * 0.895, budget)
                        if buy_qty2 >= 0.01:
                            actions.append(PlaceOrder(Side.BUY, buy_px2, buy_qty2))

        if quote_sell:
            sell_qty = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))
                if spread >= 7:
                    sell_px2 = _clip_tick(min(99, sell_px + 1))
                    if bid < sell_px2:
                        sell_qty2 = self._sell_qty(sell_px2, size * 0.895, budget, state.yes_inventory)
                        if sell_qty2 >= 0.01:
                            actions.append(PlaceOrder(Side.SELL, sell_px2, sell_qty2))

        return actions
