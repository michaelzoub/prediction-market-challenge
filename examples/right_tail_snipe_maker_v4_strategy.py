from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Right-tail capture v4: ultra-conservative toxic gating."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.total_steps: int | None = None

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

        inv = state.yes_inventory - state.no_inventory
        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)

        # Prefer spread=4 regimes; otherwise mostly don't trade.
        spread_score = 1.0 if spread >= 4 else (0.35 if spread == 3 else 0.0)
        if spread_score == 0.0:
            return actions

        # Very strict toxicity gate.
        if self.tox > 0.45 or self.abs_move > 0.5:
            return actions

        # Late simulation: shrink activity to protect tail.
        frac = state.steps_remaining / max(1, self.total_steps)
        if frac < 0.12 and self.tox > 0.20:
            return actions

        center = int(round(mid - 0.03 * inv))
        width = 2 if spread >= 4 else 1
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        size = 3.4 if spread >= 4 else 2.0
        if abs(inv) > 60:
            size = min(size, 1.4)

        # Quote both sides only when very safe; else one-side reduce risk.
        quote_buy = inv < 80
        quote_sell = inv > -80
        if self.tox > 0.28:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * (0.16 + 0.04 * spread_score)
        if quote_buy:
            q_buy = self._safe_buy_qty(buy_tick, size, budget)
            if q_buy >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * (0.16 + 0.04 * spread_score)

        if quote_sell:
            q_sell = self._safe_sell_qty(sell_tick, size, budget, state.yes_inventory)
            if q_sell >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))

        return actions
