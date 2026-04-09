from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(t: int) -> int:
    return max(1, min(99, t))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Spread>=5 harvester v3: one-sided directional bursts only."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.flow = 0.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.streak = 0

    def _buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        p = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - p)
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

        fsum = state.buy_filled_quantity + state.sell_filled_quantity
        fimb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow = 0.86 * self.flow + 0.14 * fimb
        self.abs_move = 0.91 * self.abs_move + 0.09 * abs(move)
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fsum)
        self.streak = self.streak + 1 if spread >= 5 else 0

        actions: list[object] = [CancelAll()]
        if spread < 5 or self.streak < 3:
            return actions
        if self.tox > 0.50 or self.abs_move > 0.48:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free = max(0.0, state.free_cash)

        signal = 0.8 * move + 1.2 * self.flow
        if abs(signal) < 0.18:
            return actions

        size = 14.0
        if abs(inv) > 140:
            size = 6.0

        if signal > 0 and inv < 350:
            px = _clip_tick(bid + 1)
            if px < ask:
                q = self._buy_qty(px, size, free * 0.95)
                if q >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, px, q))
            return actions

        if signal < 0 and inv > -350:
            px = _clip_tick(ask - 1)
            if px > bid:
                q = self._sell_qty(px, size, free * 0.95, state.yes_inventory)
                if q >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, px, q))
            return actions

        return actions
