from __future__ import annotations

import math

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class KalmanFairValue:
    def __init__(self):
        self.x = 50.0
        self.v = 0.0
        self.P = 1.0
        self.Q = 0.1
        self.R = 0.5

    def update(self, z: float) -> float:
        self.x += self.v
        self.P += self.Q

        k_gain = self.P / (self.P + self.R)
        self.x += k_gain * (z - self.x)
        self.P *= 1 - k_gain

        self.v = (self.x - 50.0) * 0.1
        return self.x


class Strategy(BaseStrategy):
    """Right-tail snipe v10: Kalman fair value + multi-level fading."""

    def __init__(self) -> None:
        self.kf = KalmanFairValue()
        self.prev_mid = 50.0
        self.tox = 0.0
        self.abs_move = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0
        self.unfilled_steps = 0
        self.regret = 0.0

    def _buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = cash / max(0.01, 1.0 - price)
        return _q(min(qty, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks or 49
        ask = state.competitor_best_ask_ticks or 51
        if ask <= bid:
            ask = min(99, bid + 1)

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid
        fair = self.kf.update(mid)

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imbalance = state.buy_filled_quantity - state.sell_filled_quantity
        recent_fills = fill_sum > 0

        self.abs_move = 0.909 * self.abs_move + 0.091 * abs(move)
        self.flow = 0.845 * self.flow + 0.155 * fill_imbalance
        self.tox = 0.909 * self.tox + 0.091 * (abs(move - (fair - 50.0)) * fill_sum)

        self.streak = self.streak + 1 if spread >= 4 else 0
        if recent_fills:
            self.unfilled_steps = 0
            self.regret *= 0.9
        else:
            self.unfilled_steps += 1
            self.regret += 0.05 * spread / 10.0
        # Keep regret bounded to avoid runaway sizing.
        self.regret = min(2.0, self.regret)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.05 or abs(self.kf.v) > 1.2 or self.abs_move > 0.75:
            self.cool = 5
            return actions

        if spread < 4 or self.streak < 3:
            return actions
        if self.tox > 0.65 or self.abs_move > 0.45:
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        center = int(round(fair - 0.22 * move - 0.42 * self.flow - 0.04 * inventory))
        tight_offset = int(round(1 + min(1.0, self.regret * 0.5)))
        wide_offset = int(round(3 + max(0.0, self.unfilled_steps * 0.2)))

        for offset in (tight_offset, wide_offset):
            buy_tick = _clip_tick(int(center - offset))
            sell_tick = _clip_tick(int(center + offset))
            if buy_tick >= sell_tick:
                continue

            size = 4.2 + self.regret * 0.8
            if abs(inventory) > 25:
                size *= math.exp(-abs(inventory) / 20.0)

            buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
            sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))

            buy_qty = self._buy_qty(buy_px, size * 0.6, free_cash * 0.3)
            if buy_qty >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, buy_qty))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * buy_qty)

            sell_qty = self._sell_qty(sell_px, size * 0.6, free_cash * 0.3, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sell_qty))

        return actions
