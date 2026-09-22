"""The per-deployment worker loop: one deployment, one process, woken by bar
notifications on the bus.

The ENTIRE warm path -- the warm registry, the same-tick replay fallback, the
nightly audit -- runs inside this process, with its own tick lock and its own
warm engine. That is the isolation: a 30-second pathological strategy stalls
only its own process, never a neighbour. Measured on the shared lock this
replaced, one strategy's tick queued behind another's for whole seconds in
the close window.

Exit contract with whatever supervises the process:
  exit 0 -- deployment gone/stopped/paused: nothing to do, do not restart
  exit 1 -- crash: restart with backoff
  exit 2 -- no bus configured: never spawned in that state, but be explicit
  exit 3 -- a tick wedged past STUCK_TICK_S (the heartbeat thread's own exit)

The loop never talks to the broker. It publishes a notification and the
executor side does the account reconcile -- one broker writer per connection.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dqengine.feeds.base import FeedState
from dqengine.feeds.health import (OK, SILENT, FeedHealth, check_feed,
                                   market_is_open)
from dqengine.runtime.core.data import SessionCalendar

from dqengine.live.bus import Bus, bus_from_env
from dqengine.live.driver import engine, ports

ET = ZoneInfo("America/New_York")
HB_EX_S = 120          # heartbeat TTL; supervisor treats missing as stale
HB_BEAT_S = 15         # the heartbeat THREAD's cadence (independent of ticks)
# A tick that has run longer than this is wedged (a hung engine build, a
# stuck broker call): the heartbeat thread stops the process so the
# supervisor restarts it and the poll loop covers meanwhile. Before this,
# a legitimate 120 s first-of-day build let the TTL lapse mid-tick, the
# poll loop took the deployment over, and two drivers stepped one engine.
STUCK_TICK_S = 600

# Pre-open warm window (ET, ms since midnight): one extra tick between
# 09:00 and the 09:30 open rebuilds any engine that died overnight, so an
# open-triggered signal never pays the ~9s warm-up inside its first live
# tick. Against a warm engine the extra tick costs milliseconds.
PRE_OPEN_WARM_MS = 9 * 3600 * 1000
SESSION_OPEN_MS = (9 * 3600 + 30 * 60) * 1000

# Post-notification grace before ticking (see run_once): lets the rest of
# a split minute land so one tick sees the whole minute. Keeps bar-close ->
# order comfortably sub-second.
GRACE_S = 0.3

# Close-window self-clocking (2026-08-26 latency work): in the final two
# minutes the loop polls the bus with a short block so the wall-clock
# before-close fire lands within ~0.2s of close-60s, the grace drain is
# skipped (the decision needs no straggler bars), and one forced tick per
# day fires the quote-primed batch even if no bar event wakes us. Since
# 2026-09-18 the same precision wake targets every scheduled fire the
# engine reports (the warm engine's next_fire_ms); the close-window path
# remains for deployments without a warm engine.
CLOSE_TIGHT_FROM_S = 120       # close-120s: start the tight loop
CLOSE_TIGHT_BLOCK_MS = 200

# ---- the feed this worker is fed by --------------------------------------
# This process is woken by bar notifications and by nothing else. When the
# feed dies, no bar events arrive, the fleet poll skips this deployment
# (its worker is alive and owns it), and the strategy simply stops trading
# while every heartbeat says it is fine. So the worker watches the feed: the
# process that owns the stream publishes its state under FEED_STATE_KEY, and
# `dqengine.feeds.check_feed` turns that into ok or silent.
FEED_STATE_KEY = "feed:state"
# The key carries a TTL of its own (30 s where it is published), refreshed
# about once a second. An ABSENT key is therefore not silence on its own: a
# worker can start during a rolling deploy, ahead of the process that
# publishes it, and a redis hiccup reads the same way. It becomes silence
# when nothing has been published for longer than this -- three times the
# key's own lifetime, measured from the moment the absence was first
# noticed (so a fresh worker gets the full window). Outside the session
# there is nothing to notice.
FEED_ABSENT_GRACE_S = 90.0
# How often the REST fallback runs while the feed stays silent. The bars are
# a minute apart; the call is the same per-symbol refresh the fleet poll
# makes, and hammering a vendor is how a degraded feed becomes a rate-limited
# one.
FEED_REFRESH_EVERY_S = 60.0


def _refresh_source():
    """What the silence fall-back would ask of the installed bar source, or
    None when it has nothing to ask.

    A bar source that does not answer the question is taken to have a
    refresh: that is what all of them had before the question existed, and
    the driver's own fakes and a host's own implementation are both
    entitled to stay as they are."""
    fn = getattr(ports.bars(), "refresh_source", None)
    return fn() if callable(fn) else "REST"


class BusIntentSink:
    """Tell the executor that a tick may have moved money.

    Two notifications, on the bus and nowhere else -- the driver never talks
    to a broker. `intents` is unfloored: one per acting tick, the fast lane,
    which the consumer dedupes by seq and by connection. `sync` carries the
    per-loop floor (5s on second resolution) so a 1/s tick cadence cannot
    hammer the broker sweep; the connection-level floor is the backstop.

    Publishing is the last thing a tick owes anyone: the zips it owes are
    written after (see the warm engine's defer_exports_for)."""

    def __init__(self, bus: Bus, sync_floor_s: float = 0.0):
        self.bus = bus
        self.sync_floor_s = sync_floor_s
        self._last_sync_pub = 0.0

    def acted(self, dep_id: str, conn_id: str, seq: str) -> None:
        self.bus.publish("intents",
                         {"dep": dep_id, "conn": conn_id, "seq": seq})
        if time.time() - self._last_sync_pub >= self.sync_floor_s:
            self._last_sync_pub = time.time()
            self.bus.publish("sync", {"conn": conn_id})


class WorkerLoop:
    def __init__(self, dep_id: str, bus: Bus, tick=None):
        self.dep_id = dep_id
        self.bus = bus
        if tick is None:
            from dqengine.live.driver import tick as tick_mod
            tick = tick_mod.tick_deployment
        self.tick = tick
        self.group = f"dep:{dep_id}"
        self._syms: set | None = None      # cached; IR is a deploy snapshot
        self._conn_id = None
        self._rolled_day = None
        self._prewarm_day = None
        self._close_fire_day = None
        # DAILY deployments only, resolved with the symbols on the first
        # cycle. False keeps open_fire_due permanently off, which is every
        # minute and second deployment — their run_once decisions are the
        # ones that shipped.
        self._daily = False
        self._open_fire_day = None
        # wall-clock schedule fire (spec 2026-09-18): the fire_ms of the
        # last forced tick, so one fire is forced once even if the engine's
        # next_fire_ms has not moved past it yet
        self._fired_ms: int | None = None
        # publish first, export after (2026-09-18 15:59:02 incident): this
        # process owns one deployment, so the per-symbol zip export the tick
        # used to run BEFORE returning is deferred and flushed by run_once
        # once the intent is on the bus
        engine.defer_exports_for(dep_id)
        # resolved with the IR on first cycle (second-resolution spec §7)
        self._stream = "bars"
        self._grace = GRACE_S
        # who is told this deployment acted, and on what floor
        self._sink = ports.intent_sink(bus)
        self._group_ready = False
        self._tick_started: float | None = None
        self._hb_thread = None
        # feed watch: the last verdict (so the line is printed per
        # TRANSITION, not per cycle), when the state key was first missed,
        # and when the REST fallback last ran
        self._feed_silent = False
        self._feed_absent_since: float | None = None
        self._feed_refreshed_at = 0.0

    def _minute_complete(self, events) -> bool:
        """True when this batch already carries the NEWEST minute's bar for
        every subscribed symbol -- the case the grace drain exists for
        cannot occur, so waiting buys nothing. Conservative on anything it
        cannot see: an event without a start_ms, or an empty symbol set,
        keeps the drain."""
        if not self._syms:
            return False
        newest = -1
        by_ms: dict = {}
        for _, f in events:
            sym = (f.get("sym") or "").upper()
            if sym not in self._syms:
                continue
            try:
                ms = int(f.get("start_ms"))
            except (TypeError, ValueError):
                return False
            newest = max(newest, ms)
            by_ms.setdefault(ms, set()).add(sym)
        return newest >= 0 and self._syms <= by_ms.get(newest, set())

    # ---- is the feed still feeding us? ----

    def _feed_health(self, now_et: datetime, now: float) -> FeedHealth:
        """The feed's own state, off the bus, as a verdict for THIS
        deployment's symbols. A state nobody published, or one that cannot
        be read, is 'nothing heard' -- which becomes silence once the
        absence outlives FEED_ABSENT_GRACE_S."""
        try:
            raw = self.bus.get(FEED_STATE_KEY)
        except Exception:                                  # noqa: BLE001
            raw = None          # a bus this worker cannot read is a feed it
                                # cannot hear either: the same fallback
        if raw:
            self._feed_absent_since = None
            try:
                state = FeedState.from_status(json.loads(raw))
            except (TypeError, ValueError):
                state = FeedState()
            return check_feed(state, self._syms or (), now_et, now=now)
        if self._feed_absent_since is None:
            self._feed_absent_since = now
        absent_for = now - self._feed_absent_since
        if absent_for > FEED_ABSENT_GRACE_S:
            return FeedHealth(SILENT, "no feed state on the bus", absent_for)
        return FeedHealth(OK, "waiting for the feed's first state")

    def _feed_bars(self, now_et: datetime) -> bool:
        """Watch the feed, and while it is silent fetch this deployment's
        bars over REST -- the same per-symbol refresh the fleet poll makes,
        into the same store the stream writes to.

        Returns True when a refresh actually wrote something, which makes
        the cycle relevant: the bars are in the store by then, so the tick
        still runs with refresh_bars=False like every other path.

        Never raises. A refresh that fails is one line and the next
        window; a fallback that took the worker down would turn a degraded
        feed into a stopped strategy."""
        now = now_et.timestamp()
        if not market_is_open(now_et):
            # nothing to be silent about out of hours, and tomorrow's first
            # silence has to announce itself again
            self._feed_silent = False
            self._feed_absent_since = None
            return False
        health = self._feed_health(now_et, now)
        if not health.silent:
            if self._feed_silent:
                print(f"[worker] {self.dep_id}: feed back ({health.reason}); "
                      f"bar notifications again", flush=True)
            self._feed_silent = False
            return False
        source = _refresh_source()
        if not self._feed_silent:
            self._feed_silent = True
            where = (f"falling back to {source} for {sorted(self._syms or ())}"
                     if source else
                     "and this install has no REST fall-back: bars resume "
                     "when the stream does")
            print(f"[worker] {self.dep_id}: {health.log_line()} -- {where}",
                  flush=True)
        if source is None:
            # nothing to ask. Saying it once above beats failing once per
            # symbol every window for as long as the stream is down.
            return False
        if now - self._feed_refreshed_at < FEED_REFRESH_EVERY_S:
            return False
        self._feed_refreshed_at = now
        wrote = False
        for sym in sorted(self._syms or ()):
            try:
                wrote = bool(ports.bars().refresh(sym)) or wrote
            except Exception as e:                         # noqa: BLE001
                print(f"[worker] {self.dep_id}: REST refresh of {sym} "
                      f"failed: {e!r}", flush=True)
        return wrote

    # ---- one cycle, injectable clock for tests ----

    def run_once(self, now_et: datetime | None = None) -> str:
        """-> 'exit' | 'ticked' | 'idle'."""
        injected = now_et is not None
        now_et = now_et or datetime.now(ET)
        self.bus.set_ex(f"worker:hb:{self.dep_id}", now_et.isoformat(),
                        HB_EX_S)
        with ports.store().open(self.dep_id) as tx:
            dep = tx.dep
            if dep is None or dep.status == "stopped" or dep.paused_at:
                return "exit"
            if self._syms is None:
                # every symbol the tick can touch, for EITHER kind: a python
                # deployment carries ir=None (collect_ir_symbols crashed on
                # it, so python workers died on their first cycle and were
                # served by the 60 s poll); and a two-view strategy's IR is
                # not what its code add_equity's. _tick_symbols owns that.
                from dqengine.live.driver.deployment import _dep_resolution
                self._syms = {x.upper() for x in engine._tick_symbols(dep)}
                self._conn_id = dep.broker_connection_id
                res = _dep_resolution(dep) or "minute"
                if res == "second":
                    # second bars arrive on their own stream; frames are
                    # atomic by construction so the grace drain shrinks,
                    # and sync publishes are floored so a 1/s tick cadence
                    # cannot hammer the broker sweep (the connection-level
                    # floor + close-window override remain the backstop)
                    self._stream = "bars1s"
                    self._grace = 0.05
                    self._sink.sync_floor_s = 5.0
                self._daily = res == "daily"

        pre_ms = ((now_et.hour * 3600 + now_et.minute * 60
                   + now_et.second) * 1000 + now_et.microsecond // 1000)
        from dqengine.runtime.core.data import is_market_holiday as _hol
        close_ms = (SessionCalendar.close_time_ms(now_et.date())
                    if now_et.weekday() < 5 and not _hol(now_et.date()) else None)
        in_close_window = (close_ms is not None
                           and close_ms - CLOSE_TIGHT_FROM_S * 1000
                           <= pre_ms < close_ms)

        if not self._group_ready:
            self.bus.ensure_group(self._stream, self.group)
            self._group_ready = True
        # PRECISION WAKE: sleep until the next moment the engine needs us --
        # its next scheduled fire (wall-clock schedule fire, spec
        # 2026-09-18), or the close-60s forced fire -- instead of polling.
        # The fire lands within ~10ms of its boundary rather than anywhere
        # in a 1s poll slot.
        next_fire = engine.next_fire_ms(self.dep_id)
        block = 1000
        if next_fire is not None and next_fire != self._fired_ms:
            until_fire = next_fire - pre_ms
            if until_fire > 0:
                block = max(10, min(block, until_fire))
        if in_close_window:
            until_fire = (close_ms - 60000) - pre_ms
            block = (max(10, min(CLOSE_TIGHT_BLOCK_MS, until_fire, block))
                     if until_fire > 0 else min(CLOSE_TIGHT_BLOCK_MS, block))
        events = self.bus.read(self._stream, self.group, "w",
                               block_ms=block)
        if (in_close_window or next_fire is not None) and not injected:
            # the blocking read consumed real time; the fire condition
            # must see the post-block clock or it waits an extra cycle
            now_et = datetime.now(ET)
        relevant = any((f.get("sym") or "").upper() in self._syms
                       for _, f in events)
        if relevant and not in_close_window and not self._minute_complete(events):
            # grace drain: the streamer publishes a minute's bars as a
            # batch, but a feed can split one minute across frames. Give
            # the rest of the minute a beat to land and fold it into THIS
            # tick -- a tick that steps the union timestamp off a partial
            # minute makes the stragglers arrive behind the processed
            # frontier and kills the warm engine (2026-08-25 11:42 ET).
            # ADAPTIVE (2026-09-06): skipped when every subscribed symbol's
            # bar for the newest minute is already in this batch -- there
            # is nothing left to wait for, and on a single-symbol
            # deployment the sleep was 300 ms of pure latency per bar.
            time.sleep(self._grace)
            events += self.bus.read(self._stream, self.group, "w",
                                    block_ms=50)
        # a bar event is not the only way this deployment can owe a tick:
        # when the feed has gone silent the bars come over REST instead, and
        # the ones that land are as relevant as the ones that arrive pushed
        if self._feed_bars(now_et):
            relevant = True

        today = now_et.date()
        now_ms = (now_et.hour * 3600 + now_et.minute * 60
                  + now_et.second) * 1000
        from dqengine.runtime.core.data import is_market_holiday
        session_day = now_et.weekday() < 5 and not is_market_holiday(today)
        roll_due = (session_day
                    and now_ms >= SessionCalendar.close_time_ms(today) + 60000
                    and self._rolled_day != today)
        prewarm_due = (session_day
                       and PRE_OPEN_WARM_MS <= now_ms < SESSION_OPEN_MS
                       and self._prewarm_day != today)
        # the quote-primed before-close fire must not depend on a bar event
        # arriving: force one tick at close-60s
        close_fire_due = (close_ms is not None
                          and close_ms - 60000 <= now_ms < close_ms
                          and self._close_fire_day != today)
        # the engine's next scheduled fire has arrived and no bar woke us:
        # force the tick so the engine primes it off live prices
        fire_ms_now = ((now_et.hour * 3600 + now_et.minute * 60
                        + now_et.second) * 1000 + now_et.microsecond // 1000)
        fire_due = (next_fire is not None and fire_ms_now >= next_fire
                    and self._fired_ms != next_fire)
        # DAILY ONLY. A market-on-open order the strategy placed yesterday
        # is published to the executor from 09:31 (the engine's daily
        # preview) and reaches the broker on the next sweep. That must not
        # depend on a bar event arriving: on a dead stream tick_all skips a
        # worker-owned deployment, so nothing else would tick the morning
        # and the order would wait until the close. One forced tick a day.
        # Off for every other resolution: self._daily is False there and
        # this reads exactly as it did before.
        open_fire_due = (self._daily and session_day
                         and now_ms >= engine.OPEN_PREVIEW_MS
                         and self._open_fire_day != today)

        ticked = False
        if (relevant or roll_due or prewarm_due or close_fire_due or fire_due
                or open_fire_due):
            # refresh_bars=False: the bar is already in the store either
            # way -- the feed writes it before publishing the notification,
            # and the REST fallback above writes it before saying so. The
            # fleet poll is NOT a safety net for this deployment: it skips
            # worker-owned rows, which is why _feed_bars exists.
            self._tick_started = time.time()
            t_tick0 = time.monotonic()
            try:
                self.tick(self.dep_id, refresh_bars=False)
            finally:
                self._tick_started = None
            tick_ms = round((time.monotonic() - t_tick0) * 1000)
            ticked = True
            # a bar event that lands at/after the fire time ticks the fire
            # too (the engine primes it inside that same tick): record it
            # so the next silent cycle does not force a second one
            if fire_due or (relevant and next_fire is not None
                            and fire_ms_now >= next_fire):
                self._fired_ms = next_fire
            if roll_due:
                self._rolled_day = today
                # _fired_ms is ms-since-midnight and this process lives
                # across days: a single-rule strategy reports the same
                # next_fire_ms tomorrow, and without this a silent bus
                # would never force tomorrow's fire
                self._fired_ms = None
            if prewarm_due:
                self._prewarm_day = today
            if close_fire_due:
                self._close_fire_day = today
            if open_fire_due:
                self._open_fire_day = today
            # pre-warm alone moves no money: no bar stepped, no new signal
            # -- publishing a sync for it would only wake the broker sweep
            # for nothing every morning
            if (relevant or roll_due or close_fire_due or fire_due
                    or open_fire_due) and self._conn_id:
                self._sink.acted(self.dep_id, self._conn_id,
                                 str(int(time.time() * 1000)))
            if fire_due or close_fire_due:
                # one line per wall-clock fire so the close decomposes itself:
                # how late the wake was, how long the tick held the order
                # (2026-09-18: fire at .1 s, venue at 2 s, nothing in between
                # was measured)
                late_ms = (fire_ms_now - next_fire) if next_fire is not None else None
                print(f"[worker] fire dep={self.dep_id} fire_ms={next_fire} "
                      f"wake_late_ms={late_ms} tick_ms={tick_ms} "
                      f"published={bool(self._conn_id)}", flush=True)
            # the intent is on the bus: NOW write the zips the tick owed
            # (see the warm engine's defer_exports_for) -- never before
            engine.flush_deferred_export(self.dep_id)
        if events:
            self.bus.ack(self._stream, self.group, *[i for i, _ in events])
        return "ticked" if ticked else "idle"

    def _heartbeat_loop(self) -> None:
        """Beats ONLY while a tick is running: the main loop beats between
        ticks (run_once), so a hang outside a tick still lapses the TTL and
        the poll loop takes over -- this thread must not turn that into a
        silent freeze. A tick past STUCK_TICK_S stops the process."""
        while True:
            started = self._tick_started
            if started is not None:
                if time.time() - started > STUCK_TICK_S:
                    print(f"[worker] {self.dep_id}: tick running for "
                          f"{time.time() - started:.0f}s -- wedged; exiting so "
                          f"the supervisor restarts and the poll loop covers",
                          flush=True)
                    os._exit(3)
                try:
                    self.bus.set_ex(f"worker:hb:{self.dep_id}",
                                    datetime.now(ET).isoformat(), HB_EX_S)
                except Exception:                          # noqa: BLE001
                    pass
            time.sleep(HB_BEAT_S)

    def start_heartbeat(self) -> None:
        import threading
        if self._hb_thread is None:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                               name=f"hb-{self.dep_id}",
                                               daemon=True)
            self._hb_thread.start()

    def run_forever(self) -> int:
        self.bus.ensure_group("bars", self.group)
        # the heartbeat lives on its own thread: a long tick (the first
        # engine build of the day) must not read as a dead worker
        self.start_heartbeat()
        # warm at spawn: deploys and restarts happen at arbitrary times, and
        # a lazily-built engine would otherwise pay its ~9s warm-up inside
        # the first live tick of the next session. tick_deployment no-ops
        # for stopped/paused deployments, so this is safe unconditionally.
        self.tick(self.dep_id, refresh_bars=False)
        while True:
            state = self.run_once()
            if state == "exit":
                print(f"[worker] {self.dep_id}: deployment inactive, "
                      f"exiting cleanly", flush=True)
                return 0


def run(dep_id: str, bus: Bus | None = None) -> int:
    """One deployment's worker, from a bare process to the exit code its
    supervisor reads. The ports must already be installed: this builds the
    bus, publishes it as the module singleton the stale-marker path reads,
    and loops.

    `bus` left out is the supervised worker process, which owns nothing but
    this loop and reads REDIS_URL for itself. A process that already holds a
    bus -- one that also runs the feed and the consumers -- passes it, so
    the whole process talks over one connection."""
    bus = bus if bus is not None else bus_from_env()
    if bus is None:
        print("[worker] REDIS_URL not set; refusing to start", flush=True)
        return 2
    # the warm+enforce machinery (mark_warm_stale, and the stale-marker check
    # in the warm tick) reads the module singleton; this process must see the
    # same bus the loop uses
    from dqengine.live import bus as bus_mod
    bus_mod.BUS = bus
    loop = WorkerLoop(dep_id, bus)
    try:
        return loop.run_forever()
    except Exception:
        import traceback
        print(f"[worker] {dep_id} crashed:\n"
              f"{traceback.format_exc()[-1500:]}", flush=True)
        return 1
