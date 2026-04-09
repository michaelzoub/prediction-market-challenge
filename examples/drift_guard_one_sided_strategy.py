from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Directional one-sided quoting with strict toxicity gates.

    The strategy only improves touch when short-horizon directional confidence
    exceeds a threshold and recent adverse-selection proxies are quiet.
    """

    def __init__(self) -> None:
        self._prev_mid = 50.0
        self._fast_ret = 0.0
        self._slow_ret = 0.0
        self._abs_ret = 0.0
        self._tox = 0.0
        self._cooldown = 0
        self._anchor_mid = 50.0
        self._anchor_err = 0.0

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
        ret = mid - self._prev_mid
        self._prev_mid = mid

        self._fast_ret = 0.78 * self._fast_ret + 0.22 * ret
        self._slow_ret = 0.97 * self._slow_ret + 0.03 * ret
        self._abs_ret = 0.92 * self._abs_ret + 0.08 * abs(ret)
        self._anchor_mid = 0.995 * self._anchor_mid + 0.005 * mid
        self._anchor_err = 0.97 * self._anchor_err + 0.03 * abs(mid - self._anchor_mid)

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        adverse = 0.0
        if buy_fill > 0 and ret < 0:
            adverse += abs(ret) * buy_fill
        if sell_fill > 0 and ret > 0:
            adverse += abs(ret) * sell_fill
        self._tox = 0.88 * self._tox + 0.12 * (0.8 * adverse + 0.2 * abs(ret))

        inv = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)
        actions: list[object] = [CancelAll()]

        if self._cooldown > 0:
            self._cooldown -= 1
            return actions

        # Shock gate.
        jump_like = max(0.0, abs(ret) - (self._abs_ret + 0.18))
        if jump_like > 0.45:
            self._cooldown = 6
            return actions

        # Directional confidence from fast-vs-slow return signal.
        conf = self._fast_ret - self._slow_ret
        risk_on = self._tox < 0.16 and self._abs_ret < 0.9 and spread >= 2
        if not risk_on:
            return actions

        # Adaptive threshold: stricter when inventory is already directional.
        threshold = 0.16 + min(0.22, abs(inv) / 700.0)
        side: Side | None = None
        # Extra guard: avoid trading when touch likely still mean-reverting to anchor.
        dist_from_anchor = mid - self._anchor_mid
        anchor_band = max(0.8, 1.4 * self._anchor_err)
        if conf > threshold and inv < 95 and not (dist_from_anchor > anchor_band and ret <= 0):
            side = Side.BUY
        elif conf < -threshold and inv > -95 and not (dist_from_anchor < -anchor_band and ret >= 0):
            side = Side.SELL
        else:
            return actions

        # Improve competitor by one tick when possible to win queue priority.
        if side is Side.BUY:
            px = _clip_tick(min(ask - 1, bid + 1)) if spread >= 2 else bid
            if px >= ask:
                return actions
            size = 2.6 if abs(conf) < 0.28 else 4.2
            if abs(inv) > 70:
                size = min(size, 2.0)
            qty = self._safe_buy_qty(px, size, free_cash * 0.22)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=px, quantity=qty))
            return actions

        px = _clip_tick(max(bid + 1, ask - 1)) if spread >= 2 else ask
        if px <= bid:
            return actions
        size = 2.6 if abs(conf) < 0.28 else 4.2
        if abs(inv) > 70:
            size = min(size, 2.0)
        qty = self._safe_sell_qty(px, size, free_cash * 0.22, state.yes_inventory)
        if qty >= 0.01:
            actions.append(PlaceOrder(side=Side.SELL, price_ticks=px, quantity=qty))
        return actions
