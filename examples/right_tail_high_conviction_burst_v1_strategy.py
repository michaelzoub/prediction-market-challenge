from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """High-conviction burst strategy to hunt fat right tail."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.flow = 0.0
        self.tox = 0.0
        self.stable = 0
        self.burst = 0
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

        fill_sum = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow = 0.82 * self.flow + 0.18 * fill_imb
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fill_sum)

        if abs(move) <= 0.8:
            self.stable += 1
        else:
            self.stable = 0

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions
        if self.tox > 1.1:
            self.cooldown = 8
            self.burst = 0
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        signal = 0.6 * move + 0.4 * self.flow

        # Trigger burst only in wide+stable market.
        if spread >= 4 and self.stable >= 4 and abs(signal) > 0.3 and self.tox < 0.35:
            self.burst = max(self.burst, 12)

        if self.burst > 0:
            self.burst -= 1
            size = 8.0 if abs(signal) > 0.55 else 5.0
            if abs(inv) > 70:
                size = min(size, 2.0)
            if signal > 0 and inv < 100:
                px = _clip_tick(bid + 1)
                q = self._safe_buy(px, size, free_cash * 0.24)
                if q >= 0.01 and px < ask:
                    actions.append(PlaceOrder(Side.BUY, px, q))
                return actions
            if signal < 0 and inv > -100:
                px = _clip_tick(ask - 1)
                q = self._safe_sell(px, size, free_cash * 0.24, state.yes_inventory)
                if q >= 0.01 and px > bid:
                    actions.append(PlaceOrder(Side.SELL, px, q))
                return actions
            return actions

        # Background light market making.
        if spread >= 4 and self.tox < 0.25:
            center = int(round(mid - 0.02 * inv))
            buy_tick = _clip_tick(center - 1)
            sell_tick = _clip_tick(center + 1)
            if buy_tick < sell_tick:
                if inv < 130:
                    q_buy = self._safe_buy(buy_tick, 2.0, free_cash * 0.12)
                    if q_buy >= 0.01:
                        actions.append(PlaceOrder(Side.BUY, buy_tick, q_buy))
                        free_cash = max(0.0, free_cash - (buy_tick / 100.0) * q_buy)
                if inv > -130:
                    q_sell = self._safe_sell(sell_tick, 2.0, free_cash * 0.12, state.yes_inventory)
                    if q_sell >= 0.01:
                        actions.append(PlaceOrder(Side.SELL, sell_tick, q_sell))
        return actions
