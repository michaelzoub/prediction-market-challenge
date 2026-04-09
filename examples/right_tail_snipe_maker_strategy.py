from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Snipe style maker: only quote when spread is wide and touch is stable."""

    def __init__(self) -> None:
        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._stable = 0
        self._pause = 0
        self._prev_mid = 50.0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None or ask <= bid:
            return [CancelAll()]
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        dmid = mid - self._prev_mid
        self._prev_mid = mid

        moved = 0 if self._prev_bid is None else abs(bid - self._prev_bid) + abs(ask - self._prev_ask)
        if moved <= 1:
            self._stable += 1
        else:
            self._stable = 0
        self._prev_bid, self._prev_ask = bid, ask

        if abs(dmid) > 0.9:
            self._pause = 6
        if self._pause > 0:
            self._pause -= 1
            return [CancelAll()]

        actions: list[object] = [CancelAll()]
        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # Only attack in wide and stable windows.
        if spread < 4 or self._stable < 4:
            return actions

        # Symmetric with mild inventory skew to avoid drift.
        inv_skew = int(round(-0.02 * inv))
        buy_tick = _clip_tick(bid + 1 + inv_skew)
        sell_tick = _clip_tick(ask - 1 + inv_skew)
        if buy_tick >= sell_tick:
            return actions

        size = 4.8
        if abs(inv) > 80:
            size = 2.4

        if inv < 155:
            q_buy = self._safe_buy_qty(buy_tick, size, free_cash * 0.22)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
        if inv > -155:
            q_sell = self._safe_sell_qty(sell_tick, size, free_cash * 0.22, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))
        return actions
