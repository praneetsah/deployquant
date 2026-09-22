"""Filling the bar cache from a vendor, and remembering what is filled.

Two tables hold bars for a live deployment. `hist_bars` is the adjusted
vendor history a warm-up reads; `bar_days` is the raw record of the
sessions this installation has actually seen. `dqengine.live.bar_source`
reads both. This module is the other half: it puts them there.

What is worth having here is not the HTTP -- that is
`dqengine.feed.AlpacaBarFeed`, one client for the whole distribution --
but the bookkeeping around it:

  * coverage as inclusive [start, end] ISO-date intervals in the KV table,
    meaning "everything the vendor has inside these windows is already
    cached". A non-trading day inside a fetched window simply has no bars,
    which is why coverage is recorded per RANGE and not per day;
  * a checkpoint per completed gap, so an interrupted multi-gap backfill
    resumes instead of refetching what it finished;
  * the upsert itself, including the one shape difference between what a
    feed hands back and what this cache has always held (see `cache_rows`).

`refresh` is the other direction: recent minutes into `bar_days`, for a
process with no feed running and as the fall-back for one whose feed went
silent. It applies the hygiene a feed's own store applies, because the two
write the same rows for the same readers.

Credentials are the host's: both calls take a `creds` callable returning
`(key_id, secret_key)`, and `ensure_history` only asks for it when there
is actually a gap to fetch. Pass a `feed` instead to use another vendor,
or neither and the standard Alpaca environment variables are read.

The two tables want different bars. History is fetched with
adjustment=all (splits AND dividends baked in): the engine has no
corporate-action handling, so total-return bars are the only way an
arbitrary ticker -- a dividend-heavy ETF above all -- replays correctly.
Today's minutes are fetched raw, because that is what printed.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dqengine.feed import (KEY_ENV, SECRET_ENV, AlpacaBarFeed,
                           drop_in_progress)
from dqengine.feeds import bar_is_sane, in_regular_session
from dqengine.live import persistence

ET = ZoneInfo("America/New_York")
REFRESH_DAYS = 5


# ------------------------------------------------------- interval bookkeeping

def merge_intervals(ivs: list[list[str]]) -> list[list[str]]:
    """Merge inclusive ISO-date intervals; adjacent (1-day gap) merge too."""
    out: list[list[str]] = []
    for s, e in sorted(ivs):
        if out and date.fromisoformat(s) <= (
                date.fromisoformat(out[-1][1]) + timedelta(days=1)):
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def missing_ranges(start: date, end: date,
                   known: list[list[str]]) -> list[tuple[date, date]]:
    """Sub-ranges of [start, end] not covered by the known intervals."""
    gaps: list[tuple[date, date]] = []
    cur = start
    for s_iso, e_iso in merge_intervals([list(x) for x in known]):
        s, e = date.fromisoformat(s_iso), date.fromisoformat(e_iso)
        if e < cur:
            continue
        if s > end:
            break
        if s > cur:
            gaps.append((cur, s - timedelta(days=1)))
        cur = max(cur, e + timedelta(days=1))
        if cur > end:
            break
    if cur <= end:
        gaps.append((cur, end))
    return gaps


def coverage_key(symbol: str) -> str:
    return f"histcov:{symbol.upper()}"


def load_coverage(key: str) -> list[list[str]]:
    with persistence.SessionLocal() as s:
        rec = s.get(persistence.KV, key)
        return rec.value if rec is not None else []


def save_coverage(key: str, ivs: list[list[str]]) -> None:
    with persistence.SessionLocal() as s:
        rec = s.get(persistence.KV, key)
        if rec is None:
            s.add(persistence.KV(key=key, value=ivs))
        else:
            rec.value = ivs
        s.commit()


# ------------------------------------------------------------ the bar client

def env_creds() -> tuple[str, str]:
    """The standard Alpaca environment variables. Market data is free on a
    paper account, so this is the one a self-hoster sets."""
    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not key or not secret:
        raise RuntimeError(
            f"no Alpaca market-data credentials: set {KEY_ENV} and "
            f"{SECRET_ENV} (a paper account's key is enough), or pass a "
            f"creds callable")
    return key, secret


def refresh_feed(creds=None, feed: str = "iex",
                 adjustment: str = "raw") -> AlpacaBarFeed:
    """The client for TODAY's minutes, which is a different ask.

    `bar_days` is the raw record of what actually printed, and the history
    under it is adjusted; splicing an adjusted today onto a raw yesterday
    puts a step in the series the size of every corporate action since,
    and the strategy trades the step. Hence adjustment="raw".

    The tape is IEX because Alpaca's free tier withholds the most recent
    minutes of SIP, which is exactly the window this call wants. A paid
    data subscription passes feed="sip"."""
    key_id, secret_key = (creds or env_creds)()
    return AlpacaBarFeed(key_id, secret_key, feed=feed, adjustment=adjustment)


def history_feed(creds=None, feed: str = "sip",
                 adjustment: str = "all") -> AlpacaBarFeed:
    """The bundled client, built from whatever credentials the host uses.

    `feed="sip"` is the whole consolidated tape. Alpaca's free tier serves
    full SIP HISTORY and withholds only the most recent minutes of it, so
    a backfill that stops at yesterday reads it happily."""
    key_id, secret_key = (creds or env_creds)()
    return AlpacaBarFeed(key_id, secret_key, feed=feed, adjustment=adjustment)


def cache_rows(rows: list) -> list:
    """Rows in the shape this cache holds: prices as floats, volume as the
    whole share count it is.

    A feed hands back all-float rows, because a replay reads floats. The
    vendor sends volume as an integer and every row already cached holds
    one, so writing 1000.0 where 1000 stands would rewrite a day on every
    refetch for a change of nothing. Prices stay floats: a price the vendor
    happens to emit as a bare integer is cached as 100.0 rather than 100,
    which is the same number to every reader of this table."""
    out = []
    for ms, o, h, l, c, v in rows:
        out.append([ms, o, h, l, c,
                    int(v) if float(v).is_integer() else v])
    return out


# ------------------------------------------------------------------ backfill

def ensure_history(symbol: str, start: date, end: date,
                   zip_days: list[date], progress_cb=None,
                   creds=None, feed=None) -> int:
    """Make `hist_bars` cover [start, end] for `symbol`; returns days written.

    `zip_days` are sessions a curated file store already has: they count as
    covered and are never fetched, but they are NOT written into the
    coverage ledger, which records only what the vendor was asked for.

    progress_cb(done_days, total_days) is called as pages land -- day
    granular, approximate (calendar days, not sessions), good enough for a
    progress bar.
    """
    sym = symbol.upper()
    end = min(end, datetime.now(ET).date() - timedelta(days=1))
    if end < start:
        return 0
    key = coverage_key(sym)
    known = list(load_coverage(key))
    if zip_days:
        known.append([min(zip_days).isoformat(), max(zip_days).isoformat()])
    gaps = missing_ranges(start, end, known)
    if not gaps:
        return 0
    # asked for only now: a fully covered range must cost neither a
    # credential lookup nor a request
    bars = feed if feed is not None else history_feed(creds)
    total = sum((g[1] - g[0]).days + 1 for g in gaps)
    done_base, wrote = 0, 0
    for g_start, g_end in gaps:
        def _cb(latest_day, _g_start=g_start, _base=done_base):
            if progress_cb:
                progress_cb(_base + (latest_day - _g_start).days + 1, total)
        days = bars.fetch_days(sym, g_start, g_end, progress=_cb)
        with persistence.SessionLocal() as s:
            for day, rows in days.items():
                if len(rows) < 2:
                    continue
                existing = s.get(persistence.HistBar, (sym, day))
                rows = cache_rows(rows)
                if existing is None:
                    s.add(persistence.HistBar(symbol=sym, day=day, rows=rows))
                else:
                    existing.rows = rows
                    existing.fetched_at = datetime.now(timezone.utc)
                wrote += 1
            s.commit()
        done_base += (g_end - g_start).days + 1
        # record coverage per completed gap so an interrupted multi-gap
        # backfill doesn't refetch finished ranges
        save_coverage(key, merge_intervals(
            load_coverage(key) + [[g_start.isoformat(), g_end.isoformat()]]))
    return wrote


# ------------------------------------------------------------- live refresh

_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_GUARD = threading.Lock()


def _refresh_lock(symbol: str) -> threading.Lock:
    """One refresh of a symbol at a time in this process.

    Two threads refreshing the same day would each read the stored rows,
    each merge its own fetch onto what it read, and the second to commit
    would write the first's minutes away. Across PROCESSES there is no
    lock: an install with one deployment refreshes from one of them.
    """
    with _REFRESH_GUARD:
        lk = _REFRESH_LOCKS.get(symbol)
        if lk is None:
            lk = _REFRESH_LOCKS[symbol] = threading.Lock()
        return lk


def refresh(symbol: str, days: int = REFRESH_DAYS, creds=None, feed=None,
            source: str = "rest", now=None) -> int:
    """Recent minutes into `bar_days`; returns the number of DAYS changed.

    This is the REST way to reach the same table a live feed writes: for a
    process with no feed running, and as the fall-back for one whose feed
    has gone silent. It applies the hygiene a feed's own store applies,
    because the two write the same rows for the same readers:

      * the regular session only, 09:30 to 16:00, and 13:00 on an early
        close. Applied here and not left to the client, so a host that
        passes its own `feed` fills the same table;
      * a structurally impossible candle is dropped rather than stored;
      * one row per minute, the fetched one winning over what is there;
      * the in-progress minute is never written. A partial row read as a
        closed bar lets a stop trigger on a low the real minute never
        printed;
      * whole volumes stay whole;
      * a day with fewer than two usable bars is not stored at all, which
        is what every reader of this table already asks of a day.

    A day already stored is MERGED with, not replaced by, what comes back,
    so a feed's minutes that this call did not fetch survive it. A day that
    comes back identical is left completely alone, `fetched_at` included,
    since readers use that stamp to decide whether to re-export the day.

    The vendor is asked before a database session is opened, so a request
    that fails raises with nothing written -- never half a day.
    """
    sym = symbol.upper()
    now = datetime.now(ET) if now is None else now
    if now.tzinfo is not None:
        # the window and the in-progress cut are both the exchange's clock,
        # whatever zone the caller's is set to
        now = now.astimezone(ET)
    end = now.date()
    start = end - timedelta(days=max(1, days) - 1)
    with _refresh_lock(sym):
        bars = feed if feed is not None else refresh_feed(creds)
        fetched = bars.fetch_days(sym, start, end)
        # the current minute has not closed, so the cut is the top of it
        now_ms = (now.hour * 3600 + now.minute * 60) * 1000
        wrote = 0
        with persistence.SessionLocal() as s:
            for day, rows in sorted(fetched.items()):
                rows = [r for r in rows
                        if in_regular_session(day, r[0])
                        and bar_is_sane(r[1], r[2], r[3], r[4])]
                if day == end:
                    rows = drop_in_progress(rows, now_ms)
                if len(rows) < 2:
                    continue
                rec = s.get(persistence.BarDay, (sym, day))
                merged = {} if rec is None else {int(r[0]): list(r)
                                                 for r in rec.rows}
                merged.update({int(r[0]): r for r in cache_rows(rows)})
                out = [merged[ms] for ms in sorted(merged)]
                if rec is None:
                    s.add(persistence.BarDay(symbol=sym, day=day, rows=out,
                                             source=source))
                elif [list(r) for r in rec.rows] == out:
                    continue                  # nothing new: leave the stamp
                else:
                    rec.rows = out
                    rec.fetched_at = datetime.now(timezone.utc)
                wrote += 1
            s.commit()
    return wrote
