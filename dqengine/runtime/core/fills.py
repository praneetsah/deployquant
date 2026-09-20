"""Fill-price rules — the single implementation, shared by both engines.

These were LEAN-verified in `engine.py` and then COPIED into
`dqengine/runtime/orders.py`, which left the money path with two implementations of
the same semantics kept in agreement only by an acceptance test. A fix applied
to one and not the other is a silent divergence in what reaches a broker. This
module is that single implementation; both engines call it.

Every breach is STRICT — a bar merely touching the level does not fill
(LEAN-verified: 2022-04-20 high == 24.18 == limit -> no fill). That strictness
is the whole reason these live in one place: `>` vs `>=` here is a different
set of trades, and the difference is invisible until it is real money.

    buy  limit -> Low  <  limit : fill at min(open, limit)
    sell limit -> High >  limit : fill at max(open, limit)
    buy  stop  -> High >  stop  : fill at max(open, stop)
    sell stop  -> Low  <  stop  : fill at min(open, stop)

Composite orders trigger on one leg and fill on the other, and the limit leg
only goes live on the NEXT bar: a stop breach and a limit fill cannot both be
read off the same bar's OHLC. `stop_limit` approaches its trigger from the
ADVERSE side (a buy triggers on a rise to it); `limit_if_touched` is its
mirror, approaching from the FAVOURABLE side (a buy triggers on a fall to it).

All functions are pure and return `None` for "no fill", so a caller can write
`if (px := limit_fill(...)) is not None` without a separate breach test that
could drift from the fill price it implies.
"""
from __future__ import annotations

# LEAN rounds order prices to the minimum price variation.
MIN_PRICE_VARIATION = 0.01


def round_price(px: float) -> float:
    """Round to the minimum price variation, as LEAN does."""
    return round(float(px), 2)


def limit_fill(buy: bool, o: float, h: float, l: float,
               limit: float | None) -> float | None:
    """Fill price for a resting limit order, or None if this bar did not
    breach it. Buy fills on a strict dip below; sell on a strict rise above."""
    if limit is None:
        return None
    if buy:
        return min(o, limit) if l < limit else None
    return max(o, limit) if h > limit else None


def stop_fill(buy: bool, o: float, h: float, l: float,
              stop: float | None) -> float | None:
    """Fill price for a resting stop (stop-market) order, or None. The mirror
    of `limit_fill`: a buy stop triggers ABOVE, a sell stop BELOW."""
    if stop is None:
        return None
    if buy:
        return max(o, stop) if h > stop else None
    return min(o, stop) if l < stop else None


def stop_triggered(buy: bool, h: float, l: float,
                   stop: float | None) -> bool:
    """Has a stop_limit's trigger leg been breached on this bar?

    Same adverse-side test as `stop_fill` uses, kept separate because
    conversion and filling happen on different bars.
    """
    if stop is None:
        return False
    return h > stop if buy else l < stop


def touch_triggered(buy: bool, h: float, l: float,
                    trigger: float | None) -> bool:
    """Has a limit_if_touched's trigger leg been breached on this bar?

    The mirror of `stop_triggered` — approached from the favourable side.
    """
    if trigger is None:
        return False
    return l < trigger if buy else h > trigger
