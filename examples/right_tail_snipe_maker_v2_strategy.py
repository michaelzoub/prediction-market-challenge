from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(v: float) -> float:
    return max(0.0, round(v, 2))


class Strategy(BaseStrategy):
    """Tuned right-tail snipe maker (v2) with stricter toxicity guard."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.mid_vol = 0.0
        self.tox = 0.0
        self.flow_imb = 0.0
        self.cooldown = 0

    def _buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        p = tick / 100.0
        return _q(min(target, max(0.0, yes_inv) + free_cash / max(0.01, 1.0 - p)))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        self.mid_vol = 0.92 * self.mid_vol + 0.08 * abs(dmid)
        fill_total = state.buy_filled_quantity + state.sell_filled_quantity
        self.flow_imb = 0.85 * self.flow_imb + 0.15 * (state.buy_filled_quantity - state.sell_filled_quantity)
        adverse = 0.0
        if state.buy_filled_quantity > 0 and dmid < 0:
            adverse += abs(dmid) * state.buy_filled_quantity
        if state.sell_filled_quantity > 0 and dmid > 0:
            adverse += abs(dmid) * state.sell_filled_quantity
        self.tox = 0.9 * self.tox + 0.1 * (0.75 * adverse + 0.25 * abs(dmid) * fill_total)

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        actions: list[object] = [CancelAll()]

        if self.cooldown > 0:
            self.cooldown -= 1
            return actions
        if abs(dmid) > max(0.9, self.mid_vol + 0.55):
            self.cooldown = 5
            return actions

        if spread < 3:
            return actions
        if self.tox > 1.35:
            return actions

        bullish = dmid > 0.10 or self.flow_imb > 0.7
        bearish = dmid < -0.10 or self.flow_imb < -0.7
        if bullish and bearish:
            return actions

        size = 2.4
        if spread >= 4 and self.tox < 0.55 and self.mid_vol < 0.38:
            size = 4.8
        elif spread >= 4:
            size = 3.2
        if abs(inv) > 60:
            size = min(size, 2.0)

        if bullish and inv < 100:
            px = _clip_tick(min(ask - 1, bid + 1))
            if px < ask:
                q = self._buy_qty(px, size, free_cash * 0.18)
                if q >= 0.01:
                    actions.append(PlaceOrder(side=Side.BUY, price_ticks=px, quantity=q))
            return actions

        if bearish and inv > -100:
            px = _clip_tick(max(bid + 1, ask - 1))
            if px > bid:
                q = self._sell_qty(px, size, free_cash * 0.18, state.yes_inventory)
                if q >= 0.01:
                    actions.append(PlaceOrder(side=Side.SELL, price_ticks=px, quantity=q))
            return actions

        return actions
