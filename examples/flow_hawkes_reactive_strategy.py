from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Flow-reactive quoter with Hawkes-like fill imbalance intensity."""

    def __init__(self) -> None:
        self.buy_intensity = 0.0
        self.sell_intensity = 0.0
        self.prev_mid = 50.0
        self.vol = 0.0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        px = tick / 100.0
        return _q(min(target, free_cash / max(px, 0.01)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(1.0 - px, 0.01)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        mid = 0.5 * (bid + ask)

        dmid = mid - self.prev_mid
        self.prev_mid = mid
        self.vol = 0.92 * self.vol + 0.08 * abs(dmid)

        # Hawkes-like update: intensity is persistent and self-exciting with new fills.
        self.buy_intensity = 0.88 * self.buy_intensity + 0.35 * state.buy_filled_quantity
        self.sell_intensity = 0.88 * self.sell_intensity + 0.35 * state.sell_filled_quantity
        flow_imb = self.buy_intensity - self.sell_intensity

        inv = state.yes_inventory - state.no_inventory
        tox = abs(dmid) * (state.buy_filled_quantity + state.sell_filled_quantity)

        width = 2
        if self.vol > 0.55 or tox > 1.0:
            width = 3
        if self.vol > 0.95 or tox > 2.0:
            width = 5

        flow_skew = 0
        if flow_imb > 1.5:
            flow_skew = 1
        elif flow_imb < -1.5:
            flow_skew = -1

        inv_skew = int(round(-0.05 * inv))
        center = int(round(mid)) + flow_skew + inv_skew
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        size = 4.0
        if tox > 1.2:
            size = 2.5
        if abs(inv) > 90:
            size = min(size, 1.8)

        quote_buy = inv < 160
        quote_sell = inv > -160
        if tox > 2.2:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)
        budget = free_cash * 0.24

        if quote_buy:
            q_buy = self._safe_buy_qty(buy_tick, size, budget)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * 0.24

        if quote_sell:
            q_sell = self._safe_sell_qty(sell_tick, size, budget, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=q_sell))

        return actions
