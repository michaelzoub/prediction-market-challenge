from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Pre-jump hunter with strict inventory constraints.

    Idea:
    - Detect likely pre-jump tension from widening spread + accelerating touch drift.
    - Pre-position one-sided when signal is strong.
    - Immediately revert to defensive quoting if adverse move or inventory stress appears.
    """

    def __init__(self) -> None:
        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._prev_mid = 50.0
        self._spread_ewma = 2.0
        self._abs_move_ewma = 0.0
        self._jump_score = 0.0
        self._cooldown = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        max_qty = free_cash / max(0.01, tick / 100.0)
        return _q(min(target, max_qty))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered_cap = free_cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered_cap))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        mid = 0.5 * (bid + ask)
        spread = ask - bid
        dmid = mid - self._prev_mid
        self._prev_mid = mid

        self._spread_ewma = 0.95 * self._spread_ewma + 0.05 * spread
        self._abs_move_ewma = 0.9 * self._abs_move_ewma + 0.1 * abs(dmid)

        flow_imb = state.buy_filled_quantity - state.sell_filled_quantity
        accel = 0.0
        if self._prev_bid is not None and self._prev_ask is not None:
            prev_mid = 0.5 * (self._prev_bid + self._prev_ask)
            accel = abs(mid - prev_mid) - self._abs_move_ewma
        self._prev_bid, self._prev_ask = bid, ask

        tension = max(0.0, spread - self._spread_ewma)
        prejump_signal = 0.55 * max(0.0, accel) + 0.30 * tension + 0.15 * abs(flow_imb)
        self._jump_score = 0.9 * self._jump_score + 0.1 * prejump_signal

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        actions: list[object] = [CancelAll()]

        # Defensive mode on recent instability.
        if self._cooldown > 0:
            self._cooldown -= 1
            width = 5
            size = 1.8
            center = int(round(mid - 0.05 * inv))
            buy_tick = _clip_tick(center - width)
            sell_tick = _clip_tick(center + width)
            if buy_tick < sell_tick:
                if inv < 120:
                    q_buy = self._safe_buy_qty(buy_tick, size, free_cash * 0.15)
                    if q_buy >= 0.01:
                        actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                        free_cash -= (buy_tick / 100.0) * q_buy
                if inv > -120:
                    q_sell = self._safe_sell_qty(sell_tick, size, free_cash * 0.15, state.yes_inventory)
                    if q_sell >= 0.01:
                        actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))
            return actions

        if abs(dmid) > 1.2:
            self._cooldown = 4

        # Base two-sided maker when no jump setup.
        width = 2 if self._jump_score < 0.35 else 3
        size = 4.0 if self._jump_score < 0.35 else 2.5
        center = int(round(mid - 0.04 * inv))

        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        # Pre-jump one-sided positioning when confidence is high.
        bullish = self._jump_score > 0.55 and dmid > 0 and flow_imb >= 0
        bearish = self._jump_score > 0.55 and dmid < 0 and flow_imb <= 0

        if bullish and inv < 90:
            px = _clip_tick(max(buy_tick, bid))
            q_buy = self._safe_buy_qty(px, 5.0, free_cash * 0.25)
            if q_buy >= 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, q_buy))
            return actions

        if bearish and inv > -90:
            px = _clip_tick(min(sell_tick, ask))
            q_sell = self._safe_sell_qty(px, 5.0, free_cash * 0.25, state.yes_inventory)
            if q_sell >= 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, q_sell))
            return actions

        if inv < 140:
            q_buy = self._safe_buy_qty(buy_tick, size, free_cash * 0.2)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash -= (buy_tick / 100.0) * q_buy
        if inv > -140:
            q_sell = self._safe_sell_qty(sell_tick, size, free_cash * 0.2, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))
        return actions
