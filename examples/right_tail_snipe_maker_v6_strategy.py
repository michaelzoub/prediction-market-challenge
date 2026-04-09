from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """v6: event-burst strategy targeting right-tail episodes."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.tox = 0.0
        self.vol = 0.0
        self.flow = 0.0
        self.stable = 0
        self.burst = 0
        self.cooldown = 0

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
        move = mid - self.prev_mid
        self.prev_mid = mid

        moved = 0 if self.prev_bid is None else abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        self.prev_bid, self.prev_ask = bid, ask
        if moved <= 1:
            self.stable += 1
        else:
            self.stable = 0

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow = 0.84 * self.flow + 0.16 * fill_imb
        self.vol = 0.92 * self.vol + 0.08 * abs(move)
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fill_sum)

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        if self.tox > 0.95:
            self.cooldown = 6
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # Trigger a burst when wide spread becomes stable and low-tox.
        if spread >= 4 and self.stable >= 3 and self.tox < 0.28 and self.vol < 0.42:
            self.burst = max(self.burst, 8)
        if self.burst > 0:
            self.burst -= 1

        if spread < 3:
            return actions
        if self.tox > 0.40:
            return actions

        signal = 0.35 * move + 0.65 * self.flow

        # Inside touch for fill priority.
        buy_tick = _clip_tick(min(ask - 1, bid + 1))
        sell_tick = _clip_tick(max(bid + 1, ask - 1))
        if buy_tick >= sell_tick:
            return actions

        base_size = 2.6 if spread == 3 else 3.4
        if self.burst > 0:
            base_size = 7.2
        if abs(inv) > 80:
            base_size = min(base_size, 2.0)

        quote_buy = inv < 110
        quote_sell = inv > -110
        if abs(signal) > 0.16:
            if signal > 0:
                quote_sell = inv > 0
            else:
                quote_buy = inv < 0

        budget = free_cash * 0.23
        if quote_buy:
            q_buy = self._safe_buy_qty(buy_tick, base_size, budget)
            if q_buy >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                budget = free_cash * 0.23

        if quote_sell:
            q_sell = self._safe_sell_qty(sell_tick, base_size, budget, state.yes_inventory)
            if q_sell >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))

        return actions
