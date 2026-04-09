from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Hybrid: spread5 harvester + partial-info HMM gating."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0
        self.p_good = 0.5

    def _buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - price)
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

        fills = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.abs_move = 0.91 * self.abs_move + 0.09 * abs(move)
        self.flow = 0.86 * self.flow + 0.14 * fill_imb
        self.tox = 0.91 * self.tox + 0.09 * (abs(move) * fills)
        self.streak = self.streak + 1 if spread >= 5 else 0

        # Partial-info regime update.
        evidence = 0.0
        if spread >= 5:
            evidence += 0.45
        if self.tox < 0.35:
            evidence += 0.30
        if abs(fill_imb) < 1.0:
            evidence += 0.15
        if self.abs_move < 0.45:
            evidence += 0.10
        self.p_good = 0.88 * self.p_good + 0.12 * evidence

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.0 or self.abs_move > 0.70:
            self.cool = 5
            return actions

        if spread < 5 or self.streak < 2:
            return actions
        if self.tox > 0.55 or self.abs_move > 0.52:
            return actions
        if self.p_good < 0.52:
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        center = int(round(mid - 0.20 * move - 0.35 * self.flow - 0.03 * inv))
        buy_tick = _clip_tick(center - 2)
        sell_tick = _clip_tick(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

        # Regime-scaled size: stronger in good regime, softer otherwise.
        size = 5.0 + 4.5 * max(0.0, self.p_good - 0.5)
        if abs(inv) > 80:
            size = 2.0

        quote_buy = inv < 180
        quote_sell = inv > -180
        if self.tox > 0.32:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.34
        if quote_buy:
            buy_qty = self._buy_qty(buy_px, size, budget)
            if buy_qty >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)
                budget = free_cash * 0.34

        if quote_sell:
            sell_qty = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
