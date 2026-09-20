"""Sleeve portfolio simulation: positions, cash, margin, fills, order log.

Parity notes (deliberate, to match the reference strategy's LEAN semantics):
- entry_price mode "last_fill": any BUY fill resets entry_price/entry_day to that
  fill (the script's on_order_event does exactly this — it is NOT average cost).
  Platform default will be "avg_cost" later; tqqq_weekly validation uses "last_fill".
- Margin: orders whose notional exceeds available buying power are REJECTED whole
  (LEAN behavior the script relies on: avg-down on a vol-boost week must reject).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Fill:
    day: date
    time_ms: int
    symbol: str
    qty: int                 # signed
    price: float
    tag: str
    # ---- appended with defaults: every existing positional construction
    # keeps working and the LEAN parity path stays bit-exact.
    fees: float = 0.0
    # what the MODEL would have filled at (bar close). Retained purely as a
    # diagnostic so the UI can show realized slippage; never used in
    # accounting once a real price is present.
    model_price: Optional[float] = None
    # False when the broker has not confirmed this fill yet (same-minute) —
    # the price is still the model's and the UI must say so.
    confirmed: bool = True


@dataclass
class OrderRecord:
    day: date
    time_ms: int
    symbol: str
    qty: int
    kind: str                # market | limit | market_on_close | limit_on_close | ...
    tag: str
    status: str               # filled | rejected | canceled | placed | expired
    # limit price for kinds that carry one (currently only limit_on_close);
    # appended last, defaulted, so existing positional call sites are untouched.
    price: Optional[float] = None


# UNREFERENCED since Phase 3 Task 9, and that is the first thing to know here.
# The only code that ever constructed a ManagedOrder was the old IR engine's
# engine.py, which was deleted; `Sleeve.targets` below is the declared home for these
# and is now never written. Nothing in dqengine.runtime or the api creates one. So this is
# not load-bearing today, and the banner that used to say CRITICAL pointed at a call
# site in a file that no longer exists.
#
# It is kept rather than deleted because removing it means deciding whether the python
# engine re-adopts managed orders (dqengine.codegen still refuses `trailing_stop` targets) or
# drops the feature — a deliberate change to money-path code, not a side effect of a
# deletion. Whoever makes that call should delete this type or revive it outright.
#
# If it IS revived: the first four fields (rule_id, symbol, limit_price, tag) plus
# placed_day are the shape the old engine built positionally, and
# tests/runtime/test_orders_portfolio.py still pins that order. Reordering them is
# silent rather than loud — a dataclass enforces no types at runtime, so a float binds
# happily into `kind` instead of `limit_price`. Append new fields after placed_day.
@dataclass
class ManagedOrder:
    """A standing order a rule maintains (update-in-place).

    kind:
      limit         — the original GTC sell-limit take-profit
      stop          — sell stop: low < stop_price -> min(open, stop_price)
      stop_limit    — stop that converts to a resting limit at limit_price
      trailing_stop — stop at high_water * (1 - trail_pct), ratcheting up only
    """
    rule_id: str
    symbol: str
    limit_price: Optional[float] = None
    tag: str = ""
    placed_day: Optional[date] = None       # day the ticket was first created
    kind: str = "limit"
    stop_price: Optional[float] = None
    trail_pct: Optional[float] = None
    high_water: Optional[float] = None      # trailing_stop only
    triggered: bool = False                 # stop_limit: stop hit, limit resting
    sibling_id: Optional[str] = None        # OCO partner (bracket)

    def effective_stop(self) -> Optional[float]:
        """Trailing stops derive their level from the high-water mark."""
        if self.kind == "trailing_stop":
            if self.high_water is None or self.trail_pct is None:
                return None
            return round(self.high_water * (1.0 - self.trail_pct), 2)
        return self.stop_price


# the pre-v0.3 name; kept so existing imports keep working
ManagedTarget = ManagedOrder


@dataclass
class EntryOrder:
    """A resting BUY entry order (breakout stop / pullback limit /
    stop_limit) — see strategy-ir-spec.md §15.5.

    Deliberately NOT a ManagedOrder: ManagedOrder/_check_targets's
    `qty <= 0` cleanup is exit-shaped (a target protecting nothing left is
    deleted), which is the wrong lifecycle here — an entry order has no
    position yet, carries its own fixed `qty` to buy, and is removed only
    once it fills or its rule stops firing it.

    kind:
      limit         — buy-the-dip: low < price -> min(open, price)
      stop          — breakout: high > stop -> max(open, stop)
      stop_limit    — breakout that converts to a resting limit at price
    """
    rule_id: str
    symbol: str
    qty: int
    kind: str = "limit"
    price: Optional[float] = None           # limit level (limit / stop_limit's resting leg)
    stop: Optional[float] = None            # stop level (stop / stop_limit's trigger)
    tag: str = ""
    placed_day: Optional[date] = None       # day the ticket was first created
    triggered: bool = False                 # stop_limit: stop hit, limit resting


class Sleeve:
    def __init__(self, cash: float, margin_max: float = 1.0,
                 entry_price_mode: str = "last_fill"):
        self.cash = cash
        # optional live override, installed by the caller: a callable that
        # returns the current ceiling. The dqengine.runtime uses it so a strategy's
        # set_leverage is honored whenever it is called, not only as it stood
        # at the end of initialize(). Unset (the IR engine) = the fixed value.
        self.margin_provider = None
        self._margin_max = float(margin_max)
        self.entry_price_mode = entry_price_mode
        self.qty: dict[str, int] = {}
        self.entry_price: dict[str, float] = {}
        self.entry_day: dict[str, date] = {}
        self.avg_cost: dict[str, float] = {}
        self.peak_close: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.orders: list[OrderRecord] = []
        self.targets: dict[str, ManagedTarget] = {}     # rule_id -> target
        self.entries: dict[str, EntryOrder] = {}        # rule_id -> resting entry

    # ---------- valuation ----------

    def position_value(self, prices: dict[str, float]) -> float:
        return sum(q * prices.get(s, 0.0) for s, q in self.qty.items() if q != 0)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.position_value(prices)

    @property
    def margin_max(self) -> float:
        return self.margin_provider() if self.margin_provider else self._margin_max

    @margin_max.setter
    def margin_max(self, value: float):
        self._margin_max = float(value)

    def gross_exposure(self, prices: dict[str, float]) -> float:
        """Long + |short|. What the margin ceiling is actually a ceiling on:
        a short consumes buying power like a long, it does not hand it back.
        Identical to position_value for a long-only book."""
        return sum(abs(q) * prices.get(s, 0.0)
                   for s, q in self.qty.items() if q != 0)

    def buying_power(self, prices: dict[str, float]) -> float:
        return self.margin_max * self.equity(prices) - self.gross_exposure(prices)

    def invested(self, symbol: str) -> bool:
        return self.qty.get(symbol, 0) != 0

    # ---------- fills ----------

    def apply_fill(self, day: date, time_ms: int, symbol: str, qty: int,
                   price: float, tag: str, fees: float = 0.0,
                   model_price: Optional[float] = None,
                   confirmed: bool = True):
        prev = self.qty.get(symbol, 0)
        new = prev + qty
        self.cash -= qty * price + fees
        self.qty[symbol] = new
        self.fills.append(Fill(day, time_ms, symbol, qty, price, tag,
                               fees, model_price, confirmed))
        # avg cost. Long paths are bit-identical to the original (buys from
        # flat/long: unchanged math; a buy from short was 'price' before and
        # still is when it FLIPS long). The short branches only run for
        # positions long-only strategies never hold — a short entry used to
        # record no basis at all, leaving average_price 0 (python-runtime
        # user report, 2026-08-30); a partial cover keeps the basis.
        if qty > 0 and prev >= 0:
            self.avg_cost[symbol] = price if prev == 0 else \
                (self.avg_cost[symbol] * prev + price * qty) / new
        elif qty < 0 and prev <= 0:
            self.avg_cost[symbol] = price if prev == 0 else \
                (self.avg_cost[symbol] * -prev + price * -qty) / -new
        elif new != 0 and (prev > 0) != (new > 0):
            self.avg_cost[symbol] = price       # flipped through zero
        if qty > 0:
            # last-fill entry semantics (parity with the reference strategy)
            self.entry_price[symbol] = price
            self.entry_day[symbol] = day
            self.peak_close[symbol] = max(self.peak_close.get(symbol, 0.0), price)
        if new == 0:
            self.entry_price.pop(symbol, None)
            self.entry_day.pop(symbol, None)
            self.avg_cost.pop(symbol, None)
            self.peak_close.pop(symbol, None)

    def get_entry_price(self, symbol: str) -> Optional[float]:
        if self.entry_price_mode == "avg_cost":
            return self.avg_cost.get(symbol)
        return self.entry_price.get(symbol)
