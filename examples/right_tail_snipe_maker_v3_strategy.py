from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Higher-conviction right-tail maker with stricter toxicity stop."""

    def __init__(self) -> None:
        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._prev_mid = 50.0
        self._tox = 0.0
        self._flow = 0.0
        self._spread4_streak = 0
        self._cooldown = 0

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
        dmid = mid - self._prev_mid
        self._prev_mid = mid

        flow = state.buy_filled_quantity - state.sell_filled_quantity
        self._flow = 0.82 * self._flow + 0.18 * flow
        adverse = 0.0
        if state.buy_filled_quantity > 0 and dmid < 0:
            adverse += abs(dmid) * state.buy_filled_quantity
        if state.sell_filled_quantity > 0 and dmid > 0:
            adverse += abs(dmid) * state.sell_filled_quantity
        self._tox = 0.90 * self._tox + 0.10 * (0.85 * adverse + 0.15 * abs(dmid))

        if spread >= 4:
            self._spread4_streak += 1
        else:
            self._spread4_streak = 0

        if self._cooldown > 0:
            self._cooldown -= 1
            return [CancelAll()]

        if self._tox > 1.05:
            self._cooldown = 7
            return [CancelAll()]

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        actions: list[object] = [CancelAll()]

        # Very selective: only stable wide-touch window.
        if spread < 4 or self._spread4_streak < 5:
            return actions
        if self._tox > 0.26:
            return actions

        signal = 0.55 * dmid + 0.45 * self._flow
        if abs(signal) < 0.22:
            return actions

        if signal > 0 and inv < 110:
            px = _clip_tick(bid + 1)
            if px >= ask:
                return actions
            size = 8.6 if signal > 0.55 else 5.6
            qty = self._safe_buy_qty(px, size, free_cash * 0.20)
            if qty >= 0.01:
                actions.append(PlaceOrder(Side.BUY, px, qty))
            return actions

        if signal < 0 and inv > -110:
            px = _clip_tick(ask - 1)
            if px <= bid:
                return actions
            size = 8.6 if signal < -0.55 else 5.6
            qty = self._safe_sell_qty(px, size, free_cash * 0.20, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(Side.SELL, px, qty))
            return actions

        return actions
