from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Wide-spread hybrid harvester with stricter shock and direction filters."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.prev_spread: int | None = None

        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.trend = 0.0
        self.wide_streak = 0
        self.stable_streak = 0
        self.cool = 0
        self.recovery = 0
        self.adverse_run = 0

    def _buy_qty(self, tick: int, qty: float, free_cash: float) -> float:
        return _q(min(qty, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - px)
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

        touch_move = (
            0
            if self.prev_bid is None or self.prev_ask is None
            else abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        )
        spread_move = 0 if self.prev_spread is None else spread - self.prev_spread
        self.prev_bid = bid
        self.prev_ask = ask
        self.prev_spread = spread

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        adverse = 0.0
        if state.buy_filled_quantity > 0.0 and move < 0.0:
            adverse += abs(move) * state.buy_filled_quantity
        if state.sell_filled_quantity > 0.0 and move > 0.0:
            adverse += abs(move) * state.sell_filled_quantity

        self.abs_move = 0.906 * self.abs_move + 0.094 * abs(move)
        self.flow = 0.872 * self.flow + 0.128 * fill_imb
        self.tox = 0.902 * self.tox + 0.098 * (abs(move) * fill_sum + 0.8 * adverse)
        self.trend = 0.84 * self.trend + 0.16 * move
        self.wide_streak = self.wide_streak + 1 if spread >= 5 else 0
        self.stable_streak = self.stable_streak + 1 if touch_move <= 1 else 0
        if adverse > 0.25:
            self.adverse_run = min(8, self.adverse_run + 2)
        else:
            self.adverse_run = max(0, self.adverse_run - 1)

        actions: list[object] = [CancelAll()]

        if self.cool > 0:
            self.cool -= 1
            self.recovery = max(0, self.recovery - 1)
            return actions

        shock = (
            touch_move >= 4
            or spread_move >= 3
            or abs(move) > max(0.9, self.abs_move + 0.45)
        )
        if shock or self.tox > 0.92 or self.abs_move > 0.76:
            self.cool = 4 if spread >= 5 else 5
            self.recovery = 3
            return actions

        if spread < 5 or self.wide_streak < 2 or self.stable_streak < 1:
            return actions
        if self.tox > 0.47 or self.abs_move > 0.48:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        center = int(round(mid - 0.22 * move - 0.46 * self.flow - 0.05 * self.trend - 0.032 * inv))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if self.wide_streak >= 4 and self.stable_streak >= 3 and self.tox < 0.18:
            buy_tick = _clip_tick(center - 1)
            sell_tick = _clip_tick(center + 1)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
        if self.recovery > 0:
            buy_px = min(buy_px, bid)
            sell_px = max(sell_px, ask)

        size = 6.6
        if self.wide_streak >= 5 and self.stable_streak >= 3 and self.tox < 0.22:
            size = 7.6
        if abs(inv) > 70:
            size = min(size, 2.0)
        elif self.tox > 0.28:
            size *= 0.7

        signal = 0.74 * self.flow + 0.38 * move + 0.22 * self.trend
        quote_buy = inv < 185
        quote_sell = inv > -185
        if signal > 0.36:
            quote_sell = False
        elif signal < -0.36:
            quote_buy = False

        if self.tox > 0.30 or self.adverse_run >= 4:
            if inv > 0:
                quote_buy = False
            elif inv < 0:
                quote_sell = False
            elif self.tox > 0.34:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.40
        if quote_buy:
            buy_qty = self._buy_qty(buy_px, size, budget)
            if buy_qty >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)
                budget = free_cash * 0.40

        if quote_sell:
            sell_qty = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
