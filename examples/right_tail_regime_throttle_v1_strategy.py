from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Regime-throttle right-tail strategy with burst budgeting."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.abs_move = 0.0
        self.tox = 0.0
        self.flow = 0.0
        self.spread_streak = 0
        self.cooldown = 0
        self.burst_budget = 0.0

    def _safe_buy(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _safe_sell(self, tick: int, qty: float, cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - px)
        return _q(min(qty, covered + uncovered))

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
        self.abs_move = 0.93 * self.abs_move + 0.07 * abs(dmid)
        self.flow = 0.86 * self.flow + 0.14 * fill_imb
        self.tox = 0.90 * self.tox + 0.10 * (abs(dmid) * fills)

        if spread >= 4:
            self.spread_streak += 1
            self.burst_budget = min(8.0, self.burst_budget + 0.35)
        else:
            self.spread_streak = 0
            self.burst_budget = max(0.0, self.burst_budget - 0.8)

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions
        if self.tox > 0.8 or self.abs_move > 0.75:
            self.cooldown = 8
            self.burst_budget = max(0.0, self.burst_budget - 2.0)
            return actions
        if spread < 3 or self.spread_streak < 3:
            return actions
        if self.tox > 0.42:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        center = int(round(mid - 0.03 * inv + 0.45 * self.flow + 0.35 * dmid))
        width = 1
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            return actions

        signal = 0.55 * dmid + 0.45 * self.flow
        if abs(signal) < 0.06:
            return actions

        base = 2.0 + 0.55 * self.burst_budget
        if abs(inv) > 85:
            base = min(base, 2.0)

        budget = free_cash * 0.18
        if signal > 0 and inv < 130:
            px = _clip_tick(min(ask - 1, max(bid + 1, buy_tick)))
            q = self._safe_buy(px, base, budget)
            if q >= 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, q))
            self.burst_budget = max(0.0, self.burst_budget - 0.5)
            return actions

        if signal < 0 and inv > -130:
            px = _clip_tick(max(bid + 1, min(ask - 1, sell_tick)))
            q = self._safe_sell(px, base, budget, state.yes_inventory)
            if q >= 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, q))
            self.burst_budget = max(0.0, self.burst_budget - 0.5)
            return actions

        return actions
