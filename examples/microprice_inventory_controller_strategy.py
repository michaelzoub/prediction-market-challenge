from __future__ import annotations

from typing import Sequence

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import Action, CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Microprice-style center with strong inventory mean reversion."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.fast_mid = 50.0
        self.slow_mid = 50.0
        self.move_ewma = 0.0
        self.flow_imb = 0.0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(tick / 100.0, 0.01)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(1.0 - price, 0.01)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState) -> Sequence[Action]:
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        self.fast_mid = 0.75 * self.fast_mid + 0.25 * mid
        self.slow_mid = 0.96 * self.slow_mid + 0.04 * mid
        self.move_ewma = 0.9 * self.move_ewma + 0.1 * abs(move)

        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow_imb = 0.85 * self.flow_imb + 0.15 * fill_imb

        inv = state.yes_inventory - state.no_inventory
        spread = ask - bid

        # Microprice proxy from trend + flow.
        trend = self.fast_mid - self.slow_mid
        flow_skew = 0.12 * self.flow_imb
        inv_skew = -0.05 * inv
        center = mid + trend + flow_skew + inv_skew

        width = 2.0 + min(2.5, 1.8 * self.move_ewma)
        if spread >= 4:
            width += 1.0
        if abs(inv) > 70:
            width += 1.0

        buy_tick = _clip_tick(int(round(center - width)))
        sell_tick = _clip_tick(int(round(center + width)))
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        size = 4.5
        if abs(inv) > 100:
            size = 2.0
        elif self.move_ewma > 0.55:
            size = 3.0

        # Bias toward flattening.
        buy_size = size
        sell_size = size
        if inv > 20:
            sell_size = min(6.0, size + 1.2)
            buy_size = max(1.5, size - 1.0)
        elif inv < -20:
            buy_size = min(6.0, size + 1.2)
            sell_size = max(1.5, size - 1.0)

        quote_buy = inv < 160
        quote_sell = inv > -160

        actions: list[Action] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)

        if quote_buy:
            qty = self._safe_buy_qty(buy_tick, buy_size, free_cash * 0.23)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * qty)

        if quote_sell:
            qty = self._safe_sell_qty(sell_tick, sell_size, free_cash * 0.23, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=qty))

        return actions
