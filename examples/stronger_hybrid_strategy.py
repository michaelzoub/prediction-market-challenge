from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Hybrid market maker that trades only in favorable micro-regimes."""

    def __init__(self) -> None:
        self._total_steps: int | None = None

        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._prev_spread: int | None = None
        self._prev_mid = 50.0

        self._fast_mid = 50.0
        self._slow_mid = 50.0
        self._flow = 0.0
        self._abs_move = 0.0
        self._tox = 0.0
        self._adverse_run = 0

        self._wide_streak = 0
        self._stable_streak = 0
        self._cooldown = 0
        self._recovery = 0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _buy_qty(self, tick: int, qty: float, free_cash: float) -> float:
        return _q(min(qty, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, qty: float, free_cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = free_cash / max(0.01, 1.0 - price)
        return _q(min(qty, covered + uncovered))

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        move = mid - self._prev_mid
        self._prev_mid = mid

        touch_move = (
            0
            if self._prev_bid is None or self._prev_ask is None
            else abs(bid - self._prev_bid) + abs(ask - self._prev_ask)
        )
        spread_jump = 0 if self._prev_spread is None else spread - self._prev_spread
        self._prev_bid = bid
        self._prev_ask = ask
        self._prev_spread = spread

        self._wide_streak = self._wide_streak + 1 if spread >= 5 else 0
        self._stable_streak = self._stable_streak + 1 if touch_move <= 1 else 0

        self._fast_mid = 0.72 * self._fast_mid + 0.28 * mid
        self._slow_mid = 0.965 * self._slow_mid + 0.035 * mid

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        fills = buy_fill + sell_fill
        fill_imbalance = buy_fill - sell_fill
        adverse = 0.0
        if buy_fill > 0.0 and move < 0.0:
            adverse += abs(move) * buy_fill
        if sell_fill > 0.0 and move > 0.0:
            adverse += abs(move) * sell_fill

        if adverse > 0.35:
            self._adverse_run = min(8, self._adverse_run + 2)
        else:
            self._adverse_run = max(0, self._adverse_run - 1)

        self._flow = 0.89 * self._flow + 0.11 * fill_imbalance
        self._abs_move = 0.92 * self._abs_move + 0.08 * abs(move)
        self._tox = 0.88 * self._tox + 0.12 * (0.75 * abs(move) * fills + 0.90 * adverse)

        actions: list[object] = [CancelAll()]
        frac = self._time_fraction_remaining(state)

        shock = (
            touch_move >= 4
            or spread_jump >= 3
            or abs(move) > max(1.0, self._abs_move + 0.75)
        )
        if shock:
            base_cool = 3 + int(round(3.5 * frac))
            gap_penalty = min(6, touch_move // 2 + max(0, spread_jump))
            self._cooldown = max(self._cooldown, base_cool + gap_penalty)
            self._recovery = max(self._recovery, min(10, base_cool + gap_penalty))

        if self._cooldown > 0:
            self._cooldown -= 1
            return actions

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        trend = self._fast_mid - self._slow_mid
        signal = 0.62 * self._flow + 0.34 * trend + 0.18 * move
        inv_skew = -0.048 * inv
        center = mid + 0.28 * trend - 0.68 * self._flow - 0.08 * move + inv_skew

        danger = self._tox > 0.60 or self._adverse_run >= 5
        flatten = abs(inv) >= 115 or danger

        if flatten:
            flatten_size = 1.1 if self._tox > 0.40 else 1.5
            if inv > 0:
                sell_tick = ask if self._tox > 0.45 else max(bid + 1, ask - 1)
                qty = self._sell_qty(sell_tick, flatten_size, free_cash * 0.28, state.yes_inventory)
                if qty >= 0.01:
                    actions.append(PlaceOrder(Side.SELL, sell_tick, qty))
            elif inv < 0:
                buy_tick = bid if self._tox > 0.45 else min(ask - 1, bid + 1)
                qty = self._buy_qty(buy_tick, flatten_size, free_cash * 0.28)
                if qty >= 0.01:
                    actions.append(PlaceOrder(Side.BUY, buy_tick, qty))
            return actions

        if self._wide_streak < 3 or self._stable_streak < 2 or self._tox > 0.36 or self._abs_move > 0.56:
            return actions

        base_size = 3.4
        if self._wide_streak >= 6:
            base_size += 0.6
        if self._stable_streak >= 5:
            base_size += 0.4
        if frac > 0.65:
            base_size *= 0.85
        if abs(inv) > 80:
            base_size = min(base_size, 1.8)
        elif self._tox > 0.24:
            base_size *= 0.82

        width = 2
        if self._wide_streak >= 6 and self._stable_streak >= 4 and self._tox < 0.18:
            width = 1

        buy_tick = _clip_tick(round(center - width))
        sell_tick = _clip_tick(round(center + width))
        buy_tick = max(bid, min(ask - 1, buy_tick))
        sell_tick = min(ask, max(bid + 1, sell_tick))

        if self._stable_streak >= 4 and self._wide_streak >= 4:
            buy_tick = max(buy_tick, min(ask - 1, bid + 1))
            sell_tick = min(sell_tick, max(bid + 1, ask - 1))

        if self._recovery > 0:
            self._recovery -= 1
            buy_tick = min(buy_tick, bid)
            sell_tick = max(sell_tick, ask)
            base_size = min(base_size, 1.7)

        if buy_tick >= sell_tick:
            return actions

        quote_buy = inv < 235
        quote_sell = inv > -235
        if signal > 0.34:
            quote_sell = False
        elif signal < -0.34:
            quote_buy = False

        if self._tox > 0.28:
            if inv > 0:
                quote_buy = False
            elif inv < 0:
                quote_sell = False

        budget = free_cash * 0.52
        if quote_buy:
            buy_qty = self._buy_qty(buy_tick, base_size, budget)
            if buy_qty >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, buy_qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * buy_qty)

        if quote_sell:
            sell_qty = self._sell_qty(sell_tick, base_size, free_cash * 0.52, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, sell_qty))

        return actions
