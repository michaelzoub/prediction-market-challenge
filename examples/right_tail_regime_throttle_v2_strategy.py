from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Regime throttle v2: lower turnover, stronger toxicity veto."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.spread_streak = 0
        self.cooldown = 0
        self.burst = 0
        self.last_sign = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        adverse = 0.0
        if state.buy_filled_quantity > 0 and dmid < 0:
            adverse += abs(dmid) * state.buy_filled_quantity
        if state.sell_filled_quantity > 0 and dmid > 0:
            adverse += abs(dmid) * state.sell_filled_quantity

        self.abs_move = 0.93 * self.abs_move + 0.07 * abs(dmid)
        self.flow = 0.90 * self.flow + 0.10 * fill_imb
        self.tox = 0.90 * self.tox + 0.10 * (0.8 * adverse + 0.2 * abs(dmid) * fills)

        if spread >= 4:
            self.spread_streak += 1
        else:
            self.spread_streak = 0

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        if self.tox > 0.50 or self.abs_move > 0.72:
            self.cooldown = 6
            self.burst = 0
            return actions

        signal = 0.45 * dmid + 0.55 * self.flow
        sign = 1 if signal > 0.22 else (-1 if signal < -0.22 else 0)
        if sign != 0 and sign == self.last_sign and self.spread_streak >= 4 and self.tox < 0.26:
            self.burst = min(4, self.burst + 1)
        else:
            self.burst = max(0, self.burst - 1)
        self.last_sign = sign if sign != 0 else self.last_sign

        if spread < 3 or self.tox > 0.30:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        width = 1 if spread >= 4 else 2
        center = int(round(mid - 0.03 * inv))
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        size = 2.2 + 0.6 * self.burst
        if spread >= 4:
            size += 0.8
        if abs(inv) > 70:
            size = min(size, 1.5)

        quote_buy = inv < 95
        quote_sell = inv > -95
        if sign > 0:
            quote_sell = False
        elif sign < 0:
            quote_buy = False
        else:
            quote_buy = False
            quote_sell = False

        budget = free_cash * 0.16
        if quote_buy:
            px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
            if px < ask:
                q = self._safe_buy_qty(px, size, budget)
                if q >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, px, q))
                    free_cash = max(0.0, free_cash - (px / 100.0) * q)
                    budget = free_cash * 0.16

        if quote_sell:
            px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
            if px > bid:
                q = self._safe_sell_qty(px, size, budget, state.yes_inventory)
                if q >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, px, q))

        return actions
