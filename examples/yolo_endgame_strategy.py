from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


class Strategy(BaseStrategy):
    """Simple endgame YOLO strategy (high risk, seek big right-tail).

    - Trades only late to reduce remaining-jump horizon.
    - Detects post-jump miscentering vs an early baseline midpoint.
    - Places a single large order at/inside the competitor touch (TTL=1).
    """

    def __init__(self) -> None:
        self._total_steps: int | None = None

        # Baseline competitor midpoint learned early
        self._baseline_mid: float | None = None
        self.baseline_alpha = 0.05
        self.baseline_learn_steps = 60

        # Endgame only
        self.late_only_frac = 0.20

        # Jump proxy: big change in competitor mid triggers a brief "go" window
        self._prev_mid: int | None = None
        self.jump_mid_move_ticks = 5
        self._go_timer = 0
        self.go_steps = 25

        # Dislocation thresholds (ticks)
        self.ask_lift_delta = 10.0  # ask much higher than baseline
        self.bid_drop_delta = 10.0  # bid much lower than baseline

        # Execution
        self.step_inside = 1
        self.max_spread = 6
        self.size_buy = 900.0
        self.size_sell = 3500.0

        # Risk caps
        self.inv_cap = 20_000.0
        self.net_hard = 1200.0

    def _time_fraction_remaining(self, state: StepState) -> float:
        if self._total_steps is None:
            self._total_steps = max(1, state.steps_remaining)
        return max(0.0, min(1.0, state.steps_remaining / float(self._total_steps)))

    def _update_baseline(self, mid: int) -> None:
        if self._baseline_mid is None:
            self._baseline_mid = float(mid)
        else:
            self._baseline_mid = (1.0 - self.baseline_alpha) * self._baseline_mid + self.baseline_alpha * float(mid)

    def on_step(self, state: StepState):
        bid = state.competitor_best_bid_ticks
        ask = state.competitor_best_ask_ticks
        if bid is None or ask is None:
            return [CancelAll()]

        spread = ask - bid
        if spread <= 0 or spread > self.max_spread:
            return [CancelAll()]

        frac = self._time_fraction_remaining(state)
        if frac > self.late_only_frac:
            # learn baseline early, otherwise sit out
            mid0 = (bid + ask) // 2
            if state.step < self.baseline_learn_steps:
                self._update_baseline(mid0)
            self._prev_mid = mid0
            return [CancelAll()]

        mid = (bid + ask) // 2
        self._update_baseline(mid)
        baseline = self._baseline_mid if self._baseline_mid is not None else float(mid)

        # Jump proxy
        if self._prev_mid is not None and abs(mid - self._prev_mid) >= self.jump_mid_move_ticks:
            self._go_timer = self.go_steps
        self._prev_mid = mid

        if self._go_timer > 0:
            self._go_timer -= 1
        else:
            return [CancelAll()]

        ask_delta = float(ask) - baseline
        bid_delta = baseline - float(bid)

        hunt_buy = ask_delta >= self.ask_lift_delta
        hunt_sell = bid_delta >= self.bid_drop_delta
        if not hunt_buy and not hunt_sell:
            return [CancelAll()]

        # Inventory gates + one-sided at extremes
        net = state.yes_inventory - state.no_inventory
        can_bid = state.free_cash > 0 and state.yes_inventory < self.inv_cap
        can_ask = state.no_inventory < self.inv_cap
        if abs(net) >= self.net_hard:
            if net > 0:
                can_bid = False
            else:
                can_ask = False

        actions: list[object] = [CancelAll()]  # TTL=1

        # Prefer one side (whichever signal is stronger); if tied, pick the side that reduces inventory.
        if hunt_buy and hunt_sell:
            if abs(ask_delta) > abs(bid_delta):
                hunt_sell = False
            elif abs(bid_delta) > abs(ask_delta):
                hunt_buy = False
            else:
                if net > 0:
                    hunt_buy = False
                else:
                    hunt_sell = False

        if hunt_buy and can_bid:
            px = bid + self.step_inside if spread >= 3 else bid
            px = min(99, max(1, int(px)))
            max_qty = state.free_cash / max(0.01, px / 100.0)
            qty = min(self.size_buy, max_qty)
            if qty > 0.01 and px < ask:
                actions.append(PlaceOrder(Side.BUY, px, qty))
            return actions

        if hunt_sell and can_ask:
            px = ask - self.step_inside if spread >= 3 else ask
            px = min(99, max(1, int(px)))
            collateral_per = max(0.01, 1.0 - px / 100.0)
            max_qty = state.free_cash / collateral_per
            qty = min(self.size_sell, max_qty)
            if qty > 0.01 and bid < px:
                actions.append(PlaceOrder(Side.SELL, px, qty))
            return actions

        return actions

