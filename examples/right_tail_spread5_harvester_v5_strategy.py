from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side


def _clip(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Spread>=5 harvester v5 — VPIN-hybrid with adaptive gating and multi-level quotes."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.tox_fast = 0.0
        self.tox_slow = 0.0
        self.abs_move_fast = 0.0
        self.abs_move_slow = 0.0
        self.flow = 0.0
        self.streak = 0
        self.cool = 0
        self.fill_dir_score = 0.0
        self.last_fill_side = 0
        self.total_buy_filled = 0.0
        self.total_sell_filled = 0.0
        self.recent_arb_proxy = 0.0

    def _buy_qty(self, tick: int, target: float, cash: float) -> float:
        return _q(min(target, cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, target: float, cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - px)
        return _q(min(target, covered + uncovered))

    def on_step(self, state):
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

        self.abs_move_fast = 0.88 * self.abs_move_fast + 0.12 * abs(move)
        self.abs_move_slow = 0.96 * self.abs_move_slow + 0.04 * abs(move)
        self.flow = 0.86 * self.flow + 0.14 * fill_imb
        self.tox_fast = 0.88 * self.tox_fast + 0.12 * (abs(move) * fill_sum)
        self.tox_slow = 0.96 * self.tox_slow + 0.04 * (abs(move) * fill_sum)

        if fill_sum > 0:
            adverse = fill_imb * move
            self.recent_arb_proxy = 0.85 * self.recent_arb_proxy + 0.15 * (1.0 if adverse > 0 else -0.5 if adverse < 0 else 0.0)

        self.streak = self.streak + 1 if spread >= 5 else 0
        self.total_buy_filled += state.buy_filled_quantity
        self.total_sell_filled += state.sell_filled_quantity

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        tox = max(self.tox_fast, self.tox_slow * 1.8)
        vol = max(self.abs_move_fast, self.abs_move_slow * 1.5)

        if tox > 1.05 or vol > 0.72:
            self.cool = 5
            return actions

        if spread < 5 or self.streak < 2:
            return actions

        if tox > 0.50 or vol > 0.48:
            return actions

        if self.recent_arb_proxy > 0.6:
            self.cool = 3
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        flow_adj = self.flow
        inv_adj = 0.03 * inv
        move_adj = 0.22 * move

        center = int(round(mid - move_adj - 0.38 * flow_adj - inv_adj))
        buy_tick = _clip(center - 2)
        sell_tick = _clip(center + 2)
        if buy_tick >= sell_tick:
            buy_tick = _clip(sell_tick - 1)
        if buy_tick >= sell_tick:
            return actions

        buy_px = _clip(min(ask - 1, max(bid, buy_tick)))
        sell_px = _clip(max(bid + 1, min(ask, sell_tick)))

        base_size = 7.5
        if abs(inv) > 60:
            base_size = max(2.0, base_size * (1.0 - (abs(inv) - 60) / 200.0))
        if abs(inv) > 160:
            base_size = 1.0

        if tox > 0.25:
            base_size *= 0.7
        if vol > 0.30:
            base_size *= 0.8

        quote_buy = inv < 180
        quote_sell = inv > -180
        if tox > 0.30:
            if inv > 20:
                quote_buy = False
            if inv < -20:
                quote_sell = False
            if abs(inv) <= 20:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.36
        if quote_buy and buy_px < ask:
            bq = self._buy_qty(buy_px, base_size, budget)
            if bq >= 0.01:
                actions.append(PlaceOrder(Side.BUY, buy_px, bq))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * bq)
                budget = free_cash * 0.36

        if quote_sell and sell_px > bid:
            sq = self._sell_qty(sell_px, base_size, budget, state.yes_inventory)
            if sq >= 0.01:
                actions.append(PlaceOrder(Side.SELL, sell_px, sq))
                free_cash = max(0.0, free_cash - (1.0 - sell_px / 100.0) * sq)

        if spread >= 7 and tox < 0.20 and vol < 0.25:
            l2_size = base_size * 0.4
            l2_buy = _clip(buy_px - 1)
            l2_sell = _clip(sell_px + 1)
            budget2 = free_cash * 0.20
            if quote_buy and l2_buy < ask and l2_buy >= 1:
                bq2 = self._buy_qty(l2_buy, l2_size, budget2)
                if bq2 >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, l2_buy, bq2))
                    free_cash = max(0.0, free_cash - (l2_buy / 100.0) * bq2)
                    budget2 = free_cash * 0.20
            if quote_sell and l2_sell > bid and l2_sell <= 99:
                sq2 = self._sell_qty(l2_sell, l2_size, budget2, state.yes_inventory)
                if sq2 >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, l2_sell, sq2))

        return actions
