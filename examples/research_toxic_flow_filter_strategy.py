from __future__ import annotations

import math

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Research-derived toxicity filter + selective internalization.

    Design inspirations from literature/practitioner patterns:
    - Toxic-flow filtering under partial information.
    - Internalize in low-toxicity windows; externalize/defend when toxicity rises.
    - Strongly state-dependent quote discounts.
    """

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.fast_mid = 50.0
        self.slow_mid = 50.0
        self.abs_move = 0.0
        self.flow_imb = 0.0
        self.tox = 0.0
        self.streak = 0
        self.cool = 0
        self.last_signed_fill = 0.0
        self.regime_prob = 0.5

    def _buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inventory)
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
        self.fast_mid = 0.80 * self.fast_mid + 0.20 * mid
        self.slow_mid = 0.96 * self.slow_mid + 0.04 * mid
        trend = self.fast_mid - self.slow_mid

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        fill_sum = buy_fill + sell_fill
        fill_imb = buy_fill - sell_fill
        self.flow_imb = 0.86 * self.flow_imb + 0.14 * fill_imb
        self.abs_move = 0.91 * self.abs_move + 0.09 * abs(move)

        # Toxicity proxy: adverse move against signed fill flow + movement*fills.
        signed_fill = fill_imb
        self.last_signed_fill = 0.75 * self.last_signed_fill + 0.25 * signed_fill
        adverse = 0.0
        if buy_fill > 0 and move < 0:
            adverse += abs(move) * buy_fill
        if sell_fill > 0 and move > 0:
            adverse += abs(move) * sell_fill
        self.tox = 0.90 * self.tox + 0.10 * (0.65 * adverse + 0.35 * abs(move) * fill_sum)

        # Partially observable regime score in [0,1]:
        # high means "safe/internalize", low means "toxic/defensive".
        safe_signal = 0.0
        safe_signal += 0.35 * max(0.0, 0.55 - self.tox)
        safe_signal += 0.25 * max(0.0, spread - 3.0) / 4.0
        safe_signal += 0.20 * max(0.0, 0.45 - self.abs_move)
        safe_signal += 0.20 * max(0.0, 0.4 - abs(self.last_signed_fill))
        safe_signal = max(0.0, min(1.0, safe_signal))
        self.regime_prob = 0.88 * self.regime_prob + 0.12 * safe_signal

        if spread >= 4:
            self.streak = min(30, self.streak + 1)
        else:
            self.streak = max(0, self.streak - 2)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        # Hard defensive gate.
        if self.tox > 1.20 or self.abs_move > 0.95:
            self.cool = 6
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # State-dependent quote discounting.
        center = int(round(mid - 0.16 * move - 0.46 * self.flow_imb - 0.035 * inv))
        base_width = 2 if spread >= 4 else 3
        if self.regime_prob < 0.40:
            base_width += 1
        if abs(inv) > 90:
            base_width += 1
        buy_tick = _clip_tick(center - base_width)
        sell_tick = _clip_tick(center + base_width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        # Internalize (quote both sides at/inside touch) only in safe regime.
        # Otherwise quote one side to reduce inventory risk.
        internalize = self.regime_prob > 0.52 and self.streak >= 2 and spread >= 4 and self.tox < 0.58
        quote_buy = inv < 240
        quote_sell = inv > -240

        if not internalize:
            if inv > 0:
                quote_buy = False
            elif inv < 0:
                quote_sell = False
            else:
                # If flat in toxic regime, keep tiny two-sided probing.
                quote_buy = True
                quote_sell = True

        signal = 0.55 * trend + 0.45 * self.flow_imb
        if internalize:
            # Mild directional skew while still providing both sides.
            if signal > 0.25 and quote_buy:
                quote_sell = inv > 0
            elif signal < -0.25 and quote_sell:
                quote_buy = inv < 0

        # Size policy.
        size = 3.6 + 1.8 * max(0.0, self.regime_prob - 0.4)
        if spread >= 5:
            size += 1.0
        if abs(inv) > 120:
            size = min(size, 1.8)

        # Quote placement:
        # - safe regime: touch + optional secondary level
        # - toxic regime: one tick further out
        if internalize:
            buy_px = _clip_tick(min(ask - 1, max(bid, buy_tick)))
            sell_px = _clip_tick(max(bid + 1, min(ask, sell_tick)))
        else:
            buy_px = _clip_tick(max(1, min(ask - 1, buy_tick - 1)))
            sell_px = _clip_tick(min(99, max(bid + 1, sell_tick + 1)))

        if buy_px >= sell_px:
            return actions

        budget = free_cash * (0.30 if internalize else 0.18)
        if quote_buy:
            bq = self._buy_qty(buy_px, size, budget)
            if bq >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * (0.30 if internalize else 0.18)
                if internalize and spread >= 6:
                    b2 = _clip_tick(max(1, buy_px - 2))
                    bq2 = self._buy_qty(b2, size * 0.45, budget)
                    if bq2 >= 0.01 and b2 < ask:
                        actions.append(PlaceOrder(Side.BUY, b2, bq2))

        if quote_sell:
            sq = self._sell_qty(sell_px, size, budget, state.yes_inventory)
            if sq >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))
                if internalize and spread >= 6:
                    s2 = _clip_tick(min(99, sell_px + 2))
                    sq2 = self._sell_qty(s2, size * 0.45, budget, state.yes_inventory)
                    if sq2 >= 0.01 and bid < s2:
                        actions.append(PlaceOrder(Side.SELL, s2, sq2))

        return actions
