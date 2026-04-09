from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Partial-information MM with hidden regime posterior."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.prev_spread = 2.0

        # Regime posterior: [calm, toxic]
        self.p_calm = 0.7
        self.p_toxic = 0.3

        self.flow_ewma = 0.0
        self.abs_move = 0.0
        self.tox = 0.0
        self.cool = 0

    def _safe_buy_qty(self, tick: int, qty: float, cash: float) -> float:
        return _q(min(qty, cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, qty: float, cash: float, yes_inv: float) -> float:
        px = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered = cash / max(0.01, 1.0 - px)
        return _q(min(qty, covered + uncovered))

    def _update_posterior(self, abs_move: float, fills: float, spread: int, spread_jump: float) -> None:
        # Simple Bayesian filter with handcrafted likelihood ratios.
        # toxic regime more likely under large moves, wider spread, and fill bursts.
        l_calm = 1.0
        l_toxic = 1.0
        if abs_move > 0.50:
            l_toxic *= 1.35
            l_calm *= 0.8
        if fills > 2.0:
            l_toxic *= 1.15
            l_calm *= 0.9
        if spread >= 5:
            l_toxic *= 0.93  # spread-wide is often safer for stale risk
            l_calm *= 1.05
        if spread_jump > 1.2:
            l_toxic *= 1.25
            l_calm *= 0.85

        # transition model
        p_cc = 0.95
        p_tt = 0.92
        pred_calm = self.p_calm * p_cc + self.p_toxic * (1.0 - p_tt)
        pred_toxic = self.p_toxic * p_tt + self.p_calm * (1.0 - p_cc)

        post_calm = pred_calm * l_calm
        post_toxic = pred_toxic * l_toxic
        z = post_calm + post_toxic
        if z > 1e-9:
            self.p_calm = post_calm / z
            self.p_toxic = post_toxic / z

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
        self.flow_ewma = 0.87 * self.flow_ewma + 0.13 * fill_imb
        self.abs_move = 0.91 * self.abs_move + 0.09 * abs(move)
        self.tox = 0.90 * self.tox + 0.10 * (abs(move) * fills)

        spread_jump = abs(spread - self.prev_spread)
        self.prev_spread = spread
        self._update_posterior(abs(move), fills, spread, spread_jump)

        actions: list[object] = [CancelAll()]
        if self.cool > 0:
            self.cool -= 1
            return actions

        if self.tox > 1.1 or self.abs_move > 0.9:
            self.cool = 4
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        # Internalize when calm probability is high; externalize/flatten when toxic.
        if self.p_toxic > 0.58:
            # only inventory-reducing passive quotes farther out.
            center = int(round(mid - 0.05 * inv))
            buy_px = _clip_tick(center - 3)
            sell_px = _clip_tick(center + 3)
            size = 1.8
            if inv < -5:
                q = self._safe_buy_qty(buy_px, size, free_cash * 0.18)
                if q >= 0.01 and buy_px < ask:
                    actions.append(PlaceOrder(Side.BUY, buy_px, q))
            if inv > 5:
                q = self._safe_sell_qty(sell_px, size, free_cash * 0.18, state.yes_inventory)
                if q >= 0.01 and bid < sell_px:
                    actions.append(PlaceOrder(Side.SELL, sell_px, q))
            return actions

        # Calm regime: trade more, prefer wide spread windows.
        if spread < 4:
            return actions

        center = int(round(mid - 0.18 * move - 0.42 * self.flow_ewma - 0.03 * inv))
        buy_px = _clip_tick(min(ask - 1, max(bid, center - 2)))
        sell_px = _clip_tick(max(bid + 1, min(ask, center + 2)))
        if buy_px >= sell_px:
            return actions

        signal = 0.75 * move + 0.75 * self.flow_ewma
        quote_buy = inv < 220
        quote_sell = inv > -220
        if signal > 0.42:
            quote_sell = False
        elif signal < -0.42:
            quote_buy = False

        size = 4.6 + 1.4 * max(0.0, self.p_calm - 0.5)
        if abs(inv) > 80:
            size = min(size, 2.2)

        budget = free_cash * 0.34
        if quote_buy:
            q = self._safe_buy_qty(buy_px, size, budget)
            if q >= 0.01 and buy_px < ask:
                actions.append(PlaceOrder(Side.BUY, buy_px, q))
                free_cash = max(0.0, free_cash - (buy_px / 100.0) * q)
                budget = free_cash * 0.34
        if quote_sell:
            q = self._safe_sell_qty(sell_px, size, budget, state.yes_inventory)
            if q >= 0.01 and bid < sell_px:
                actions.append(PlaceOrder(Side.SELL, sell_px, q))

        return actions
