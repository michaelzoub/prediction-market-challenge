from __future__ import annotations

from typing import Sequence

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import Action, CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Half-reverse variant: reverse center bias only, keep safer sizing."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.fast_ret = 0.0
        self.slow_ret = 0.0
        self.last_fill_imb = 0.0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        max_qty = free_cash / max(tick / 100.0, 0.01)
        return _q(min(target, max_qty))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered_cap = free_cash / max(1.0 - price, 0.01)
        return _q(min(target, covered + uncovered_cap))

    def on_step(self, state: StepState) -> Sequence[Action]:
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        mid = 0.5 * (bid + ask)

        ret = mid - self.prev_mid
        self.prev_mid = mid
        self.fast_ret = 0.75 * self.fast_ret + 0.25 * ret
        self.slow_ret = 0.95 * self.slow_ret + 0.05 * ret
        trend = self.fast_ret - self.slow_ret

        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.last_fill_imb = 0.8 * self.last_fill_imb + 0.2 * fill_imb

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        trend_skew = 0
        if trend > 0.22:
            trend_skew = -1
        elif trend < -0.22:
            trend_skew = 1

        inv_skew = int(round(-0.03 * inv))
        center = int(mid) + trend_skew + inv_skew

        width = 3 if abs(trend) > 0.35 else 2
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        base_size = 3.0
        if abs(inv) > 70:
            base_size = 1.8

        # Keep original (not reversed) rebalancing to reduce inventory blowups.
        buy_size = base_size
        sell_size = base_size
        if self.last_fill_imb > 1.0:
            sell_size = min(4.2, base_size + 0.8)
        elif self.last_fill_imb < -1.0:
            buy_size = min(4.2, base_size + 0.8)

        quote_buy = inv < 130
        quote_sell = inv > -130

        actions: list[Action] = [CancelAll()]
        if quote_buy:
            qty = self._safe_buy_qty(buy_tick, buy_size, free_cash * 0.2)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * qty)

        if quote_sell:
            qty = self._safe_sell_qty(sell_tick, sell_size, free_cash * 0.2, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=qty))

        return actions
