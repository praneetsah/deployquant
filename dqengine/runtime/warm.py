"""WarmPyEngine: a long-lived PyBacktester driven by a live feed.

The python live path replayed the whole script on every tick: ~0.4s pooled
for a young single-symbol deployment, growing ~0.3s per year of age, and
never sub-second. A WarmPyEngine constructs the algorithm ONCE, then steps
each new bar through the SAME _begin_session/_step_bar/_end_session methods
the batch path runs (backtester.py, Task B1), so backtest/live parity is
structural rather than re-derived.

Warm-up drives each stored day exactly as `_run` does -- fast path included,
because a complete stored day is what the fast path is correct for. The
LIVE day is the one place the two drivers differ: `_begin_session` loads
the day's bars once, but on a live day more bars land between calls, so
advance() re-reads the store, verifies the history it already stepped is
unchanged, and steps only the bar-ends that have CLOSED (end <= now) and
were not yet processed. The per-session gates `_begin_session` sets
(`_prev_t`, `_first_bar_done`, `_warm`, `_carried_day`) are never reset by
a reload -- that is the whole reason B1 moved them onto self._.

Correctness posture, inherited from ir_engine/live_engine.py and paid for
one incident at a time there: warm state must never guess.

  * a bar at or before an already-processed end, or a symbol-day whose
    FIRST bar changed since we stepped it, marks the engine `dead`;
  * a symbol that joins mid-session is UNRECOVERABLE here, unlike the IR
    engine: user on_data has already run for slices that did not carry it,
    and there is no way to replay those handlers with it present. Dead,
    rebuild;
  * user code calling quit() marks the engine dead -- a warm engine that
    ignored it would keep trading after the strategy said stop;
  * any exception mid-step marks it dead;
  * a dead engine refuses to snapshot. Dead => no payload => no order, ever.

Pull model: the driver puts closed bars in the store (the feed persists,
then wakes us) and advance() reads today's bars from the store. Pushed
frames from N symbols arriving concurrently would be exactly the ordering
race this design avoids, and a faster feed makes that race likelier.
"""
from __future__ import annotations

import math
import time
from datetime import date
from typing import Optional

from dqengine.runtime.core.data import DataStore, DayBars, day_bars_from_scaled

from .backtester import PyBacktester, RunOverrides, _dt, _parse_date


