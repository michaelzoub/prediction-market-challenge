from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Best high-risk searched variant (v9)."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell(self, tick: int, qty: float, cash: float, yes_inv: float) -> float:
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

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.89 * self.abs_move + 0.11 * abs(move)
        self.flow = 0.891 * self.flow + 0.109 * fill_imb
        self.tox = 0.919 * self.tox + 0.081 * (abs(move) * fill_sum)
        self.streak = self.streak + 1 if spread >= 4 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions
        if self.tox > 0.886 or self.abs_move > 0.834:
            self.cool = 6
            return actions
        if spread < 4 or self.streak < 4:
            return actions
        if self.tox > 0.501 or self.abs_move > 0.522:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free = max(0.0, state.free_cash)

        center = int(round(mid - 0.339 * move - 0.62 * self.flow - 0.041 * inv))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        quote_buy = inv < 260
        quote_sell = inv > -260
        size = 5.34

        if quote_buy:
            px_buy = _clip_tick(min(ask - 1, max(bid, buy_tick)))
            q_buy = self._buy(px_buy, size, free * 0.596)
            if q_buy >= 0.01 and px_buy < ask:
                actions.append(PlaceOrder(Side.BUY, px_buy, q_buy))
                free = max(0.0, free - (px_buy / 100.0) * q_buy)

        if quote_sell:
            px_sell = _clip_tick(max(bid + 1, min(ask, sell_tick)))
            q_sell = self._sell(px_sell, size, free * 0.596, state.yes_inventory)
            if q_sell >= 0.01 and bid < px_sell:
                actions.append(PlaceOrder(Side.SELL, px_sell, q_sell))

        return actions
