from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """v5: v4 with adaptive burst sizing in spread>=4 windows."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.total_steps: int | None = None
        self.streak = 0
        self.burst = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        if self.total_steps is None:
            self.total_steps = max(1, state.step + state.steps_remaining)

        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        fills_last = state.buy_filled_quantity + state.sell_filled_quantity
        self.abs_move = 0.93 * self.abs_move + 0.07 * abs(move)
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fills_last)
        flow = state.buy_filled_quantity - state.sell_filled_quantity

        if spread >= 4:
            self.streak += 1
        else:
            self.streak = 0
            self.burst = 0

        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)
        inv = state.yes_inventory - state.no_inventory

        if spread < 3:
            return actions
        if self.tox > 0.55 or self.abs_move > 0.55:
            self.burst = 0
            return actions

        if spread >= 4 and self.streak >= 3 and self.tox < 0.24:
            self.burst = min(12, self.burst + 2)
        elif self.burst > 0:
            self.burst -= 1

        frac = state.steps_remaining / max(1, self.total_steps)
        if frac < 0.12 and self.tox > 0.18:
            return actions

        center = int(round(mid - 0.03 * inv))
        width = 2 if spread >= 4 else 1
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        # Burst mode: larger one-sided quote in flow direction.
        if self.burst > 0 and spread >= 4 and abs(flow) > 0.6 and abs(inv) < 90:
            budget = free_cash * 0.28
            if flow > 0:
                px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
                qty = self._safe_buy_qty(px, 6.8, budget)
                if qty >= 0.01 and px < ask:
                    actions.append(PlaceOrder(Side.BUY, px, qty))
                    return actions
            else:
                px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
                qty = self._safe_sell_qty(px, 6.8, budget, state.yes_inventory)
                if qty >= 0.01 and bid < px:
                    actions.append(PlaceOrder(Side.SELL, px, qty))
                    return actions

        size = 3.8 if spread >= 4 else 2.2
        if abs(inv) > 65:
            size = min(size, 1.7)

        quote_buy = inv < 95
        quote_sell = inv > -95
        if self.tox > 0.30:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.20
        if quote_buy:
            q_buy = self._safe_buy_qty(buy_tick, size, budget)
            if q_buy >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * 0.20

        if quote_sell:
            q_sell = self._safe_sell_qty(sell_tick, size, budget, state.yes_inventory)
            if q_sell >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))

        return actions
