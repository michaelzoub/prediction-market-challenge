from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Hybrid strategy that explicitly accounts for all 3 counterparties.

    Actors:
    - **Arb** sweeps any stale quote vs true p_t before retail arrives.
    - **Retail** provides positive edge when we provide liquidity at fair-ish prices.
    - **Competitor** is a static ladder around the *initial* probability; it doesn't react to jumps.

    Design:
    - Run a jump/stability filter using only observable signals (competitor best bid/ask).
    - Use a strict TTL (cancel+replace every step we participate) to reduce arb exposure.
    - Use two quoting tiers:
      - ULTRA-STABLE: step inside competitor by 1 tick with larger size (retail capture).
      - STABLE: join competitor best (or 1 tick back) with smaller size (presence without being arb's first target).
    - After detecting a jump-like shock, cancel and sit out for a cooldown; then re-enter in STABLE tier only.
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        # EWMA of absolute midpoint changes (ticks) as stability proxy
        self._prev_mid: int | None = None
        self._vol_ewma = 0.0
        self.vol_alpha = 0.12

        # Jump proxy via discontinuities in competitor best quotes
        self._prev_comp_mid: int | None = None
        self._prev_comp_spread: int | None = None
        self.jump_mid_move_ticks = 4
        self.jump_spread_widen_ticks = 4
        self.jump_cooldown_base = 4
        self.jump_cooldown_max = 14
        self._cooldown = 0
        self._recovery = 0

        # Engagement thresholds
        self.stable_spread_max = 5
        self.stable_vol_max = 0.9
        self.stable_mid_dist_max = 18
        self.stable_time_frac_min = 0.10

        self.ultra_spread_max = 2
        self.ultra_vol_max = 0.30
        self.ultra_mid_dist_max = 9
        self.ultra_time_frac_min = 0.30

        # Sizes
        self.stable_size = 3.2
        self.ultra_size = 12.0

        # Price aggressiveness
        self.step_inside = 1
        self.join_backoff_ticks = 0  # set to 1 to quote behind competitor

        # Risk / inventory
        self.inv_cap = 160.0
        self.net_hard = 85.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_vol(self, mid: int) -> None:
        dm = 0.0 if self._prev_mid is None else abs(mid - self._prev_mid)
        self._prev_mid = mid
        self._vol_ewma = (1.0 - self.vol_alpha) * self._vol_ewma + self.vol_alpha * float(dm)

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None:
            return [CancelAll()]

        spread = ask - bid
        if spread <= 0:
            return [CancelAll()]

        mid = (bid + ask) // 2
        self._update_vol(mid)
        mid_dist = abs(mid - 50)
        frac = self._time_fraction_remaining(state)

        # Jump detection based on competitor quote gaps/spread widening.
        if self._prev_comp_mid is not None:
            mid_move = abs(mid - self._prev_comp_mid)
        else:
            mid_move = 0
        if self._prev_comp_spread is not None:
            spread_move = spread - self._prev_comp_spread
        else:
            spread_move = 0
        self._prev_comp_mid = mid
        self._prev_comp_spread = spread

        if mid_move >= self.jump_mid_move_ticks or spread_move >= self.jump_spread_widen_ticks:
            early_bonus = int(round(4.0 * frac))
            gap_bonus = int(round(min(8.0, 0.9 * mid_move + 0.5 * max(0, spread_move))))
            cd = min(self.jump_cooldown_max, self.jump_cooldown_base + early_bonus + gap_bonus)
            self._cooldown = max(self._cooldown, cd)
            self._recovery = max(self._recovery, cd)

        if self._cooldown > 0:
            self._cooldown -= 1
            return [CancelAll()]

        stable = (
            spread <= self.stable_spread_max
            and self._vol_ewma <= self.stable_vol_max
            and mid_dist <= self.stable_mid_dist_max
            and frac >= self.stable_time_frac_min
        )
        if not stable:
            return [CancelAll()]

        ultra = (
            spread <= self.ultra_spread_max
            and self._vol_ewma <= self.ultra_vol_max
            and mid_dist <= self.ultra_mid_dist_max
            and frac >= self.ultra_time_frac_min
        )

        # Inventory gates + one-sided at extremes
        net = state.yes_inventory - state.no_inventory
        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        # TTL=1: always cancel/replace each step we participate.
        actions: list[object] = [CancelAll()]

        # Post-jump recovery: never step inside; join competitor best with small size.
        if self._recovery > 0:
            self._recovery -= 1
            size = self.stable_size
            bid_tick = bid - self.join_backoff_ticks
            ask_tick = ask + self.join_backoff_ticks
        elif ultra:
            size = self.ultra_size
            # step inside only if spread allows it
            bid_tick = bid + self.step_inside if spread >= 3 else bid
            ask_tick = ask - self.step_inside if spread >= 3 else ask
        else:
            size = self.stable_size
            bid_tick = bid - self.join_backoff_ticks
            ask_tick = ask + self.join_backoff_ticks

        if bid_tick >= ask_tick:
            return [CancelAll()]

        # mild size reduction away from 50
        size *= 1.0 - 0.35 * min(1.0, mid_dist / 25.0)
        size = max(0.6, round(size, 4))

        if can_bid:
            actions.append(PlaceOrder(Side.BUY, max(1, bid_tick), size))
        if can_ask:
            actions.append(PlaceOrder(Side.SELL, min(99, ask_tick), size))
        return actions

