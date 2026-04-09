from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Price-discount control under inferred toxicity.

    Practitioner-inspired approximation:
    - In low toxicity, offer small discount (inside touch) to attract uninformed flow.
    - In high toxicity, withdraw discount and/or quote farther from touch.
    """

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, qty: float, free_cash: float) -> float:
        return _q(min(qty, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, free_cash: float, yes_inventory: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = free_cash / max(0.01, 1.0 - px)
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
        self.abs_move = 0.90 * self.abs_move + 0.10 * abs(move)
        self.flow = 0.87 * self.flow + 0.13 * fill_imb
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fill_sum)
        self.streak = self.streak + 1 if spread >= 4 else 0

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.05 or self.abs_move > 0.80:
            self.cool = 4
            return actions

        if spread < 4 or self.streak < 2:
            return actions
        if self.tox > 0.65 or self.abs_move > 0.60:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # Toxicity-dependent discounting depth.
        if self.tox < 0.18:
            discount = 1  # inside touch
            width = 1
        elif self.tox < 0.35:
            discount = 0  # at touch
            width = 2
        else:
            discount = -1  # behind touch
            width = 3

        center = int(round(mid - 0.18 * move - 0.38 * self.flow - 0.03 * inv))
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip_tick(min(ask - 1, max(bid + discount, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask - discount, sell_tick)))
        if buy_px >= sell_px:
            return actions

        size = 5.4 if self.tox < 0.25 else 3.4
        if abs(inv) > 70:
            size = min(size, 2.0)

        quote_buy = inv < 200
        quote_sell = inv > -200
        if self.tox > 0.42:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.35
        if quote_buy:
            bq = self._buy_qty(buy_px, size, budget)
            if bq >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * 0.35

        if quote_sell:
            sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sq >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))

        return actions

