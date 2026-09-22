"""What a live feed delivers, and the order it delivers it in.

`BarFeed` (dqengine.feed) answers "what were the bars"; a `QuoteFeed`
answers "what is happening now". Two streams come out of it:

  * **completed regular-session minute bars**, one callback per bar, fired
    only *after* the bar is in the store. A consumer woken by the callback
    pulls the bar from the store, so the write has to be visible first —
    and a whole wire frame is stored before any of it is announced, because
    a reader that pulls a half-written minute steps its engine past the
    stragglers still in flight;
  * **L1 quote and trade updates** for a quote board. Quotes never reach
    the engine as bars; they price a scheduled fire and they detect a
    breached simulated stop. That boundary is what keeps a live run and a
    replay of the same day equivalent.

Shape: callbacks plus a synchronous `poll()` pump. The live worker is a
plain process with a loop, and the hosted platform's own streamer is
already written this way — one frame loop that stores, then notifies, then
folds the quote into a board. An async iterator would invert that loop and
force every adapter to be rewritten around it.

`FeedState` carries exactly the fields the platform's health block
reports, so a feed's state can be handed to it unchanged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Protocol, Sequence

from dqengine.runtime.core.data import REG_OPEN_MS, close_time_ms


@dataclass(frozen=True)
class MinuteBar:
    """One completed regular-session minute, in exchange time.

    `start_ms` is the bar's START, ms since midnight ET — the same clock
    the store's rows and the engine's `start_ms` use, so nothing converts
    between the live path and a replay."""
    symbol: str
    day: date
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def row(self) -> list:
        return [self.start_ms, self.open, self.high, self.low, self.close,
                self.volume]


@dataclass(frozen=True)
class QuoteTick:
    """One L1 update. Partial by nature: a venue conflates and sends only
    the fields that changed, so any of bid/ask/last may be None.

    `at_ms` is when this frame arrived; `last_at_ms` is when the last
    TRADE printed. They differ, and the difference matters: a thin ETF
    whose last print was 15:40 but whose bid ticked at 15:58:59 would
    otherwise report a second of staleness for a 19-minute-old price."""
    symbol: str
    at_ms: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    last_at_ms: Optional[int] = None


@dataclass
class FeedState:
    """Connection state, in the shape the platform's health block reports.

    Times are epoch seconds (the silence check does arithmetic on them);
    `as_status()` renders the ISO strings the health JSON carries today."""
    connected: bool = False
    last_frame_at: Optional[float] = None
    last_bar_at: Optional[float] = None
    error: Optional[str] = None
    symbols: tuple = ()

    def as_status(self) -> dict:
        return {"connected": self.connected,
                "last_frame_at": _iso(self.last_frame_at),
                "last_bar_at": _iso(self.last_bar_at),
                "error": self.error,
                "symbols": list(self.symbols)}

    @classmethod
    def from_status(cls, status: dict) -> "FeedState":
        """The inverse of `as_status`, for a process reading another
        process's feed state (a worker reads it off the bus). Anything
        unreadable comes back as None rather than raising: the silence
        check then treats it as "nothing heard", which is the safe answer
        for a state nobody can parse."""
        status = status or {}
        return cls(connected=bool(status.get("connected")),
                   last_frame_at=_epoch(status.get("last_frame_at")),
                   last_bar_at=_epoch(status.get("last_bar_at")),
                   error=status.get("error"),
                   symbols=tuple(status.get("symbols") or ()))


def _iso(epoch_s: Optional[float]) -> Optional[str]:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


class BarStore(Protocol):
    """Where a feed puts a bar before it tells anyone about it.

    `write` takes a WHOLE frame and returns only the bars that changed
    something: a resent candle identical to what is already stored is
    nothing to store and nothing to announce."""

    def write(self, bars: Sequence[MinuteBar]) -> list: ...


class QuoteFeed(Protocol):
    """A live feed. `state` is readable at any time; `poll` is the pump.

    A feed may also implement `BarRefresher` below, which is the REST half
    of the same vendor."""

    state: FeedState

    def subscribe(self, symbols: Iterable[str]) -> None: ...

    def unsubscribe(self, symbols: Iterable[str]) -> None: ...

    def poll(self, timeout: float = 1.0) -> int:
        """Handle at most one wire frame. Returns how many messages it
        carried. Never raises on feed trouble: a dead connection is
        recorded in `state` and retried, because the loop that calls this
        also has a strategy to run."""

    def health(self, symbols: Iterable[str] = (), now_et=None, policy=None):
        """`ok` or `silent` for the symbols the caller needs (see
        dqengine.feeds.health)."""

    def close(self) -> None: ...


class BarRefresher(Protocol):
    """Optional on a `QuoteFeed`: the same vendor's recent minutes over
    REST, written to the bar tables a replay reads.

    A stream that has gone quiet is why this exists. The worker watching it
    still owes its strategy today's bars, and the answer that keeps a live
    run honest is the SAME vendor's own record of the minutes it missed --
    not a second vendor's idea of them, which is a different tape spliced
    into the middle of a session. So the fall-back is a capability of the
    feed rather than a separate composition, and an install that names one
    feed gets that feed's REST bars without saying so twice.

    `refresh_bars` returns the number of DAYS it changed, not rows: the
    caller uses it as "was there anything new", and a day either has fresh
    minutes in it or it has not. It fetches before it writes and writes
    whole days, so a vendor call that fails leaves the tables as they were.

    The write rule itself is the vendor's, deliberately. What a stored day
    already holds and what the REST call returns are two records of the
    same session, and how they reconcile depends on which one is more
    complete -- a question only the vendor's own behaviour answers (see
    `dqengine.live.history.refresh`, which merges, against the Schwab
    plugin's, which replaces a thinner day whole). A feed without this
    capability is not a broken feed: it simply has no REST bars, and
    `dqengine.live.bar_source` says so out loud rather than failing once
    per symbol."""

    def refresh_bars(self, symbol: str) -> int: ...


def bar_refresher(feed):
    """`feed.refresh_bars` when the feed has it, else None.

    One copy of the question, asked by the bar source and by anything that
    reports what an install would do while its stream is quiet."""
    fn = getattr(feed, "refresh_bars", None)
    return fn if callable(fn) else None


class Socket(Protocol):
    """The websocket, injectable so nothing in a test opens a connection."""

    def send(self, text: str) -> None: ...

    def recv(self, timeout: Optional[float] = None) -> str: ...

    def close(self) -> None: ...


def bar_is_sane(o, h, l, c) -> bool:
    """Reject structurally impossible OHLC.

    A feed occasionally emits a malformed candle — an observed one read
    o=539.0, h=72.55, l=72.65, c=72.40, v=73: low above high, an open
    eight times the price, and 73 shares in a minute that traded 200k+.
    Stored, it replaced a good bar, and being the last bar of the day it
    marked every position off nonsense.

    Prices finite and positive, low not above high, open and close inside
    the range — the invariants that make a bar a bar. A bar failing them
    is dropped, which leaves the previous good one in place."""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
        return False
    if l > h:
        return False
    return l <= o <= h and l <= c <= h


def in_regular_session(day: date, start_ms: int) -> bool:
    """[09:30, close) in exchange time, where close is 13:00 on a half-day.
    Feeds keep pushing candles until 16:00 on those days; stored as regular
    bars they fill resting orders on after-hours prints."""
    return REG_OPEN_MS <= int(start_ms) < close_time_ms(day)


class MinuteZipStore:
    """`BarStore` over the LEAN-layout zips the engine reads.

    The day's rows are held in memory and the zip rewritten atomically on
    every change, so a reader woken by the callback that follows always
    finds the minute on disk. Deduplication is done on the STORED encoding
    (prices x10000), which is what a replay reads back, not on the float
    that arrived on the wire."""

    def __init__(self, data_root: str):
        self.root = data_root
        self._days: dict = {}

    def _rows_for(self, symbol: str, day: date) -> dict:
        key = (symbol.upper(), day)
        rows = self._days.get(key)
        if rows is None:
            rows = self._days[key] = _load_day(self.root, symbol, day)
        return rows

    def write(self, bars: Sequence[MinuteBar]) -> list:
        from dqengine.store import write_minute_day

        changed, dirty = [], set()
        for bar in bars:
            if not in_regular_session(bar.day, bar.start_ms):
                continue
            rows = self._rows_for(bar.symbol, bar.day)
            row = bar.row()
            if _scaled(rows.get(bar.start_ms)) == _scaled(row):
                continue          # identical resend: nothing to write, nothing to say
            rows[int(bar.start_ms)] = row
            dirty.add((bar.symbol.upper(), bar.day))
            changed.append(bar)
        for symbol, day in sorted(dirty):
            rows = self._days[(symbol, day)]
            write_minute_day(self.root, symbol, day,
                             [rows[ms] for ms in sorted(rows)])
        return changed


def _scaled(row):
    """A row in the encoding the zip holds, for comparison. None stays
    None so a minute that is not stored yet never equals one that is."""
    if row is None:
        return None
    from dqengine.store import scaled_rows
    return tuple(scaled_rows([row])[0])


def _load_day(data_root: str, symbol: str, day: date) -> dict:
    from dqengine.runtime.core.data import DataStore

    bars = DataStore(data_root).load_minute_day(symbol, day)
    if bars is None:
        return {}
    return {int(bars.start_ms[i]): [int(bars.start_ms[i]), float(bars.open[i]),
                                    float(bars.high[i]), float(bars.low[i]),
                                    float(bars.close[i]), float(bars.volume[i])]
            for i in range(bars.n)}


@dataclass
class QuoteBoard:
    """The last L1 state per symbol, folded from partial ticks.

    `snapshot()` is the shape the driver primes scheduled fires from:
    `{SYM: {"last": px, "at_ms": epoch_ms_of_the_last_trade}}`. A symbol
    with no usable last is OMITTED rather than published as None or zero —
    `set_holdings` returns None on a price <= 0 (a silently missing order),
    and a zero mark on a held name collapses the portfolio value every
    other symbol is sized against."""

    quotes: dict = field(default_factory=dict)

    def update(self, tick: QuoteTick) -> dict:
        q = self.quotes.setdefault(tick.symbol.upper(), {})
        for name in ("bid", "ask", "last"):
            value = getattr(tick, name)
            if value is not None:
                q[name] = value
        q["at_ms"] = tick.at_ms
        if tick.last is not None:
            q["last_at_ms"] = tick.last_at_ms if tick.last_at_ms is not None else tick.at_ms
        return q

    def get(self, symbol: str) -> Optional[dict]:
        return self.quotes.get(symbol.upper())

    def snapshot(self) -> dict:
        return usable_snapshot(self.quotes)


def usable_snapshot(quotes: dict) -> dict:
    """`{SYM: {"last": px, "at_ms": ...}}` for every symbol on `quotes`
    whose last print is a price something may be sized against.

    The filter is the whole point, and it is one copy because both boards
    in this distribution feed it: `QuoteBoard` above, and the raw dict a
    plugin folds its wire frames into. A symbol is dropped when it has no
    last, when the field is not a number, and when the number is not
    finite or not positive -- `set_holdings` returns None on a price <= 0,
    which is a silently missing order, and a zero mark on a held name
    collapses the portfolio value every other symbol is sized against.

    `at_ms` is when the last TRADE printed, not when the frame arrived: a
    board may carry it as epoch ms (`last_at_ms`) or as an ISO timestamp
    (`last_at`), and a board carrying neither reports None rather than
    the frame's own time."""
    out = {}
    for sym, q in quotes.items():
        last = q.get("last")
        if last is None:
            continue
        try:
            px = float(last)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(px) or px <= 0:
            continue
        at_ms = q.get("last_at_ms")
        if at_ms is None:
            secs = _epoch(q.get("last_at"))
            at_ms = None if secs is None else int(secs * 1000)
        out[sym] = {"last": px, "at_ms": at_ms}
    return out


def say(message: str) -> None:
    """The default `log` for a feed, and flushed.

    A feed's lines are the only account an operator gets of a stream that
    dropped, reconnected, or is refusing to authenticate. A plain `print`
    block-buffers whenever stdout is not a terminal, which is how a feed
    is actually run -- `dqengine live > live.log`, or a pipe -- so those
    lines arrive in 8 KB batches, long after the minute they describe. The
    rest of this package already prints with flush=True."""
    print(message, flush=True)


__all__ = ["MinuteBar", "QuoteTick", "FeedState", "BarStore", "QuoteFeed",
           "BarRefresher", "bar_refresher", "Socket", "QuoteBoard",
           "MinuteZipStore", "bar_is_sane", "in_regular_session",
           "usable_snapshot", "say"]
