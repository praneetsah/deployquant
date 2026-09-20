"""Quiet-bar fast path: skip minute bars that provably do nothing.

A session is eligible when user code has NO per-bar consumers: on_data is
not overridden (either spelling) and no MINUTE/SECOND-resolution indicator
is registered. The runtime's API surface is closed — everything outside
the supported subset raises UnsupportedApiError — so this enumeration is
complete: scheduled events, resting orders, daily indicators, history()
and the session-close equity mark are the only remaining observers, and
none of them needs the bar-by-bar walk.

The scanner never fills anything itself. It only picks which bars the
engine VISITS with the real per-bar machinery (price roll-forward,
check_resting, scheduled events). A false positive costs one extra visit;
a miss is impossible because the predicates below are exactly orders.py's
strict-breach fill rules evaluated on the same arrays. Stateful cases
resolve AT a visit through the real code: a stop-limit's stop leg
triggers at its visited bar, after which the scan switches to the limit
predicate from the next bar — preserving the limit-leg-live-next-bar
rule by construction. Eligibility is re-checked after every visit so a
handler that registers a minute indicator mid-day demotes the rest of
the day to the sequential walk.

Kill switch: DQENGINE_FAST_PATH=0.
"""
import numpy as np

from .enums import OrderType, Resolution


def on_data_is_noop(algo) -> bool:
    """True when neither on_data nor OnData is overridden — the same
    resolution rule resolve_hook uses, asked in reverse."""
    from .algorithm import QCAlgorithm, resolve_hook
    hook = resolve_hook(algo, "on_data", "OnData")
    return getattr(hook, "__func__", None) is QCAlgorithm.on_data


def has_consolidators(algo) -> bool:
    """A consolidator must see EVERY bar to aggregate correctly — the
    quiet-bar fast path visits only a few per session, so a session it
    skipped would silently produce a wrong hourly bar (or none at all).
    Same reason minute indicators disable it."""
    return any(algo._consolidators.get(s) for s in algo._consolidators)


def minute_indicator_count(algo) -> int:
    """Registered indicators needing per-bar delivery. Compared before and
    after every visit: a change mid-day means user code just subscribed to
    the stream we are skipping — fall back to the sequential walk."""
    n = 0
    for regs in algo._indicators.values():
        for res, _ in regs:
            if res in (Resolution.MINUTE, Resolution.SECOND):
                n += 1
    return n


def next_candidate_ms(book, ends_by, day_bars, last_t):
    """Earliest bar-end > last_t at which any resting order COULD act,
    or None. Predicates mirror OrderBook.check_resting exactly (strict
    breaches, same float operands); a deferred market order acts on its
    symbol's very next bar; an untriggered stop-limit scans its stop leg,
    a triggered one its limit leg."""
    best = None
    for t in list(book._open):
        if not t.is_open():
            continue
        b = day_bars.get(t.symbol)
        if b is None:
            continue
        ends = ends_by[t.symbol]
        pos = int(np.searchsorted(ends, last_t, side="right"))
        if pos >= len(ends):
            continue
        if t.order_type in (OrderType.MARKET_ON_OPEN, OrderType.MARKET_ON_CLOSE):
            # a clock triggers these, not a price: the session's first and
            # last bars are always visited, and that is where they fill
            continue
        if t.order_type in (OrderType.MARKET, OrderType.TRAILING_STOP,
                            OrderType.LIMIT_IF_TOUCHED):
            # a deferred market order acts on the next bar. A trailing stop
            # moves with every bar and a limit-if-touched has its own two
            # legs, so neither can be scanned ahead: visit every bar while
            # one rests (always right, one visit per bar).
            cand = int(ends[pos])
        else:
            hi = b.high[pos:]
            lo = b.low[pos:]
            buy = t.quantity > 0
            if t.order_type == OrderType.LIMIT:
                mask = (lo < t.limit_price) if buy else (hi > t.limit_price)
            elif t.order_type == OrderType.STOP_MARKET:
                mask = (hi > t.stop_price) if buy else (lo < t.stop_price)
            else:  # STOP_LIMIT
                if not t._triggered:
                    mask = (hi > t.stop_price) if buy else (lo < t.stop_price)
                else:
                    mask = (lo < t.limit_price) if buy else (hi > t.limit_price)
            nz = np.flatnonzero(mask)
            if nz.size == 0:
                continue
            cand = int(ends[pos + int(nz[0])])
        if best is None or cand < best:
            best = cand
    return best
