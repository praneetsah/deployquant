"""Is the feed actually feeding us?

A stream that dies quietly is worse than one that dies loudly: the worker
keeps its heartbeat, the fleet poll skips it because a worker owns it, and
the strategy simply stops seeing bars. Nothing errors. This module is the
check that turns that into a stated fact.

It is pure: state in, verdict out. Inputs are the connection state, the
symbols the caller actually needs, and the clock. The session comes from
the engine's own calendar, so a half-day ends at 13:00 here exactly as it
does for the bars.

Outside the regular session there is nothing to be silent about — a feed
with no frames at 03:00 is a feed with nothing to send.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from dqengine.runtime.core.data import close_time_ms, is_market_holiday

SESSION_OPEN_S = 9 * 3600 + 30 * 60      # 09:30 ET

OK = "ok"
SILENT = "silent"


def market_is_open(now_et: datetime) -> bool:
    """Holiday- and early-close-aware, on the exchange clock."""
    if now_et.weekday() >= 5 or is_market_holiday(now_et.date()):
        return False
    s = now_et.hour * 3600 + now_et.minute * 60
    return SESSION_OPEN_S <= s <= close_time_ms(now_et.date()) / 1000


def session_open_epoch(now_et: datetime) -> float:
    """Epoch seconds of today's 09:30 ET. The reference a feed that has
    never delivered a frame is measured against — without it, a stream
    that fails to connect at the open reads as 'no news yet' all day."""
    return now_et.replace(hour=9, minute=30, second=0,
                          microsecond=0).timestamp()


@dataclass(frozen=True)
class SilencePolicy:
    """`frame_timeout_s` is the one that matters: any frame — a trade, a
    quote, a bar — proves the socket is alive. 90 s of a moving tape with
    nothing on it is a dead stream, not a quiet one.

    Bars are rarer (one a minute per symbol, and only when the symbol
    trades), so their threshold is separate and looser."""
    frame_timeout_s: float = 90.0
    bar_timeout_s: float = 300.0


@dataclass(frozen=True)
class FeedHealth:
    status: str
    reason: str
    silent_for_s: Optional[float] = None
    missing: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def silent(self) -> bool:
        return self.status == SILENT

    def log_line(self) -> str:
        """The one loud line a worker prints when this turns silent."""
        if self.silent_for_s is None:
            return f"[feed] {self.status}: {self.reason}"
        return f"[feed] {self.status}: {self.reason} ({self.silent_for_s:.0f}s)"


def check_feed(state, symbols: Iterable[str] = (), now_et: Optional[datetime] = None,
               policy: Optional[SilencePolicy] = None,
               now: Optional[float] = None) -> FeedHealth:
    """`ok` or `silent` for `symbols`, which are the ones the caller needs
    bars for — not the ones the feed happens to carry.

    `now_et` is the exchange clock (session rules); `now` is epoch seconds
    (the arithmetic). Passing both keeps the tests honest about which is
    which; `now` defaults to `now_et`'s own timestamp."""
    policy = policy or SilencePolicy()
    now_et = now_et or datetime.now()
    if now is None:
        now = now_et.timestamp()

    if not market_is_open(now_et):
        return FeedHealth(OK, "market closed")
    if not state.connected:
        return FeedHealth(SILENT, f"not connected: {state.error or 'no reason given'}")

    want = {s.upper() for s in symbols}
    have = {s.upper() for s in (state.symbols or ())}
    missing = tuple(sorted(want - have))
    if missing:
        # the socket may be busy with other symbols; these ones get nothing
        return FeedHealth(SILENT, f"not subscribed: {', '.join(missing)}",
                          missing=missing)

    opened = session_open_epoch(now_et)
    frame_age = now - (state.last_frame_at if state.last_frame_at is not None else opened)
    if frame_age > policy.frame_timeout_s:
        return FeedHealth(SILENT, "no frame during the regular session", frame_age)
    bar_age = now - (state.last_bar_at if state.last_bar_at is not None else opened)
    if bar_age > policy.bar_timeout_s:
        return FeedHealth(SILENT, "frames but no bars during the regular session", bar_age)
    return FeedHealth(OK, "receiving", frame_age)


__all__ = ["SilencePolicy", "FeedHealth", "check_feed", "market_is_open",
           "session_open_epoch", "OK", "SILENT"]
