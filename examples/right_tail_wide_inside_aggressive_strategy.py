from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Trade only wide/stable windows with inside-touch quotes."""

    def __init__(self) -> None:
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.prev_mid = 50.0
        self.stable = 0
        self.tox = 0.0
        self.abs_move = 0.0
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
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        move_ticks = 0 if self.prev_bid is None else abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        self.prev_bid, self.prev_ask = bid, ask
        if move_ticks <= 1:
            self.stable += 1
        else:
            self.stable = 0

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        self.abs_move = 0.93 * self.abs_move + 0.07 * abs(dmid)
        self.tox = 0.9 * self.tox + 0.1 * (abs(dmid) * fills)

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions
        if self.tox > 0.9 or self.abs_move > 0.9:
            self.cooldown = 6
            return actions

        # Focus where empirical edge per share is highest.
        if spread < 4 or self.stable < 4 or self.tox > 0.28:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        buy_tick = _clip_tick(bid + 1)
        sell_tick = _clip_tick(ask - 1)
        if buy_tick >= sell_tick:
            return actions

        # Aggressive sizing to capture right tail when conditions are favorable.
        size = 12.0
        if abs(inv) > 80:
            size = 4.0

        quote_buy = inv < 130
        quote_sell = inv > -130
        if self.tox > 0.2:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.35
        if quote_buy:
            q_buy = self._safe_buy(buy_tick, size, budget)
            if q_buy >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * 0.35

        if quote_sell:
            q_sell = self._safe_sell(sell_tick, size, budget, state.yes_inventory)
            if q_sell >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))

        return actions

