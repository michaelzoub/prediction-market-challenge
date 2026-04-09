from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Ultra right-tail attempt: spread>=4 only, larger but inventory-capped sizing."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.abs_move = 0.0
        self.tox = 0.0
        self.stable = 0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.cooldown = 0

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
        move = mid - self.prev_mid
        self.prev_mid = mid

        moved = 0 if self.prev_bid is None else abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        self.prev_bid, self.prev_ask = bid, ask
        self.stable = self.stable + 1 if moved <= 1 else 0

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        self.abs_move = 0.94 * self.abs_move + 0.06 * abs(move)
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fills)

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        if self.tox > 0.65 or self.abs_move > 0.8:
            self.cooldown = 8
            return actions

        if spread < 4 or self.stable < 3:
            return actions
        if self.tox > 0.22:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        budget = free_cash * 0.24

        buy_tick = _clip_tick(bid + 1)
        sell_tick = _clip_tick(ask - 1)
        if buy_tick >= sell_tick:
            return actions

        signal = 0.55 * move + 0.45 * (state.buy_filled_quantity - state.sell_filled_quantity)
        buy_size = 10.5
        sell_size = 10.5
        if signal > 0.12:
            buy_size = 14.0
            sell_size = 6.0
        elif signal < -0.12:
            sell_size = 14.0
            buy_size = 6.0

        if inv > 30:
            buy_size = min(buy_size, 4.0)
            sell_size = max(sell_size, 10.0)
        elif inv < -30:
            sell_size = min(sell_size, 4.0)
            buy_size = max(buy_size, 10.0)

        if inv < 150:
            q_buy = self._safe_buy(buy_tick, buy_size, budget)
            if q_buy >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * 0.24

        if inv > -150:
            q_sell = self._safe_sell(sell_tick, sell_size, budget, state.yes_inventory)
            if q_sell >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))

        return actions
