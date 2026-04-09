from __future__ import annotations

from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


def _clip_tick(tick: int) -> int:
    return max(1, min(99, int(tick)))


def _q(value: float) -> float:
    return max(0.0, round(value, 2))


class Strategy(BaseStrategy):
    """Market maker built from a reservation-price + risk-premium model.

    First-principles view for this simulator:
    - We do not make money from predicting long-run direction.
    - We make money when our quotes survive the arb sweep and are then consumed
      by uninformed retail at favorable prices.
    - Therefore, we estimate a reservation price and only quote if the observed
      competitor spread is wide enough to compensate for forecast error,
      toxicity, and inventory risk.
    """

    def __init__(self) -> None:
        self.prev_mid = 50.0
        self.prev_bid: int | None = None
        self.prev_ask: int | None = None

        # Latent fair-value filter.
        self.fast_mid = 50.0
        self.slow_mid = 50.0
        self.flow_ewma = 0.0

        # Risk model inputs.
        self.vol_ewma = 0.0
        self.tox_ewma = 0.0
        self.adverse_run = 0
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

        # Reservation price = filtered fair-value estimate minus inventory cost.
        trend = self.fast_mid - self.slow_mid
        reservation = mid + 0.35 * trend - 0.45 * self.flow_ewma - 0.035 * inventory

        # Risk premium required to survive arb + earn spread from retail.
        uncertainty = 0.85 * self.vol_ewma + 0.35 * self.tox_ewma + 0.08 * max(0, 2 - self.stability)
        half_spread = 1.0 + 1.75 * uncertainty
        half_spread = min(4.0, max(1.0, half_spread))

        # Only provide liquidity if the observed public spread can pay for our
        # risk premium with at least one tick of slack.
        required_spread = int(round(2.0 * half_spread + 1.0))
        if spread < required_spread:
            return actions

        # If conditions are quiet and the book is wide, improve the touch by one
        # tick; otherwise stay farther from the center to reduce arb exposure.
        improve = 1 if self.stability >= 3 and self.tox_ewma < 0.18 and self.vol_ewma < 0.30 else 0

        raw_bid = reservation - half_spread
        raw_ask = reservation + half_spread
        buy_tick = _clip_tick(round(raw_bid))
        sell_tick = _clip_tick(round(raw_ask))
        buy_tick = max(bid + improve, min(ask - 1, buy_tick))
        sell_tick = min(ask - improve, max(bid + 1, sell_tick))
        if buy_tick >= sell_tick:
            return actions

        # Size is the product of expected edge and survival probability, so we
        # shrink aggressively as the uncertainty estimate rises.
        size = 6.0
        size *= max(0.25, 1.0 - 0.55 * self.vol_ewma - 0.45 * self.tox_ewma)
        if abs(inventory) > 70:
            size = min(size, 2.0)
        size = max(0.8, size)

        quote_buy = inventory < 165
        quote_sell = inventory > -165

        directional_signal = 0.55 * self.flow_ewma + 0.45 * trend
        if directional_signal > 0.70:
            quote_sell = False
        elif directional_signal < -0.70:
            quote_buy = False

        # Under strong toxicity, only quote the inventory-reducing side.
        if self.tox_ewma > 0.28 or self.adverse_run >= 4:
            if inventory > 0:
                quote_buy = False
            elif inventory < 0:
                quote_sell = False
            else:
                quote_buy = False
                quote_sell = False

        budget = free_cash * 0.28
        if quote_buy:
            buy_qty = self._buy_qty(buy_tick, size, budget)
            if buy_qty >= 0.01 and buy_tick < ask:
                actions.append(PlaceOrder(Side.BUY, buy_tick, buy_qty))
                free_cash = max(0.0, free_cash - (buy_tick / 100.0) * buy_qty)
                budget = free_cash * 0.28

        if quote_sell:
            sell_qty = self._sell_qty(sell_tick, size, budget, state.yes_inventory)
            if sell_qty >= 0.01 and bid < sell_tick:
                actions.append(PlaceOrder(Side.SELL, sell_tick, sell_qty))

        return actions
