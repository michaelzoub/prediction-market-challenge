from __future__ import annotations

from typing import Sequence

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Jump-aware quoter with inventory-first controls."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.ewma_abs_move = 0.0
        self.shock = 0.0
        self.total_steps: int | None = None

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(tick / 100.0, 0.01)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered_cap = free_cash / max(1.0 - price, 0.01)
        return _q(min(target, covered + uncovered_cap))

    def on_step(self, state: StepState) -> Sequence[object]:
        if self.total_steps is None:
            self.total_steps = max(1, state.step + state.steps_remaining)

        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        mid = 0.5 * (bid + ask)
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        self.ewma_abs_move = 0.94 * self.ewma_abs_move + 0.06 * abs(dmid)
        surprise = max(0.0, abs(dmid) - (self.ewma_abs_move + 0.2))
        one_sided = abs(state.buy_filled_quantity - state.sell_filled_quantity)
        self.shock = 0.9 * self.shock + 0.1 * (0.8 * surprise + 0.2 * min(3.0, one_sided))

        inv = state.yes_inventory - state.no_inventory
        time_frac = state.steps_remaining / max(1, self.total_steps)

        base_w = 2.0 + min(3.0, 5.0 * self.shock)
        # Early horizon: protect from jump risk; late horizon: tighten a bit.
        width = base_w + 0.8 * time_frac
        inv_skew = 0.06 * inv * (0.4 + time_frac)
        trend_skew = 1 if dmid > 0.35 else (-1 if dmid < -0.35 else 0)
        center = mid - inv_skew + trend_skew

        buy_tick = _clip_tick(int(round(center - width)))
        sell_tick = _clip_tick(int(round(center + width)))
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        # Increase size only when market looks calm.
        size = 5.0 if self.shock < 0.18 else (3.0 if self.shock < 0.35 else 1.8)
        if abs(inv) > 80:
            size = min(size, 1.5)

        quote_buy = inv < 135
        quote_sell = inv > -135

        # In strong trend/shock, avoid adding to directional risk.
        if self.shock > 0.45:
            if dmid > 0:
                quote_buy = inv < 0
            elif dmid < 0:
                quote_sell = inv > 0

        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)

        if quote_buy:
            qty = self._safe_buy_qty(buy_tick, size, free_cash * 0.2)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * qty)

        if quote_sell:
            qty = self._safe_sell_qty(sell_tick, size, free_cash * 0.2, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=qty))

        return actions
