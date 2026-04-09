from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Late-game tail hunter (high risk, 1-2 big trades).

    - Trades mostly late (fewer expected Poisson jumps remaining).
    - Waits for a shock proxy + stabilization in competitor best quotes.
    - Posts very large size at extreme ticks (low bid / high ask) so arb is unlikely to touch,
      but a rare huge retail order can.
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        # Baseline competitor midpoint (anchored near initial probability).
        self._baseline_mid: float | None = None
        self.baseline_alpha = 0.03
        self.baseline_learn_steps = 80

        # Stability signals.
        self._prev_mid: int | None = None
        self._vol_ewma = 0.0
        self.vol_alpha = 0.15
        self.vol_huge_max = 0.22

        self._prev_bid: int | None = None
        self._prev_ask: int | None = None
        self._stable_steps = 0
        self.stable_required = 6
        self.stable_huge_required = 10

        # Shock proxy and post-shock window.
        self.jump_mid_move_ticks = 6
        self.jump_spread_widen_ticks = 8
        self._cooldown = 0
        self.cooldown_steps = 10
        self._shock_timer = 0
        self.shock_window = 220
        # If we never see a detected shock in the late window, still allow hunting.
        self.require_shock_window = False

        # Late-game focus.
        self.late_only_frac = 0.28  # trade only when steps_remaining/total <= this

        # Dislocation thresholds vs baseline.
        self.ask_lift_delta = 14.0  # ask >> baseline
        self.bid_drop_delta = 14.0  # bid << baseline
        self.ask_lift_huge_delta = 20.0
        self.bid_drop_huge_delta = 20.0

        # Tail orders:
        # We split into two modes:
        # - Touch mode: join/step-inside competitor to get filled (higher arb risk).
        # - Extreme mode: only place huge size when the competitor touch is already extreme
        #   (i.e. arb has swept the ladder down/up), so our extreme price is near-touch.
        self.step_inside = 1

        self.target_size_buy_touch = 700.0
        self.target_size_sell_touch = 2500.0
        self.max_spread_to_step_inside = 6
        self.huge_spread_max = 3
        self.tiny_size = 30.0

        self.extreme_bid_tick = 12
        self.extreme_ask_tick = 88
        self.target_size_buy_extreme = 6000.0
        self.target_size_sell_extreme = 40000.0
        # Dynamic "behind-touch" extreme mode:
        # only place huge size when touch itself is extreme.
        self.extreme_bid_touch_max = 15
        self.extreme_ask_touch_min = 85
        self.extreme_behind_ticks = 1  # one tick behind touch
        self.extreme_burst_steps = 18
        self._extreme_burst = 0

        # Inventory / risk.
        self.inv_cap = 50_000.0
        self.net_hard = 800.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_baseline(self, mid: int) -> None:
        if self._baseline_mid is None:
            self._baseline_mid = float(mid)
        else:
            self._baseline_mid = (1.0 - self.baseline_alpha) * self._baseline_mid + self.baseline_alpha * float(mid)

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

        frac = self._time_fraction_remaining(state)
        late_phase = frac <= self.late_only_frac

        mid = (bid + ask) // 2
        self._update_baseline(mid)
        self._update_vol(mid)
        baseline = self._baseline_mid if self._baseline_mid is not None else float(mid)

        # Stability + shock detection from competitor best quotes.
        if self._prev_bid is None or self._prev_ask is None:
            self._stable_steps = 0
        else:
            moved = abs(bid - self._prev_bid) + abs(ask - self._prev_ask)
            if moved <= 1:
                self._stable_steps += 1
            else:
                self._stable_steps = 0

            prev_mid = (self._prev_bid + self._prev_ask) // 2
            prev_spread = self._prev_ask - self._prev_bid
            mid_move = abs(mid - prev_mid)
            spread_move = spread - prev_spread
            if mid_move >= self.jump_mid_move_ticks or spread_move >= self.jump_spread_widen_ticks:
                self._cooldown = self.cooldown_steps
                self._shock_timer = self.shock_window
                self._stable_steps = 0

        self._prev_bid, self._prev_ask = bid, ask

        # Preserve liquidity early; just learn baseline.
        if state.step < self.baseline_learn_steps or not late_phase:
            return [CancelAll()]

        if self._cooldown > 0:
            self._cooldown -= 1
            return [CancelAll()]

        if self.require_shock_window:
            if self._shock_timer > 0:
                self._shock_timer -= 1
            else:
                return [CancelAll()]

        if self._stable_steps < self.stable_required:
            return [CancelAll()]

        # Dislocation vs baseline (competitor is miscentered post-jump).
        ask_delta = float(ask) - baseline
        bid_delta = baseline - float(bid)
        hunt_buy = ask_delta >= self.ask_lift_delta and spread <= 6
        hunt_sell = bid_delta >= self.bid_drop_delta and spread <= 6
        if not hunt_buy and not hunt_sell:
            return [CancelAll()]

        huge_ok = (
            spread <= self.huge_spread_max
            and self._stable_steps >= self.stable_huge_required
            and self._vol_ewma <= self.vol_huge_max
            and (ask_delta >= self.ask_lift_huge_delta or bid_delta >= self.bid_drop_huge_delta)
        )

        # Inventory gates.
        net = state.yes_inventory - state.no_inventory
        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        # Track extreme burst window after a detected shock (if enabled).
        if self._shock_timer > 0:
            # if we recently saw a shock and are stable, allow a burst of extreme quotes
            if self._stable_steps >= self.stable_required:
                self._extreme_burst = max(self._extreme_burst, self.extreme_burst_steps)

        if self._extreme_burst > 0:
            self._extreme_burst -= 1

        actions: list[object] = [CancelAll()]  # TTL=1 always.

        # Extreme mode: only when competitor touch is already at extreme levels.
        # This is "huge size at extreme ticks where arb almost never trades".
        if self._extreme_burst > 0:
            # If best bid is already very low, place a huge bid one tick behind touch.
            if huge_ok and can_bid and bid <= self.extreme_bid_touch_max:
                px = max(1, bid - self.extreme_behind_ticks)
                max_qty = state.free_cash / max(0.01, px / 100.0)
                qty = min(self.target_size_buy_extreme, max_qty)
                if qty > 0.01 and px < ask:
                    actions.append(PlaceOrder(Side.BUY, px, qty))
                    return actions

            # If best ask is already very high, place a huge ask one tick behind touch.
            if huge_ok and can_ask and ask >= self.extreme_ask_touch_min:
                px = min(99, ask + self.extreme_behind_ticks)
                collateral_per = max(0.01, 1.0 - px / 100.0)
                max_qty = state.free_cash / collateral_per
                qty = min(self.target_size_sell_extreme, max_qty)
                if qty > 0.01 and bid < px:
                    actions.append(PlaceOrder(Side.SELL, px, qty))
                    return actions

        # Place only one side, at/inside competitor to get filled by retail.
        # Use collateral-aware sizing.
        if hunt_buy and can_bid:
            px = bid + self.step_inside if spread >= (self.step_inside + 1) and spread <= self.max_spread_to_step_inside else bid
            px = min(99, max(1, int(px)))
            max_qty = state.free_cash / max(0.01, px / 100.0)
            qty = min((self.target_size_buy_touch if huge_ok else self.tiny_size), max_qty)
            if qty > 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, qty))
            return actions

        if hunt_sell and can_ask:
            px = ask - self.step_inside if spread >= (self.step_inside + 1) and spread <= self.max_spread_to_step_inside else ask
            px = min(99, max(1, int(px)))
            collateral_per = max(0.01, 1.0 - px / 100.0)
            max_qty = state.free_cash / collateral_per
            qty = min((self.target_size_sell_touch if huge_ok else self.tiny_size), max_qty)
            if qty > 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, qty))
            return actions

        return actions

