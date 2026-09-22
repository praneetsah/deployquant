"""Running a live feed: where a bar goes, and who is told about it.

A `QuoteFeed` (dqengine.feeds) owns the wire -- the socket, the login, the
subscriptions, the candle layouts, the reconnect. It does not own a
database, a bus, or a loop, and it should not: those are the process's.
This module is that process.

Three things happen around a feed, and all three are here:

  * a whole frame of minute bars is written to `bar_days` in ONE
    transaction (`BarDayStore`), and only then is each bar that changed
    something announced on the bus (`notify_bar`). The order is not a
    style choice: a reader woken by the first publish of a frame pulls
    the bar from the store, and a half-written minute steps its engine
    past the stragglers still in flight;
  * the quote board is republished as `quotes:last` on a short throttle,
    filtered to prices something may be sized against, so a process that
    cannot see this one can still price a wall-clock scheduled fire;
  * the feed's own state is republished as `feed:state` once a second,
    whether the socket is up or down, so a reader that hears nothing can
    tell a quiet market from a dead stream.

`FeedRunner.run()` is the pump. A host that wants more on top -- its own
re-tick, its own quote rules, its own symbol scan -- wraps the two
callbacks and passes a `rescan`; nothing above needs to know about it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dqengine.feeds import bar_is_sane, in_regular_session, usable_snapshot
from dqengine.live.driver.loop import FEED_STATE_KEY

ET = ZoneInfo("America/New_York")

QUOTE_SNAPSHOT_KEY = "quotes:last"
QUOTE_SNAPSHOT_TTL_S = 30
QSNAP_MIN_S = 0.1       # was 0.5: a 15:59:00.000 read must not carry 15:58:59.5

FEED_STATE_TTL_S = 30
FSTATE_MIN_S = 1.0

SYMBOL_RESCAN_S = 15.0


class BarDayStore:
    """The `bar_days` table as the feed's `BarStore`.

    One commit per frame, and only the bars that CHANGED something come
    back: a resent candle identical to what bar_days holds is nothing to
    store and nothing to publish -- a duplicate event is a wasted tick, and
    on the pushed-bars path a bar the engine already holds. Atomicity is the
    point of taking the whole frame: any reader sees the whole minute or
    none of it (2026-08-25 11:42 ET, "bar arrived at or before processed
    frontier").

    `source` is the tag written on a row this store creates, so a reader
    can tell which feed a day came from. It is the feed's name, not the
    store's business, so the host passes it in."""

    def __init__(self, source: str = "stream"):
        self.source = source

    def write(self, bars) -> list:
        from dqengine.live.persistence import BarDay, SessionLocal
        changed = []
        if not bars:
            return changed
        with SessionLocal() as s:
            for bar in bars:
                row = bar.row()
                rec = s.get(BarDay, (bar.symbol, bar.day))
                if rec is None:
                    s.add(BarDay(symbol=bar.symbol, day=bar.day, rows=[row],
                                 source=self.source))
                else:
                    same = next((r for r in rec.rows
                                 if r[0] == bar.start_ms), None)
                    if same is not None and [float(x) for x in same] == row:
                        continue
                    rows = [r for r in rec.rows if r[0] != bar.start_ms]
                    rows.append(row)
                    rows.sort()
                    rec.rows = rows
                    rec.fetched_at = datetime.now(timezone.utc)   # export change signal
                changed.append(bar)
            s.commit()
        return changed


def store_bar(sym: str, epoch_ms: int, o, h, l, c, v,
              notify: bool = True, source: str = "stream") -> bool:
    """Upsert one minute bar into today's BarDay row. True if stored.

    A live feed does not come through here (it hands whole frames to the
    `BarStore` above); this is the single-bar path, for a REST fill-in and
    for tests.

    notify=False defers the bus publish to the caller (`notify_bar`) —
    a whole frame is stored before any of it is published, so a reader
    woken by the first publish never pulls a half-written minute. That
    mid-frame pull is exactly what killed two warm engines at 2026-08-25
    11:42 ET ("bar arrived at or before processed frontier"): the tick
    stepped the union timestamp off the first few symbols, then the rest of
    the frame landed behind the frontier."""
    from dqengine.live.persistence import BarDay, SessionLocal
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(ET)
    ms = (dt.hour * 3600 + dt.minute * 60) * 1000
    if not in_regular_session(dt.date(), ms):
        return False
    if not bar_is_sane(o, h, l, c):
        print(f"[stream] dropped malformed {sym} bar at {ms}: "
              f"o={o} h={h} l={l} c={c} v={v}", flush=True)
        return False
    row = [ms, float(o), float(h), float(l), float(c), float(v)]
    with SessionLocal() as s:
        rec = s.get(BarDay, (sym, dt.date()))
        if rec is None:
            s.add(BarDay(symbol=sym, day=dt.date(), rows=[row],
                         source=source))
        else:
            rows = [r for r in rec.rows if r[0] != ms]
            rows.append(row)
            rows.sort()
            rec.rows = rows
            rec.fetched_at = datetime.now(timezone.utc)   # export change signal
        s.commit()
    if notify:
        notify_bar(sym, dt.date(), ms)
    return True


def notify_bar(sym: str, day, ms: int) -> None:
    """Bus publish AFTER the store write commits: a reader woken by this
    event pulls the bar from the store, so the write must be visible
    first. No bus (or a dead one) degrades to the poll path, and says so
    in the log and in the feed's state -- never silently."""
    from dqengine.live import bus as bus_mod
    if bus_mod.BUS is not None:
        try:
            bus_mod.BUS.publish("bars", {"sym": sym,
                                         "day": day.isoformat(),
                                         "start_ms": ms})
        except Exception as e:
            print(f"[stream] bus publish failed for {sym}: {e!r}",
                  flush=True)


class FeedRunner:
    """The loop around one `QuoteFeed`, and the two keys it publishes.

    `quotes` is the board the feed folds its L1 frames into; pass one to
    share it with a reader in this process (a health endpoint, a UI), or
    leave it out and the feed's own board is used. `status` is the mirror
    of the feed's state, updated in place once per poll so a holder of the
    dict never has to re-read it.

    `bus` defaults to the process bus (`dqengine.live.bus.BUS`), looked up
    on every publish rather than bound here: a process installs its bus
    after importing, and a bus that went away mid-session must stop the
    publish, not the feed."""

    def __init__(self, feed, quotes=None, status=None, bus=None):
        from dqengine.feeds import FeedState
        self.feed = feed
        if quotes is not None:
            feed.quotes = quotes
        self.quotes = quotes if quotes is not None else getattr(feed, "quotes",
                                                                {})
        self.status = status if status is not None else FeedState().as_status()
        self.bus = bus
        self._qsnap = {"at": 0.0}
        self._fstate = {"at": 0.0}
        self._qseen = False

    def _bus(self):
        if self.bus is not None:
            return self.bus
        from dqengine.live import bus as bus_mod
        return bus_mod.BUS

    # ---- the two callbacks, or the open half of a host's own ------------

    def on_bar(self, bar) -> None:
        """One stored minute bar. The store write has already committed
        (the feed stores the whole frame first), so publishing it is safe
        here."""
        notify_bar(bar.symbol, bar.day, bar.start_ms)

    def on_quote(self, tick) -> None:
        """One L1 update, already folded into the board by the feed. The
        snapshot itself is published once per poll rather than once per
        tick: a busy symbol prints many times a second, and every one of
        them would otherwise pay for a serialise and a round trip."""
        self._qseen = True

    # ---- the two published keys -----------------------------------------

    def publish_quote_snapshot(self) -> None:
        """Latest last-trade prices to the bus (key `quotes:last`, json
        {SYM: {"last": px, "at_ms": epoch_ms}}, 30s TTL), at most every
        QSNAP_MIN_S. A process that ticks reads it to price a wall-clock
        scheduled fire -- the quote board itself lives in this process's
        memory and no other process can see it.

        `at_ms` is when the LAST TRADE arrived, not when the frame did (any
        bid/ask tick moves the frame time), so the reader measures the
        price's staleness rather than the quote's; a symbol with no last
        trade is omitted rather than published as None."""
        bus = self._bus()
        if bus is None:
            return
        now = time.time()
        if now - self._qsnap["at"] < QSNAP_MIN_S:
            return
        self._qsnap["at"] = now
        # never let the snapshot take the stream down: this runs inside the
        # frame loop, and a field a venue sends as something other than a
        # number is that symbol's problem, not every live deployment's.
        # usable_snapshot is the one copy of that filter in the tree.
        snap = usable_snapshot(self.quotes)
        if not snap:
            return
        try:
            bus.set_ex(QUOTE_SNAPSHOT_KEY, json.dumps(snap),
                       QUOTE_SNAPSHOT_TTL_S)
        except Exception as e:
            print(f"[stream] quote snapshot publish failed: {e!r}", flush=True)

    def publish_feed_state(self) -> None:
        """The stream's own state to the bus (key `feed:state`, the shape a
        health block reports, 30s TTL), at most once a second.

        A process that owns a deployment's ticks is woken by bar
        notifications; when this stream dies, nothing wakes it. This key is
        how it finds that out: `dqengine.feeds.check_feed` reads it and
        falls back to REST. It is refreshed on a cadence SHORTER than its
        TTL and published whether the socket is up or down, so a dead
        socket says `connected: false` within a second instead of waiting
        for the key to expire."""
        bus = self._bus()
        if bus is None:
            return
        now = time.time()
        if now - self._fstate["at"] < FSTATE_MIN_S:
            return
        self._fstate["at"] = now
        try:
            bus.set_ex(FEED_STATE_KEY, json.dumps(self.status),
                       FEED_STATE_TTL_S)
        except Exception as e:
            print(f"[stream] feed state publish failed: {e!r}", flush=True)

    def refresh_status(self) -> None:
        """The health block readers hold, off the feed's own state."""
        self.status.update(self.feed.state.as_status())

    # ---- the pump --------------------------------------------------------

    def poll_once(self, timeout: float = 1.0) -> None:
        """One turn of the loop: handle at most one wire frame, then
        publish what that frame changed."""
        try:
            self.feed.poll(timeout=timeout)
        except Exception as e:                              # noqa: BLE001
            # the feed records its own trouble in `state` rather than
            # raising; this is the belt for everything else, because a
            # dead thread is a dead stream with nobody to say so
            print(f"[stream] poll failed: {e!r}", flush=True)
            time.sleep(1.0)
        if self._qseen:
            self._qseen = False
            self.publish_quote_snapshot()
        self.refresh_status()
        self.publish_feed_state()

    def run(self, rescan=None, rescan_s: float = SYMBOL_RESCAN_S,
            timeout: float = 1.0, stop=None) -> None:
        """The feed's pump, and the only thread that should touch it.

        Every callback runs on this thread. `rescan()` is called before the
        first poll and then every `rescan_s` seconds: it is where a host
        follows whatever it wants subscribed now, and it is deliberately
        not this module's business.

        `stop` is a callable asked once per turn whether to return. A host
        whose feed thread outlives nothing -- the api, where the process is
        the lifetime -- passes none and the pump runs until the process
        does. A host that has to shut the thread down (a command-line
        install handling a signal, a test) passes an event's `is_set`."""
        last_rescan = 0.0
        while stop is None or not stop():
            now = time.time()
            if rescan is not None and now - last_rescan > rescan_s:
                last_rescan = now
                rescan()
            self.poll_once(timeout=timeout)
