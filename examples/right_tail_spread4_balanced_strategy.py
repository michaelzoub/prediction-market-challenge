from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(x: float) -> float:
    return max(0.0, round(x, 2))


class Strategy(BaseStrategy):
    """Capture right-tail by trading inside wide competitor spreads."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox = 0.0
        self.retail_score = 0.0
        self.stable = 0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None
        self.cooldown = 0

    def _safe_buy(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
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
        dmid = mid - self.prev_mid
        self.prev_mid = mid

        if self.prev_bid is None or self.prev_ask is None:
            self.stable = 0
        else:
            moved = abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
            self.stable = self.stable + 1 if moved <= 1 else 0
        self.prev_bid, self.prev_ask = bid, ask

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        fills = buy_fill + sell_fill
        adverse = 0.0
        if buy_fill > 0 and dmid < 0:
            adverse += abs(dmid) * buy_fill
        if sell_fill > 0 and dmid > 0:
            adverse += abs(dmid) * sell_fill
        self.tox = 0.9 * self.tox + 0.1 * (0.8 * adverse + 0.2 * abs(dmid))

        # Retail-rich proxy: fills with limited adverse follow-through.
        good_fill = fills if adverse < 0.08 else 0.0
        bad_fill = fills if adverse > 0.30 else 0.0
        self.retail_score = max(0.0, min(10.0, 0.94 * self.retail_score + 0.08 * good_fill - 0.12 * bad_fill))

        actions: list[object] = [CancelAll()]
        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        if self.cooldown > 0:
            self.cooldown -= 1
            return actions
        if adverse > 0.8:
            self.cooldown = 6
            return actions

        if spread < 4 or self.stable < 2 or self.tox > 0.26:
            return actions

        # One tick inside competitor touch is the key right-tail trade.
        buy_px = _clip_tick(bid + 1)
        sell_px = _clip_tick(ask - 1)
        if buy_px >= sell_px:
            return actions

        size = 4.0 + min(8.0, 1.2 * self.retail_score)
        if abs(inv) > 80:
            size = min(size, 3.0)

        quote_buy = inv < 120
        quote_sell = inv > -120

        # Keep inventory mean-reverting.
        if inv > 30:
            size_sell = size * 1.35
            size_buy = size * 0.65
        elif inv < -30:
            size_buy = size * 1.35
            size_sell = size * 0.65
        else:
            size_buy = size
            size_sell = size

        if quote_buy:
            q_buy = self._safe_buy(buy_px, size_buy, free_cash * 0.25)
            if q_buy >= 0.01:
                actions.append(PlaceOrder(Side.BUY, buy_px, q_buy))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * q_buy)
        if quote_sell:
            q_sell = self._safe_sell(sell_px, size_sell, free_cash * 0.25, state.yes_inventory)
            if q_sell >= 0.01:
                actions.append(PlaceOrder(Side.SELL, sell_px, q_sell))

        return actions
