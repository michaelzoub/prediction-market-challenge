from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Wait for shock, then harvest spread with bounded risk."""

    def __init__(self) -> None:
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.prev_mid = 50.0
        self.vol = 0.0
        self.shock_age = 9999
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
        ret = mid - self.prev_mid
        self.prev_mid = mid
        self.vol = 0.9 * self.vol + 0.1 * abs(ret)

        moved = 0
        if self.prev_bid is not None and self.prev_ask is not None:
            moved = abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        self.prev_bid, self.prev_ask = bid, ask

        # Shock: large touch displacement.
        if moved >= 6 or abs(ret) >= 2.5:
            self.shock_age = 0
            self.cooldown = 3
        else:
            self.shock_age += 1

        actions: list[object] = [CancelAll()]
        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # Cooldown during immediate post-shock uncertainty.
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        # Harvest only shortly after shock, and only when spread is wide enough.
        if self.shock_age > 25 or spread < 3:
            return actions

        tox = abs(ret) * (state.buy_filled_quantity + state.sell_filled_quantity)
        if tox > 1.8:
            return actions

        center = int(round(mid - 0.02 * inv))
        width = 2 if spread >= 4 else 1
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            return actions

        size = 4.5 if spread >= 4 else 3.0
        if abs(inv) > 90:
            size = min(size, 2.0)

        if inv < 150:
            q_buy = self._safe_buy_qty(buy_tick, size, free_cash * 0.2)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
        if inv > -150:
            q_sell = self._safe_sell_qty(sell_tick, size, free_cash * 0.2, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))
        return actions
