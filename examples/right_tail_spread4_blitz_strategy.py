from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Aggressive spread-4 retail blitz with toxicity kill switch."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.mode = "idle"
        self.mode_timer = 0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        p = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = free_cash / max(0.01, 1.0 - p)
        return _q(min(target, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        spread = ask - bid
        mid = 0.5 * (bid + ask)
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        adverse = 0.0
        if buy_fill > 0 and dmid < 0:
            adverse += abs(dmid) * buy_fill
        if sell_fill > 0 and dmid > 0:
            adverse += abs(dmid) * sell_fill
        self.tox = 0.9 * self.tox + 0.1 * (0.7 * adverse + 0.3 * abs(dmid))

        if self.mode_timer > 0:
            self.mode_timer -= 1
        if spread >= 4 and self.tox < 0.16:
            self.mode = "blitz"
            self.mode_timer = max(self.mode_timer, 8)
        elif self.mode_timer == 0:
            self.mode = "idle"

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        actions: list[object] = [CancelAll()]

        if self.mode != "blitz":
            # Minimal exposure outside target regime.
            if self.tox > 0.25:
                return actions
            px_bid = _clip_tick(bid - 1)
            px_ask = _clip_tick(ask + 1)
            if px_bid < ask and inv < 90:
                q_buy = self._safe_buy_qty(px_bid, 1.2, free_cash * 0.08)
                if q_buy >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, px_bid, q_buy))
                    free_cash -= (px_bid / 100.0) * q_buy
            if px_ask > bid and inv > -90:
                q_sell = self._safe_sell_qty(px_ask, 1.2, free_cash * 0.08, state.yes_inventory)
                if q_sell >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, px_ask, q_sell))
            return actions

        # Blitz mode: join touch with larger size; switch one-sided when toxicity rises.
        size = 8.0
        if self.tox > 0.22:
            size = 4.0
        if abs(inv) > 90:
            size = min(size, 3.0)

        quote_buy = inv < 145
        quote_sell = inv > -145
        if self.tox > 0.28:
            quote_buy = inv < 0
            quote_sell = inv > 0

        px_buy = _clip_tick(min(ask - 1, bid + 1)) if spread >= 2 else bid
        px_sell = _clip_tick(max(bid + 1, ask - 1)) if spread >= 2 else ask

        if quote_buy and px_buy < ask:
            q_buy = self._safe_buy_qty(px_buy, size, free_cash * 0.3)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(Side.BUY, px_buy, q_buy))
                free_cash -= (px_buy / 100.0) * q_buy

        if quote_sell and px_sell > bid:
            q_sell = self._safe_sell_qty(px_sell, size, free_cash * 0.3, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(Side.SELL, px_sell, q_sell))

        return actions
