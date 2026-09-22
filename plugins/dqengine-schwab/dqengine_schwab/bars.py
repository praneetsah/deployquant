"""Schwab's own record of the recent minutes, over REST.

The REST half of the feed next door. `SchwabQuoteFeed` streams a candle as
each minute closes; this fetches the candles of the last few sessions in
one call and puts them in `bar_days`, the table the stream writes and a
replay reads. Two callers want exactly that:

  * a worker whose stream has gone quiet. It still owes its strategy
    today's bars, and the honest answer is the same vendor's record of the
    minutes the socket missed -- not another vendor's idea of them, which
    is a different tape spliced into the middle of a session. That is why
    this sits in the plugin rather than in whatever composed the feed;
  * a chart or a page that wants the latest session without subscribing
    to it.

The hygiene is the stream's, one copy: the regular session only (13:00 on
an early close), a structurally impossible candle dropped rather than
stored, one row per minute. `bar_is_sane` and `in_regular_session` are
`dqengine.feeds.base`'s, and the feed applies them to every candle it
receives.

The write rule is this vendor's, and it is not the one
`dqengine.live.history.refresh` applies to Alpaca's bars. Here a stored day
holding at least as many rows as came back is left completely alone, and a
thinner one is REPLACED whole. Alpaca's merges the fetch onto what is
stored. Both are right about their own vendor: a Schwab price-history call
for a session returns that session, so the longer record is the better one
and a merge would keep minutes the vendor has since restated.

Credentials are the caller's. Every call takes `token`, a callable
returning a fresh Schwab access token, which is the same shape the feed
takes -- so a host with its own token store passes the same callable to
both, and nothing here reads a file or an environment variable.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dqengine.feeds.base import bar_is_sane, in_regular_session

ET = ZoneInfo("America/New_York")

PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"


def fetch_recent_minute_days(symbol: str, period_days: int = 10, *,
                             token) -> dict:
    """{date: [[ms,o,h,l,c,v], ...]} regular session only, from Schwab REST.
    endDate must be explicit: without it Schwab stops at the PREVIOUS session's
    close and today's completed bars never arrive (verified 2026-08-13)."""
    q = urllib.parse.urlencode({
        "symbol": symbol.upper(), "periodType": "day", "period": period_days,
        "frequencyType": "minute", "frequency": 1, "needExtendedHoursData": "false",
        "endDate": int(time.time() * 1000)})
    req = urllib.request.Request(
        f"{PRICE_HISTORY_URL}?{q}",
        headers={"Authorization": f"Bearer {token()}"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    out: dict = {}
    for c in d.get("candles", []):
        dt = datetime.fromtimestamp(c["datetime"] / 1000, tz=timezone.utc).astimezone(ET)
        ms = (dt.hour * 3600 + dt.minute * 60) * 1000
        if not in_regular_session(dt.date(), ms):    # 09:30 to the SESSION's close
            continue
        # same sanity gate as the stream — a malformed bar from either source
        # would be what a sleeve gets valued at (dqengine.feeds.bar_is_sane is
        # the one copy, and the live feed applies it to every candle)
        if not bar_is_sane(c["open"], c["high"], c["low"], c["close"]):
            continue
        out.setdefault(dt.date(), []).append(
            [ms, c["open"], c["high"], c["low"], c["close"], c["volume"]])
    for k in out:
        # Schwab can return the same minute twice (seen with period=2 queried
        # after midnight ET: the session arrives duplicated). One row per
        # minute, last occurrence wins — every consumer downstream (bar cache,
        # engine replay, charts) assumes unique minutes.
        dedup = {r[0]: r for r in sorted(out[k])}
        out[k] = [dedup[ms] for ms in sorted(dedup)]
    return out


# Per-symbol, because two deployments sharing a symbol now tick
# concurrently (live.py narrowed its global tick lock to one lock per
# deployment). Two threads upserting the same BarDay row would race: both
# see `existing is None` and both insert. Keyed by symbol so unrelated
# symbols still refresh in parallel.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_guard = threading.Lock()


def _refresh_lock(symbol: str) -> threading.Lock:
    with _refresh_guard:
        lk = _refresh_locks.get(symbol)
        if lk is None:
            lk = _refresh_locks[symbol] = threading.Lock()
        return lk


def refresh_bar_cache(symbol: str, *, token) -> int:
    """Fetch recent days from Schwab and upsert into bar_days. Returns days written."""
    with _refresh_lock(symbol.upper()):
        return _refresh_bar_cache(symbol, token=token)


def _refresh_bar_cache(symbol: str, *, token) -> int:
    # imported here, not at the top of the file: this is the one function in
    # the plugin that reaches a database, and an install that only trades
    # through the adapter never installs `deployquant[live]`
    from dqengine.live.persistence import BarDay, SessionLocal
    days = fetch_recent_minute_days(symbol, token=token)
    wrote = 0
    with SessionLocal() as s:
        for d, rows in days.items():
            if len(rows) < 2:
                continue
            existing = s.get(BarDay, (symbol.upper(), d))
            if existing is not None and len(existing.rows) >= len(rows):
                continue
            if existing is None:
                s.add(BarDay(symbol=symbol.upper(), day=d, rows=rows))
            else:
                existing.rows = rows
                existing.fetched_at = datetime.now(timezone.utc)   # export change signal
                existing.fetched_at = datetime.now(timezone.utc)
            wrote += 1
        s.commit()
    return wrote
