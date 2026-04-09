from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelOrder, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Inventory-as-signal strategy, tuned to be more aggressive.

    Main knobs vs a conservative variant:
    - Quote closer to the competitor midpoint (smaller base width).
    - When conditions are stable, step inside the competitor by 1 tick to gain queue priority.
    - Use price skew (not just one-sided shutdown) to reduce inventory; still hard one-sided at extremes.
    - Reduce "sit out" time by loosening engage thresholds slightly.
    """

    def __init__(self):
        self._bid_id = "bid"
        self._ask_id = "ask"

        self.inv_cap = 180.0

        # Width + size schedules (more aggressive: tighter + larger)
        self.size_early = 6.5
        self.size_late = 1.2
        self.k_early = 2
        self.k_late = 12
        self.k_extreme_bonus = 14

        self.max_spread_to_quote_early = 12
        self.max_spread_to_quote_late = 5

        # Inventory signal thresholds
        self.net_soft = 35.0   # start leaning
        self.net_hard = 95.0   # fully one-sided (only reduce exposure)

        # Stronger skew so we keep quoting but bias fills
        self.skew_ticks_per_25_shares = 2
        self.max_skew_ticks = 10

        # Mild response to fills
        self.widen_on_fill = 1
        self.cooldown_steps_after_fill = 0

        # Stability proxy (EWMA on midpoint changes)
        self._prev_mid: int | None = None
        self._vol_ewma = 0.0
        self.vol_alpha = 0.10

        # Simple trend signal (EWMA of signed midpoint changes)
        self._trend_ewma = 0.0
        self.trend_alpha = 0.22
        self.trend_deadband = 0.12
        self.max_trend_skew_ticks = 5

        # "Tall bar" retail-capture mode:
        # Quote very aggressively ONLY when the book looks extremely stable,
        # aiming for many similar small positive edges across runs.
        self.sniper_spread_max = 2
        self.sniper_vol_max = 0.28
        self.sniper_mid_dist_max = 6
        self.sniper_trend_abs_max = 0.10
        self.sniper_time_frac_min = 0.35
        self.sniper_step_inside = 1
        self.sniper_size = 18.0

        # Jump handling: detect shocks (arb sweeps competitor ladder) and sit out briefly.
        # We only observe competitor best quotes, so we use sudden changes in midpoint/spread
        # as a proxy for Poisson jump events.
        self._prev_comp_mid: int | None = None
        self._prev_comp_spread: int | None = None
        self.jump_mid_move_ticks = 4
        self.jump_spread_widen_ticks = 4
        self.jump_vol_trigger = 1.6
        self.jump_cooldown_steps = 2
        self.jump_cooldown_max = 10
        self._jump_cooldown = 0

        # Post-jump recovery: quote safely by joining competitor best levels (no step-in).
        self.recovery_steps = 5
        self.recovery_max = 14
        self._recovery = 0
        self.recovery_size = 3.0

        # Engage filter: quote more often than a conservative stance
        self.engage_spread_max = 6
        self.engage_vol_max = 1.25
        self.engage_mid_dist_max = 16
        self.engage_time_frac_min = 0.12

        # Stable aggression: step in for priority, modestly boost size
        self.super_spread_max = 2
        self.super_vol_max = 0.55
        self.super_mid_dist_max = 11
        self.super_time_frac_min = 0.35
        self.super_size_boost = 1.75
        self.super_step_inside = 1

        # Ultra-stable: go for volume (aim for positive outliers)
        self.ultra_spread_max = 2
        self.ultra_vol_max = 0.35
        self.ultra_mid_dist_max = 9
        self.ultra_time_frac_min = 0.40
        self.ultra_size_boost = 2.8
        self.ultra_step_inside = 2

        # State
        self._cooldown = 0
        self._total_steps: int | None = None
        self._last_bid_tick: int | None = None
        self._last_ask_tick: int | None = None
        self._last_size: float | None = None

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_vol(self, mid: int) -> None:
        if self._prev_mid is None:
            dm = 0.0
            d = 0.0
        else:
            d = float(mid - self._prev_mid)
            dm = abs(d)
        self._prev_mid = mid
        self._vol_ewma = (1.0 - self.vol_alpha) * self._vol_ewma + self.vol_alpha * float(dm)
        self._trend_ewma = (1.0 - self.trend_alpha) * self._trend_ewma + self.trend_alpha * d

    def _inventory_skew_ticks(self, state: StepState) -> int:
        net = state.yes_inventory - state.no_inventory
        skew_units = int(net // 25.0)
        skew = skew_units * self.skew_ticks_per_25_shares
        if skew > 0:
            return min(self.max_skew_ticks, skew)
        if skew < 0:
            return max(-self.max_skew_ticks, skew)
        return 0

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None:
            return self._cancel_if_needed(state)

        frac = self._time_fraction_remaining(state)
        late = 1.0 - frac

        max_spread = int(round(self.max_spread_to_quote_early * frac + self.max_spread_to_quote_late * late))
        spread = ask - bid
        if spread > max_spread or spread <= 0:
            return self._cancel_if_needed(state)

        mid = (bid + ask) // 2
        self._update_vol(mid)

        # Detect jump-like shocks via competitor quote discontinuities.
        # After a true-probability jump, arb will sweep many competitor levels, causing
        # best bid/ask to "gap" several ticks from one step to the next.
        if self._prev_comp_mid is not None:
            mid_move = abs(mid - self._prev_comp_mid)
            mid_jump = mid_move >= self.jump_mid_move_ticks
        else:
            mid_move = 0
            mid_jump = False
        if self._prev_comp_spread is not None:
            spread_move = spread - self._prev_comp_spread
            spread_jump = spread_move >= self.jump_spread_widen_ticks
        else:
            spread_move = 0
            spread_jump = False
        vol_jump = self._vol_ewma >= self.jump_vol_trigger

        self._prev_comp_mid = mid
        self._prev_comp_spread = spread

        if mid_jump or spread_jump or vol_jump:
            # Adaptive cooldown/recovery:
            # - larger mid gaps imply larger latent shocks / arb sweeps
            # - earlier in the sim (larger H) implies more jump risk ahead, so we wait longer
            frac = self._time_fraction_remaining(state)
            early_bonus = int(round(4.0 * frac))
            gap_bonus = int(round(min(6.0, 0.8 * mid_move + 0.5 * max(0, spread_move))))
            cd = min(self.jump_cooldown_max, self.jump_cooldown_steps + early_bonus + gap_bonus)
            rec = min(self.recovery_max, self.recovery_steps + early_bonus + gap_bonus)
            self._jump_cooldown = max(self._jump_cooldown, cd)
            self._recovery = max(self._recovery, rec)

        if self._jump_cooldown > 0:
            self._jump_cooldown -= 1
            return self._cancel_if_needed(state)

        mid_dist = abs(mid - 50)
        engaged = (
            spread <= self.engage_spread_max
            and self._vol_ewma <= self.engage_vol_max
            and mid_dist <= self.engage_mid_dist_max
            and frac >= self.engage_time_frac_min
        )
        if not engaged:
            return self._cancel_if_needed(state)

        # Post-jump recovery phase: avoid stepping inside while the book re-stabilizes.
        if self._recovery > 0:
            self._recovery -= 1
            net = state.yes_inventory - state.no_inventory
            can_place_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
            can_place_ask = state.no_inventory < self.inv_cap
            if abs(net) >= self.net_hard:
                if net > 0:
                    can_place_bid = False
                else:
                    can_place_ask = False

            bid_tick = bid
            ask_tick = ask
            if bid_tick >= ask_tick:
                return self._cancel_if_needed(state)

            size = self.recovery_size * (1.0 - 0.45 * min(1.0, mid_dist / 20.0))
            size = max(0.6, round(size, 4))

            return self._sync_quotes(
                state,
                bid_tick=bid_tick,
                ask_tick=ask_tick,
                size=size,
                can_bid=can_place_bid,
                can_ask=can_place_ask,
            )

        # If the book is extremely stable, switch to a consistent retail-capture stance:
        # place inside the competitor by 1 tick with a fixed, larger size.
        if (
            spread <= self.sniper_spread_max
            and self._vol_ewma <= self.sniper_vol_max
            and mid_dist <= self.sniper_mid_dist_max
            and abs(self._trend_ewma) <= self.sniper_trend_abs_max
            and frac >= self.sniper_time_frac_min
        ):
            net = state.yes_inventory - state.no_inventory
            can_place_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
            can_place_ask = state.no_inventory < self.inv_cap
            if abs(net) >= self.net_hard:
                if net > 0:
                    can_place_bid = False
                else:
                    can_place_ask = False

            bid_tick = max(1, bid + self.sniper_step_inside)
            ask_tick = min(99, ask - self.sniper_step_inside)
            if bid_tick >= ask_tick:
                return self._cancel_if_needed(state)

            size = self.sniper_size
            # Basic safety scaling with distance from 50 (even within sniper regime)
            size *= 1.0 - 0.35 * min(1.0, mid_dist / 20.0)
            size = max(0.8, round(size, 4))

            return self._sync_quotes(
                state,
                bid_tick=bid_tick,
                ask_tick=ask_tick,
                size=size,
                can_bid=can_place_bid,
                can_ask=can_place_ask,
            )

        # Time-decayed base
        size = self.size_early * frac + self.size_late * late
        k_mid = self.k_early * frac + self.k_late * late
        extreme_scale = min(1.0, mid_dist / 35.0)
        k = int(round(k_mid + self.k_extreme_bonus * extreme_scale + min(5.0, self._vol_ewma)))
        k = max(1, min(40, k))

        # Reduce size when price is far from 50 or volatility is elevated
        size *= 1.0 - 0.55 * extreme_scale
        size *= 1.0 / (1.0 + 0.25 * max(0.0, self._vol_ewma - 0.7))

        super_stable = (
            spread <= self.super_spread_max
            and self._vol_ewma <= self.super_vol_max
            and mid_dist <= self.super_mid_dist_max
            and frac >= self.super_time_frac_min
        )
        ultra_stable = (
            spread <= self.ultra_spread_max
            and self._vol_ewma <= self.ultra_vol_max
            and mid_dist <= self.ultra_mid_dist_max
            and frac >= self.ultra_time_frac_min
        )
        if ultra_stable:
            size *= self.ultra_size_boost
        elif super_stable:
            size *= self.super_size_boost

        size = max(0.6, round(size, 4))

        if (state.buy_filled_quantity + state.sell_filled_quantity) > 0:
            k = min(40, k + self.widen_on_fill)
            self._cooldown = max(self._cooldown, self.cooldown_steps_after_fill)
        elif self._cooldown > 0:
            self._cooldown -= 1

        if self._cooldown > 0:
            return self._cancel_if_needed(state)

        net = state.yes_inventory - state.no_inventory

        can_place_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_place_ask = state.no_inventory < self.inv_cap

        # Hard one-sided at extremes (risk stop)
        if abs(net) >= self.net_hard:
            if net > 0:
                can_place_bid = False
            else:
                can_place_ask = False

        skew = self._inventory_skew_ticks(state)

        # Directional bias: if competitor mid is drifting, shift both quotes in that direction.
        # This intentionally creates "winner" runs in trending regimes (more aggressive PnL shape),
        # at the cost of some additional tail risk (capped via inventory limits + one-sided hard stop).
        trend = self._trend_ewma
        if abs(trend) < self.trend_deadband:
            trend_skew = 0
        else:
            trend_skew = int(round(max(-self.max_trend_skew_ticks, min(self.max_trend_skew_ticks, trend))))

        # Base quotes
        bid_tick = max(1, mid - k - max(0, skew) + trend_skew)
        ask_tick = min(99, mid + k - min(0, skew) + trend_skew)

        # Aggressive "step-in" during super-stable conditions:
        # improve each side by 1 tick, but never cross / lock.
        if ultra_stable:
            bid_tick = min(bid_tick + self.ultra_step_inside, ask - 1)
            ask_tick = max(ask_tick - self.ultra_step_inside, bid + 1)
        elif super_stable:
            bid_tick = min(bid_tick + self.super_step_inside, ask - 1)
            ask_tick = max(ask_tick - self.super_step_inside, bid + 1)

        # Additional lean when inventory is meaningfully offside:
        # keep quoting both sides when possible, but make the inventory-increasing side less attractive.
        if abs(net) >= self.net_soft and abs(net) < self.net_hard:
            lean = 2
            if net > 0:
                # too much YES => discourage buying YES (worse bid), encourage selling YES (better ask)
                bid_tick = max(1, bid_tick - lean)
                ask_tick = max(1, ask_tick - 1)
            else:
                # too much NO (i.e. net negative) => discourage selling YES (worse ask), encourage buying YES (better bid)
                ask_tick = min(99, ask_tick + lean)
                bid_tick = min(99, bid_tick + 1)

        if bid_tick >= ask_tick:
            return self._cancel_if_needed(state)

        return self._sync_quotes(
            state,
            bid_tick=bid_tick,
            ask_tick=ask_tick,
            size=size,
            can_bid=can_place_bid,
            can_ask=can_place_ask,
        )

    def _cancel_if_needed(self, state: StepState) -> list[object]:
        actions: list[object] = []
        existing = {o.order_id for o in state.own_orders}
        if self._bid_id in existing:
            actions.append(CancelOrder(self._bid_id))
        if self._ask_id in existing:
            actions.append(CancelOrder(self._ask_id))
        self._last_bid_tick = None
        self._last_ask_tick = None
        self._last_size = None
        return actions

    def _sync_quotes(
        self,
        state: StepState,
        *,
        bid_tick: int,
        ask_tick: int,
        size: float,
        can_bid: bool,
        can_ask: bool,
    ) -> list[object]:
        actions: list[object] = []
        own = {o.order_id: o for o in state.own_orders}

        if not can_bid:
            if self._bid_id in own:
                actions.append(CancelOrder(self._bid_id))
            self._last_bid_tick = None
        else:
            if self._bid_id not in own or self._last_bid_tick != bid_tick or self._last_size != size:
                if self._bid_id in own:
                    actions.append(CancelOrder(self._bid_id))
                actions.append(PlaceOrder(Side.BUY, bid_tick, size, client_order_id=self._bid_id))
                self._last_bid_tick = bid_tick

        if not can_ask:
            if self._ask_id in own:
                actions.append(CancelOrder(self._ask_id))
            self._last_ask_tick = None
        else:
            if self._ask_id not in own or self._last_ask_tick != ask_tick or self._last_size != size:
                if self._ask_id in own:
                    actions.append(CancelOrder(self._ask_id))
                actions.append(PlaceOrder(Side.SELL, ask_tick, size, client_order_id=self._ask_id))
                self._last_ask_tick = ask_tick

        self._last_size = size
        return actions

