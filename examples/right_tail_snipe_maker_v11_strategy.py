from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Right-tail snipe v11: Kalman-lite + inv defense."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.fair_value = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0

    def _buy_qty(self, tick: int, base_qty: float, cash: float, inv: float) -> float:
        skew = max(0.0, -inv / 20.0)
        return _q(min(base_qty * (1 + skew), cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, base_qty: float, cash: float, yes_inv: float, inv: float) -> float:
        price = tick / 100.0
        skew = max(0.0, inv / 20.0)
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - price)
        return _q(min(base_qty * (1 + skew), covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        # Kalman-lite fair value.
        self.fair_value = 0.7 * self.fair_value + 0.3 * mid

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imbalance = state.buy_filled_quantity - state.sell_filled_quantity

        self.abs_move = 0.909 * self.abs_move + 0.091 * abs(move)
        self.flow = 0.845 * self.flow + 0.155 * fill_imbalance
        self.tox = 0.909 * self.tox + 0.091 * (abs(move) * fill_sum)

        self.streak += 1 if spread >= (3 + min(self.streak, 3)) else -1
        self.streak = max(0, min(10, self.streak))

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 0.55 or self.abs_move > 0.60 or abs(self.flow) > 1.8:
            self.cool = 3
            return actions

        if spread < 3.5 or self.streak < 2:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        if abs(inventory) > 20:
            return actions

        center = int(round(self.fair_value - 0.15 * move - 0.35 * self.flow - 0.04 * inventory))
        buy_tick = _clip_tick(center - 1)
        sell_tick = _clip_tick(center + 1)

        buy_tick2 = None
        sell_tick2 = None
        if spread > 6:
            buy_tick2 = _clip_tick(center - 3)
            sell_tick2 = _clip_tick(center + 3)

        if buy_tick >= sell_tick:
            return actions

        base_size = min(3.2, 0.8 / (1 + abs(inventory) / 20.0))

        buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip_tick(max(bid, min(ask, sell_tick)))

        buy_qty = self._buy_qty(buy_px, base_size, free_cash * 0.22, inventory)
        if buy_qty >= 0.01 and buy_px < ask:
            actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
            if buy_tick2 is not None and buy_tick2 < buy_px:
                actions.append(PlaceOrder(Side.BUY, buy_tick2, buy_qty * 0.4))
            free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

        sell_qty = self._sell_qty(sell_px, base_size, free_cash * 0.22, state.yes_inventory, inventory)
        if sell_qty >= 0.01 and bid < sell_px:
            actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))
            if sell_tick2 is not None and sell_tick2 > sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_tick2, sell_qty * 0.4))

        return actions
