from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Market maker built from a participation-first microstructure model.

    First-principles view for this simulator:
    - We do not make money from predicting long-run direction.
    - We make money when our quotes survive the arb sweep and are then consumed
      by uninformed retail at favorable prices.
    - Therefore, the core decision is not "where is fair?" but "is the public
      spread wide enough to pay for the adverse-selection risk of quoting?".
    - We only quote when the observed spread comfortably exceeds our estimated
      uncertainty band; then we place orders near the safe edges of that band.
    """

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None

        # Public-state filter.
        self.fast_mid = 50.0
        self.slow_mid = 50.0
        self.flow_ewma = 0.0

        # Risk model inputs.
        self.vol_ewma = 0.0
        self.tox_ewma = 0.0
        self.adverse_run = 0
        self.wide_streak = 0
        self.stability = 0
        self.cooldown = 0

    def _buy_qty(self, tick: int, target: float, free_cash: float) -> float:
        return _q(min(target, free_cash / max(0.01, tick / 100.0)))

    def _sell_qty(self, tick: int, target: float, free_cash: float, yes_inventory: float) -> float:
        price = tick / 100.0
        covered = max(0.0, yes_inventory)
        uncovered = free_cash / max(0.01, 1.0 - price)
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

        touch_move = (
            0
            if self.prev_bid is None or self.prev_ask is None
            else abs(bid - self.prev_bid) + abs(ask - self.prev_ask)
        )
        self.prev_bid = bid
        self.prev_ask = ask
        self.wide_streak = self.wide_streak + 1 if spread >= 5 else 0
        self.stability = self.stability + 1 if touch_move <= 1 else 0

        buy_fill = state.buy_filled_quantity
        sell_fill = state.sell_filled_quantity
        fill_sum = buy_fill + sell_fill
        fill_imb = buy_fill - sell_fill

        adverse = 0.0
        if buy_fill > 0.0 and move < 0.0:
            adverse += abs(move) * buy_fill
        if sell_fill > 0.0 and move > 0.0:
            adverse += abs(move) * sell_fill

        if adverse > 0.18:
            self.adverse_run = min(10, self.adverse_run + 2)
        else:
            self.adverse_run = max(0, self.adverse_run - 1)

        self.fast_mid = 0.78 * self.fast_mid + 0.22 * mid
        self.slow_mid = 0.965 * self.slow_mid + 0.035 * mid
        self.flow_ewma = 0.88 * self.flow_ewma + 0.12 * fill_imb
        self.vol_ewma = 0.92 * self.vol_ewma + 0.08 * abs(move)
        self.tox_ewma = 0.90 * self.tox_ewma + 0.10 * (0.60 * adverse + 0.40 * abs(move) * fill_sum)

        actions: list[object] = [CancelAll()]
        if self.cooldown > 0:
            self.cooldown -= 1
            return actions

        # Large touch jumps imply we are extrapolating off stale public state.
        if touch_move >= 5 or self.tox_ewma > 0.85 or self.vol_ewma > 0.80:
            self.cooldown = 4
            return actions

        inventory = state.yes_inventory - state.no_inventory
        free_cash = max(0.0, state.free_cash)

        trend = self.fast_mid - self.slow_mid
        directional_signal = 0.65 * self.flow_ewma + 0.35 * trend

        # Estimated uncertainty band around a public reservation price.
        uncertainty = 1.15 * self.vol_ewma + 0.55 * self.tox_ewma + 0.08 * max(0, 2 - self.stability)
        band = min(3.5, max(1.25, 1.35 + 1.55 * uncertainty))
        required_spread = int(round(2.0 * band + 0.5))
        if spread < required_spread or self.wide_streak < 2:
            return actions

        # Reservation price from public information only, with a stronger
        # inventory penalty than directional alpha.
        reservation = mid + 0.18 * trend - 0.34 * self.flow_ewma - 0.045 * inventory

        # We quote near the safe band edges rather than near midpoint.
        raw_bid = reservation - band
        raw_ask = reservation + band
        buy_tick = _clip_tick(round(raw_bid))
        sell_tick = _clip_tick(round(raw_ask))
        buy_tick = max(bid, min(ask - 1, buy_tick))
        sell_tick = min(ask, max(bid + 1, sell_tick))

        # If the book is both wide and stable, step one tick inside; otherwise
        # keep the safer edge prices.
        if self.wide_streak >= 3 and self.stability >= 3 and self.tox_ewma < 0.18 and self.vol_ewma < 0.28:
            buy_tick = max(buy_tick, min(ask - 1, bid + 1))
            sell_tick = min(sell_tick, max(bid + 1, ask - 1))
        if buy_tick >= sell_tick:
            return actions

        # Size should scale with survival probability more than with spread.
        size = 5.2
        size *= max(0.24, 1.0 - 0.70 * self.vol_ewma - 0.45 * self.tox_ewma)
        if abs(inventory) > 70:
            size = min(size, 2.0)
        elif self.wide_streak >= 4 and self.stability >= 3 and self.tox_ewma < 0.16:
            size *= 1.15
        size = max(0.8, size)

        quote_buy = inventory < 165
        quote_sell = inventory > -165

        if directional_signal > 0.55:
            quote_sell = False
        elif directional_signal < -0.55:
            quote_buy = False

        # Under strong toxicity, only quote the inventory-reducing side.
        if self.tox_ewma > 0.26 or self.adverse_run >= 4:
            if inventory > 0:
                quote_buy = False
            elif inventory < 0:
                quote_sell = False
            else:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.30
        if quote_buy:
            buy_qty = self._buy_qty(buy_tick, size, budget)
            if buy_qty >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, buy_qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * buy_qty)
                budget = free_cash * 0.30

        if quote_sell:
            sell_qty = self._sell_qty(sell_tick, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, sell_qty))

        return actions