class WarmPyEngine:
    def __init__(self, code: str, data_root: str | None = None,
                 overrides: dict | None = None, ledger=None,
                 bar_ms: int = 60_000, generated: bool = False, store=None):
        from .algorithm_imports import register_algorithm_imports
        register_algorithm_imports()
        overrides = dict(overrides or {})
        self.dead: Optional[str] = None
        self.bar_ms = int(bar_ms)
        self.open_grace_ms = 120_000
        self.store = store if store is not None else DataStore(data_root)

        ns = {"__name__": "user_algorithm"}
        exec(compile(code, "<algorithm>", "exec"), ns)  # noqa: S102 — sandboxed
        from .algorithm import QCAlgorithm
        algo_cls = next((v for v in ns.values()
                         if isinstance(v, type) and issubclass(v, QCAlgorithm)
                         and v is not QCAlgorithm), None)
        if algo_cls is None:
            raise ValueError("no QCAlgorithm subclass found")
        self._bt = PyBacktester(
            algo_cls(), self.store,
            overrides=RunOverrides(start=_parse_date(overrides.get("start")),
                                   end=_parse_date(overrides.get("end")),
                                   project_calendar=bool(overrides.get("project_calendar")),
                                   cash=overrides.get("cash"),
                                   cash_events=list(overrides.get("cash_events") or [])),
            ledger=ledger, bar_ms=self.bar_ms,
            generated=generated or bool(ns.get("__STRATEGY_LAB_GENERATED__")))
        self._warmed = False
        self._day: Optional[date] = None          # in-progress live session
        self.last_completed: Optional[date] = None
        self._last_end: dict[str, int] = {}       # sym -> last stepped bar end
        self._first_ms: dict[str, int] = {}       # sym -> first bar start at begin
        # ONE frontier for the whole engine, not one per symbol. With
        # per-symbol frontiers a symbol whose bars arrive late (one failed
        # refresh, a streamer retick on a sibling's close) is stepped BEHIND
        # bars other symbols already stepped: algo.time goes backwards,
        # _prev_t moves backwards, and a scheduled event whose window was
        # already crossed fires AGAIN -- a second rebalance. The IR warm
        # engine dies on exactly this (live_engine.py processed_ts); so do we.
        self._frontier: int = -1
        self.bars_stepped = 0

    # ------------------------------------------------------------ state

    @property
    def session_open(self) -> bool:
        """A live session has been begun and not yet ended."""
        return self._day is not None

    def stale(self, reason: str) -> None:
        if not self.dead:
            self.dead = reason


    def _check_quit(self) -> None:
        if getattr(self._bt.algo, "_quit", False):
            self.stale("strategy called quit()")
            raise RuntimeError(self.dead)

    # ------------------------------------------------------------- warm

    def warm(self, through: date) -> float:
        """Run every stored session up to and including `through`, each
        driven exactly as `_run` drives it. Idempotent."""
        if self._warmed:
            return 0.0
        if self.dead:
            raise RuntimeError(self.dead)
        t0 = time.monotonic()
        try:
            for day in self._setup_or_refuse_daily():
                if day > through:
                    break
                self._run_stored_day(day)
                self.last_completed = day
                self._check_quit()
            self._warmed = True
        except Exception as e:                        # noqa: BLE001
            self.stale(f"warm failed: {e!r}")
            raise
        return time.monotonic() - t0

    # A daily session is ONE bar and it lands at the close. advance() cannot
    # step a session's lone first bar until a second one arrives, so a daily
    # strategy on a warm engine would sit here every day holding yesterday's
    # position while its checks never ran. The live path serves the replay
    # for daily deployments; this refusal is the belt to that pair of braces,
    # so a future caller that forgets gets an error rather than a silence.
    DAILY_REFUSAL = ("a daily-resolution strategy cannot run on a warm "
                     "engine: its session is a single bar, which the engine "
                     "cannot step until a second one arrives. Daily "
                     "deployments run on the replay path.")

    def _setup_or_refuse_daily(self):
        bt = self._bt
        try:
            sessions = bt._setup()
        except Exception:
            # _setup raises for a live daily run with no clock, and a warm
            # engine never has one. Name the real reason.
            if getattr(bt, "_daily_mode", False):
                raise RuntimeError(self.DAILY_REFUSAL) from None
            raise
        if bt._daily_mode:
            raise RuntimeError(self.DAILY_REFUSAL)
        return sessions

    def _run_stored_day(self, day: date) -> None:
        """A COMPLETE stored day: identical to one iteration of `_run`,
        fast path and all, because that is the driver a complete day is
        correct under."""
        bt = self._bt
        bt._begin_session(day)
        if not bt._day_bars:
            return
        resume_after = bt._fast_forward(day)
        if resume_after is not None:
            for t in sorted(bt._ends_map):
                bt._step_bar(day, t, bt._ends_map[t])
                if bt.algo._quit:
                    break
        bt._end_session(day)

    # ---------------------------------------------------------- advance

    def advance(self, now_ms_et: int, today: date, bars: dict | None = None) -> bool:
        """Step every bar for `today` that has CLOSED by `now_ms_et` and not
        been processed. True if anything was stepped. Raises (and dies) on
        any anomaly.

        `bars` is the driver PUSHING today's rows instead of the engine
        re-reading today's zip (~200 µs of a ~300 µs step). Per symbol:
        {"first_ms": <start of the day's first bar>, "n_total": <bars in the
        day so far>, "rows": [[ms, o, h, l, c, v], ...]} where rows are
        LEAN-scaled ints (what the zip holds) and carry only the bars the
        driver has not pushed yet. first_ms/n_total are the same integrity
        anchors the store path checks; a push that does not reconcile with
        what the engine holds kills the engine rather than being absorbed.
        None => read the store (tests, in-process use)."""
        if self.dead:
            raise RuntimeError(self.dead)
        if not self._warmed:
            self.stale("advance before warm")
            raise RuntimeError(self.dead)
        try:
            return self._advance(int(now_ms_et), today, bars)
        except Exception as e:
            if not self.dead:
                self.stale(f"exception in advance: {e!r}")
            raise

    def _store_bars(self, day: date) -> dict:
        """Today's bars for every subscribed symbol, from the store, keeping
        only symbol-days with at least two bars: a single bar has no closed
        end to step and its span would default to 60_000 (backtester's
        one-bar assumption), which is wrong at second resolution."""
        out = {}
        for s in self._bt._syms:
            b = self.store.load_minute_day(s, day)
            if b is not None and b.n >= 2:
                out[s] = b
        return out

    def _pushed_bars(self, today: date, bars: dict) -> dict:
        """Today's bars from a driver push, reconciled against what the
        engine already holds. Same output contract as _store_bars: the
        FULL day per symbol, only symbol-days with >= 2 bars."""
        bt = self._bt
        held = bt._day_bars if self._day is not None else {}
        out = {}
        for s in bt._syms:
            p = bars.get(s)
            if p is None:
                if s in held:
                    out[s] = held[s]          # nothing new for it this tick
                continue
            n_total = int(p["n_total"])
            delta = day_bars_from_scaled(today, p.get("rows") or [])
            old = held.get(s)
            if old is None:
                # first push for this symbol-day must be the WHOLE day
                if (delta.n if delta else 0) != n_total:
                    self.stale(f"push out of sync for {s}: {delta.n if delta else 0} "
                               f"rows pushed but the day holds {n_total} and "
                               f"the engine holds none")
                    raise RuntimeError(self.dead)
                if delta is not None and delta.n >= 2:
                    out[s] = delta
                continue
            if int(p["first_ms"]) != int(old.start_ms[0]):
                self.stale(f"history changed under {s}: first bar "
                           f"{int(old.start_ms[0])} -> {int(p['first_ms'])}")
                raise RuntimeError(self.dead)
            add = delta.n if delta else 0
            if old.n + add != n_total:
                self.stale(f"push out of sync for {s}: engine holds {old.n}, "
                           f"pushed {add}, day holds {n_total}")
                raise RuntimeError(self.dead)
            if add == 0:
                out[s] = old
                continue
            if int(delta.start_ms[0]) <= int(old.start_ms[-1]):
                self.stale(f"pushed bar for {s} at {int(delta.start_ms[0])} is "
                           f"not after the last held bar {int(old.start_ms[-1])}")
                raise RuntimeError(self.dead)
            import numpy as np
            out[s] = DayBars(
                day=today,
                start_ms=np.concatenate([old.start_ms, delta.start_ms]),
                open=np.concatenate([old.open, delta.open]),
                high=np.concatenate([old.high, delta.high]),
                low=np.concatenate([old.low, delta.low]),
                close=np.concatenate([old.close, delta.close]),
                volume=np.concatenate([old.volume, delta.volume]))
        return out

    def _advance(self, now_ms_et: int, today: date, bars: dict | None = None) -> bool:
        bt = self._bt
        self._check_quit()
        if self.last_completed is not None and today <= self.last_completed:
            self.stale(f"advance for {today} but {self.last_completed} is "
                       f"already completed")
            raise RuntimeError(self.dead)
        if self._day is not None and self._day != today:
            self.stale(f"session {self._day} rolled without end_session")
            raise RuntimeError(self.dead)

        fresh = (self._pushed_bars(today, bars) if bars is not None
                 else self._store_bars(today))
        if not fresh:
            return False                            # nothing closed yet

        if self._day is None:
            # Do not open the session until every subscribed symbol has bars,
            # for a grace period after the open: a symbol whose first bars
            # land a minute late (one slow refresh among 29) would otherwise
            # be "joined mid-session" on the next call -- unrecoverable for
            # user code, so a rebuild every morning on a wide universe.
            from dqengine.runtime.core.data import REG_OPEN_MS
            missing = set(bt._syms) - set(fresh)
            if missing and now_ms_et < REG_OPEN_MS + self.open_grace_ms:
                return False
            # first bars of the live day: open the session the way the batch
            # driver does, then take the LIVE timeline path (no fast path --
            # it reads "last bar" from what is available, wrong on a partial
            # day)
            # pushed bars seed the session directly: the store is never
            # consulted on the pushed path, so a zip write that failed on
            # the driver cannot leave the engine "healthy" and never opening
            bt._begin_session(today, preloaded=fresh if bars is not None else None)
            if not bt._day_bars or bt._carried_day:
                if bars is not None:
                    self.stale("pushed bars but the session opened with no data")
                    raise RuntimeError(self.dead)
                # begin saw no real data (a race with the feed): do not
                # start a fill-forward session on a live day; try again
                # next call
                bt._day_bars = {}
                return False
            bt._ends_map = {}
            bt._prev_t = -1
            self._day = today
            self._last_end = {}
            self._first_ms = {s: int(b.start_ms[0])
                              for s, b in bt._day_bars.items()}
        else:
            joined = set(fresh) - set(bt._day_bars)
            if joined:
                self.stale(f"symbols joined mid-session: {sorted(joined)}")
                raise RuntimeError(self.dead)
            for s, b in fresh.items():
                old = bt._day_bars[s]
                if int(b.start_ms[0]) != self._first_ms[s]:
                    self.stale(f"history changed under {s}: first bar "
                               f"{self._first_ms[s]} -> {int(b.start_ms[0])}")
                    raise RuntimeError(self.dead)
                if b.n < old.n:
                    self.stale(f"history shrank under {s}: {old.n} -> {b.n}")
                    raise RuntimeError(self.dead)
                bt._day_bars[s] = b                 # extend, gates untouched

        # the merged timeline of CLOSED, UNPROCESSED bar-ends
        ends: dict[int, list] = {}
        for s, b in bt._day_bars.items():
            span = bt._spans.get(s)
            if span is None:
                # a late joiner takes the session's span (never its own
                # first-two-bar gap -- see PyBacktester._session_span)
                span = min(bt._spans.values()) if bt._spans else self.bar_ms
                bt._spans[s] = span
            done = self._last_end.get(s, -1)
            for i in range(b.n):
                t = int(b.start_ms[i]) + span
                if t > now_ms_et:
                    break                           # not closed yet
                if t <= done:
                    continue
                if t <= self._frontier:
                    # a bar for this symbol that closed BEFORE bars we have
                    # already stepped for other symbols. Stepping it would
                    # run the timeline backwards. Never absorbed.
                    self.stale(f"{s} bar ending {t} arrived behind the "
                               f"frontier {self._frontier}")
                    raise RuntimeError(self.dead)
                ends.setdefault(t, []).append((s, i))
        stepped = False
        for t in sorted(ends):
            entries = ends[t]
            bt._step_bar(today, t, entries)
            for s, _ in entries:
                self._last_end[s] = t
            self._frontier = t
            self.bars_stepped += len(entries)
            stepped = True
            if bt.algo._quit:
                self.stale("strategy called quit()")
                raise RuntimeError(self.dead)
        return stepped

    # ------------------------------------------------------------- prime

    def _due_events(self, now_ms_et: int) -> list:
        """(fire_ms, ev) for every scheduled event the clock has reached
        that neither a bar nor an earlier prime has run, in fire order."""
        bt = self._bt
        floor = max(bt._prev_t, bt._primed_through)
        return sorted(((f, ev) for f, ev in bt._day_events
                       if floor < f <= now_ms_et), key=lambda x: x[0])

    def next_fire_ms(self) -> Optional[int]:
        """The earliest scheduled fire still ahead of both cursors, or None
        when no session is open. The engine tells the driver when it next
        needs to be woken; the driver never re-derives the schedule."""
        if self._day is None or self.dead:
            return None
        bt = self._bt
        floor = max(bt._prev_t, bt._primed_through)
        ahead = [f for f, _ in bt._day_events if f > floor]
        return min(ahead) if ahead else None

    def prime(self, now_ms_et: int, today: date, prices: dict) -> Optional[dict]:
        """Wall-clock schedule fire (spec 2026-09-18). It is `now_ms_et`;
        `prices` is {SYM: {"last": px, "at_ms": epoch_ms}} from whatever
        realtime feed the driver has. Run every scheduled callback the clock
        has reached that no bar has run yet, pricing Security.price and
        algo._prices (LEAN's Security.Price -- what set_holdings and
        market_order size against) from `prices`. Nothing else moves: the
        official bar, when it lands, feeds indicators, runs on_data and
        evaluates fills exactly as today and skips these events.

        None when there is nothing due or no session is open -- a pure
        no-op. A symbol missing from `prices` keeps the last stepped close,
        which is what its candle would carry if it has not printed."""
        if self.dead:
            raise RuntimeError(self.dead)
        if not self._warmed or self._day != today:
            return None
        due = self._due_events(int(now_ms_et))
        if not due:
            return None
        bt = self._bt
        algo = bt.algo
        priced, unpriced = 0, []
        usable: dict = {}
        for s in bt._syms:
            p = (prices or {}).get(s)
            last = p.get("last") if isinstance(p, dict) else None
            # a usable quote is a finite POSITIVE number. Zero passes every
            # finiteness check and is the worst value here: set_holdings
            # returns None on price <= 0 (no order, no exception), the event
            # would be marked fired and the bar would never retry it -- a
            # silent missing order; and a zero mark on a held name collapses
            # total_portfolio_value and mis-sizes every other symbol. Such a
            # symbol keeps its last stepped close instead.
            px = (float(last) if isinstance(last, (int, float))
                  and not isinstance(last, bool) else None)
            if px is None or not math.isfinite(px) or px <= 0:
                unpriced.append(s)
                continue
            usable[s] = px
        # validated BEFORE any write: a bad entry can never half-write
        for s, px in usable.items():
            algo.securities[s].price = px
            algo._prices[s] = px
            priced += 1
        try:
            algo._market_open = True
            fired = []
            for fire_ms, ev in due:
                # Both clocks: algo.time is what user code reads; bt._ms is
                # what the order book stamps orders with (backtester.py:181,
                # clock=lambda: (self._day, self._ms)). An order placed here
                # must carry (today, fire_ms) -- the stamp the batch walk
                # gives it -- or resting pass 2's exclude_created would not
                # defer it and the model fill lands one bar early.
                bt._ms = int(fire_ms)
                algo.time = _dt(today, fire_ms)
                ev.callback()
                self._check_quit()
                fired.append({"name": ev.name, "fire_ms": int(fire_ms)})
        except Exception as e:
            if not self.dead:
                self.stale(f"exception in prime: {e!r}")
            raise
        bt._primed_through = max(bt._primed_through, int(now_ms_et))
        return {"fired": fired, "priced": priced, "unpriced": unpriced}

    # ------------------------------------------------------------- roll

    def end_session(self, today: date) -> None:
        if self.dead:
            raise RuntimeError(self.dead)
        if self._day != today:
            self.stale(f"end_session({today}) but the open day is {self._day}")
            raise RuntimeError(self.dead)
        try:
            self._bt._end_session(today)
        except Exception as e:
            self.stale(f"exception in end_session: {e!r}")
            raise
        self._day = None
        self.last_completed = today
        self._last_end = {}
        self._first_ms = {}
        self._frontier = -1

    # --------------------------------------------------------- snapshot

    def pushed_state(self) -> dict:
        """{sym: start_ms of the last bar the engine holds for the open
        session}. The driver pushes only bars after these; {} when no
        session is open, which tells the driver to push the whole day."""
        if self._day is None:
            return {}
        return {s: int(b.start_ms[-1]) for s, b in self._bt._day_bars.items()
                if b.n}

    def snapshot(self, since: dict | None = None) -> dict:
        """The engine's result dict -- exactly what run_python_backtest
        returns -- so the driver builds its payload through the same code
        the replay path uses. Dead => refuses: a payload from a dead engine
        is an instruction to the executor built on numbers nobody trusts.

        `since` (see PyBacktester._result) asks for only what grew since
        the driver's last snapshot; the driver merges."""
        if self.dead:
            raise RuntimeError(f"engine dead: {self.dead}")
        return self._bt._result(since=since)
