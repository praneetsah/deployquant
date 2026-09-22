"""Fetching over REST what the stream did not deliver.

The second half of the silence check. Once `dqengine.feeds.health` says
the feed is silent, a worker still owes its strategy today's bars, and
the history client already knows how to get them: `BarFeed.fetch_days`
for today, the in-progress minute cut, everything written through the
same store the stream writes to. The store's deduplication is what makes
this exact — only the minutes actually missing are written, and only
those are announced.

It never raises. A symbol whose REST call fails is logged and the rest of
the universe is still filled; a backfill that took the worker down would
turn a degraded feed into a stopped strategy.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from dqengine.feed import drop_in_progress
from dqengine.feeds.base import (MinuteBar, bar_is_sane, in_regular_session,
                                 say)


def backfill_day(bar_feed, store, symbols: Iterable[str], day: date,
                 now_ms: Optional[int] = None, log=say) -> dict:
    """Fetch `day` for each symbol over REST and write what is missing.

    `now_ms` is ms since midnight ET; bars that have not closed by it are
    left alone — the in-progress minute is the one bar a feed can hand
    over that a later read would answer differently.

    Returns {SYMBOL: [MinuteBar, ...]} for the bars that were actually
    written, which is the list of minutes the stream had missed."""
    filled = {}
    for symbol in sorted({s.upper() for s in symbols}):
        try:
            rows = (bar_feed.fetch_days(symbol, day, day) or {}).get(day, [])
        except Exception as e:                                # noqa: BLE001
            log(f"[feed] backfill of {symbol} {day} failed: {e!r}")
            continue
        if now_ms is not None:
            rows = drop_in_progress(rows, int(now_ms))
        bars = []
        for ms, o, h, l, c, v in rows:
            if not in_regular_session(day, ms) or not bar_is_sane(o, h, l, c):
                log(f"[feed] backfill dropped malformed {symbol} bar at {ms}")
                continue
            bars.append(MinuteBar(symbol, day, int(ms), float(o), float(h),
                                  float(l), float(c), float(v)))
        if not bars:
            continue
        try:
            written = store.write(bars)
        except Exception as e:                                # noqa: BLE001
            log(f"[feed] backfill store of {symbol} {day} failed: {e!r}")
            continue
        if written:
            log(f"[feed] backfilled {len(written)} minute(s) of {symbol} {day}")
            filled[symbol] = written
    return filled
