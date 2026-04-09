from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, tick))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Arm:
    def __init__(self, width: int, size: float, inv_coeff: float, tox_coeff: float, jump_coeff: float) -> None:
        self.width = width
        self.size = size
        self.inv_coeff = inv_coeff
        self.tox_coeff = tox_coeff
        self.jump_coeff = jump_coeff


class Strategy(BaseStrategy):
    """Guarded ensemble over inventory/toxicity/jump archetypes."""

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.ewma_abs_move = 0.0
        self.ewma_tox = 0.0
        self.flow_imb_ewma = 0.0
        self.jump_score = 0.0
        self.t = 0

        self.arms = [
            Arm(width=2, size=4.2, inv_coeff=0.03, tox_coeff=0.35, jump_coeff=0.35),  # retail capture
            Arm(width=3, size=3.4, inv_coeff=0.05, tox_coeff=0.60, jump_coeff=0.70),  # balanced
            Arm(width=5, size=2.2, inv_coeff=0.08, tox_coeff=1.00, jump_coeff=1.00),  # defensive
        ]
        self.arm_counts = [0, 0, 0]
        self.arm_means = [0.0, 0.0, 0.0]
        self.last_arm = 1
        self.last_inv = 0.0

    def _safe_buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _safe_sell_qty(self, tick: int, target: float, free_cash: float, yes_inv: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inv)
        uncovered_cap = free_cash / max(0.01, 1.0 - price)
        return _q(min(target, covered + uncovered_cap))

    def _update_reward_proxy(self, state: StepState, move: float) -> None:
        fills = state.buy_filled_quantity + state.sell_filled_quantity
        tox_proxy = abs(move) * fills
        inv_now = state.yes_inventory - state.no_inventory
        inv_pen = max(0.0, abs(inv_now) - abs(self.last_inv))
        arm = self.arms[self.last_arm]
        reward = 0.012 * fills * arm.width - 0.03 * tox_proxy - arm.inv_coeff * inv_pen
        idx = self.last_arm
        self.arm_counts[idx] += 1
        n = self.arm_counts[idx]
        self.arm_means[idx] += (reward - self.arm_means[idx]) / n
        self.last_inv = inv_now

    def _select_arm(self, inv: float) -> int:
        for i, n in enumerate(self.arm_counts):
            if n == 0:
                return i

        best_i = 0
        best_score = -1e18
        for i, arm in enumerate(self.arms):
            mean = self.arm_means[i]
            bonus = 1.4 / (self.arm_counts[i] ** 0.5)
            penalty = arm.inv_coeff * abs(inv) + arm.tox_coeff * self.ewma_tox + arm.jump_coeff * self.jump_score
            score = mean + bonus - penalty
            if score > best_score:
                best_score = score
                best_i = i
        return best_i

    def on_step(self, state: StepState):
        self.t += 1
        bid = state.competitor_best_bid_ticks if state.competitor_best_bid_ticks is not None else 49
        ask = state.competitor_best_ask_ticks if state.competitor_best_ask_ticks is not None else 51
        if ask <= bid:
            ask = min(99, bid + 1)
        mid = 0.5 * (bid + ask)
        move = mid - self.prev_mid
        self.prev_mid = mid

        self._update_reward_proxy(state, move)

        self.ewma_abs_move = 0.93 * self.ewma_abs_move + 0.07 * abs(move)
        fills = state.buy_filled_quantity + state.sell_filled_quantity
        fill_imb = state.buy_filled_quantity - state.sell_filled_quantity
        self.flow_imb_ewma = 0.85 * self.flow_imb_ewma + 0.15 * fill_imb
        self.ewma_tox = 0.9 * self.ewma_tox + 0.1 * (abs(move) * fills)
        jump_impulse = max(0.0, abs(move) - (self.ewma_abs_move + 0.22))
        self.jump_score = 0.9 * self.jump_score + 0.1 * jump_impulse

        inv = state.yes_inventory - state.no_inventory
        arm_idx = self._select_arm(inv)
        arm = self.arms[arm_idx]
        self.last_arm = arm_idx

        flow_skew = 1 if self.flow_imb_ewma > 1.4 else (-1 if self.flow_imb_ewma < -1.4 else 0)
        trend_skew = 1 if move > 0.45 else (-1 if move < -0.45 else 0)
        inv_skew = int(round(-0.03 * inv))
        center = int(round(mid)) + flow_skew + trend_skew + inv_skew

        width = arm.width + (1 if self.jump_score > 0.45 else 0)
        buy_tick = _clip_tick(center - width)
        sell_tick = _clip_tick(center + width)
        if buy_tick >= sell_tick:
            buy_tick = _clip_tick(sell_tick - 1)

        size = arm.size
        if abs(inv) > 95:
            size = min(size, 1.8)

        quote_buy = inv < 150
        quote_sell = inv > -150
        if self.ewma_tox > 0.9:
            quote_buy = inv < 0
            quote_sell = inv > 0
            if inv == 0:
                quote_buy = False
                quote_sell = False

        actions: list[object] = [CancelAll()]
        free_cash = max(0.0, state.free_cash)

        if quote_buy:
            qty = self._safe_buy_qty(buy_tick, size, free_cash * 0.19)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.BUY, price_ticks=buy_tick, quantity=qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * qty)

        if quote_sell:
            qty = self._safe_sell_qty(sell_tick, size, free_cash * 0.19, state.yes_inventory)
            if qty >= 0.01:
                actions.append(PlaceOrder(side=Side.SELL, price_ticks=sell_tick, quantity=qty))

        return actions
