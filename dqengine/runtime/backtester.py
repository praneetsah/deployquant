"""PyBacktester — the event loop that drives a QCAlgorithm.

Mirrors ir_engine.engine.Backtester's bar walk (same stores, same calendar,
same Sleeve, same fill rules via OrderBook) but instead of evaluating IR
rules it calls user hooks. Per bar-end t, the order is:

    1. securities' prices <- bars ending at t (close)
    2. resting orders checked against those bars
    3. on_data(slice) with algo.time = t
    4. scheduled events with fire_ms in (prev_t, t]   (data first, then
       events: a before_market_close(1) handler sees the bar ending at
       close-60s — the IR engine's before_close decision anchor)

Warm-up sessions stream data with is_warming_up=True, refuse orders, fire
no scheduled events, and mark no equity.
"""
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from dqengine.runtime.core.data import DayBars, REG_OPEN_MS, SessionCalendar, close_time_ms
from dqengine.runtime.core import stats_from_equity
from dqengine.runtime.core.portfolio import Sleeve

from . import fastpath
from .algorithm import QCAlgorithm, resolve_hook
from .bars import Bars, Slice, TradeBar
from .enums import Resolution
from .orders import OrderBook, Transactions
from .portfolio_view import PortfolioManager


@dataclass
class RunOverrides:
    start: date | None = None
    end: date | None = None
    cash: float | None = None
    # live only (the IR engine's cfg.project_calendar): extend the calendar
    # past the data horizon with scheduled sessions so week-boundary rules
    # see the week's real shape. Off for backtests, whose window ends on a
    # data day and whose results are pinned against LEAN.
    project_calendar: bool = False
    # live only (the IR engine's cfg.cash_events): [(iso_day, amount)]
    # external cash landing at that session's open; a non-session day rolls
    # forward to the next session, exactly as the IR engine applies it
    cash_events: list = field(default_factory=list)


def _dt(day: date, ms: int) -> datetime:
    return datetime(day.year, day.month, day.day) + timedelta(milliseconds=int(ms))


def _fill_forward_day(day: date, px: float, close_ms: int) -> DayBars:
    """LEAN-style fill-forward session: a calendar trading day with no data
    for a symbol still streams bars — every OHLC pinned at the last known
    close, volume 0. Dropping the day instead shifts every rolling-window
    computation in user code (found the hard way: empty 2024-12-31 zip).

    NOTE: minute bars are hard-coded (60_000), as is the one-bar-day span
    default in _begin_session. A second-resolution carried day therefore
    streams minute-shaped bars. Deliberately untouched by the session
    split (zero behaviour change); to be threaded through bar_ms with the
    live driver."""
    starts = np.arange(REG_OPEN_MS, close_ms, 60_000)
    flat = np.full(len(starts), float(px))
    return DayBars(day=day, start_ms=starts, open=flat, high=flat,
                   low=flat, close=flat, volume=np.zeros(len(starts)))


class PyBacktester:
    def __init__(self, algo: QCAlgorithm, store, second_store=None,
                 overrides: RunOverrides | None = None,
                 progress_cb=None, progress_every_s: float = 0.3,
                 ledger=None, generated: bool = False, bar_ms: int | None = None):
        self.algo = algo
        self.store = store
        self.second_store = second_store
        # Broker-execution ledger (live only). None = backtest. Handed to the
        # OrderBook, where it OUTRANKS the model's own fills.
        # the bar span the driver says (live: the deployment's resolution),
        # else derived from the subscriptions in _setup
        self._bar_ms_override = int(bar_ms) if bar_ms else None
        self.ledger = ledger
        # codegen OUTPUT (module sets __STRATEGY_LAB_GENERATED__): allowed to
        # use the reserved `ir:` tag prefix, which is how a compiled block
        # strategy keeps its IR deployment's order identities
        self.generated = generated
        self.overrides = overrides or RunOverrides()
        # A live run (replay or warm engine). project_calendar is the mark the
        # live driver puts on every run it starts and no backtest carries.
        # Settled history of a live deployment must not move when a backtest
        # rule changes, or the determinism check freezes it.
        self._live = bool(getattr(self.overrides, "project_calendar", False))
        self._day: date | None = None
        self._ms: int = 0
        # live-progress observation (never affects the sim): a throttled
        # snapshot after each session's equity mark, plus one final flush
        self.progress_cb = progress_cb
        self.progress_every_s = progress_every_s
        self._last_emit = 0.0

    # ---------- public ----------

    def run(self) -> dict:
        try:
            return self._run()
        except BaseException as e:  # noqa: BLE001 — user code can raise anything
            return self._error_result(e)

    # ---------- internals ----------

    def _error_result(self, e: BaseException) -> dict:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        return {"error": {"type": type(e).__name__, "message": str(e),
                          "traceback": tb},
                "logs": list(getattr(self.algo, "_logs", []))}

    def _run(self) -> dict:
        """Batch driver: the whole window, session by session. A live driver
        steps the SAME methods — _setup once, then per day _begin_session,
        _step_bar per closed bar-end, _end_session — and takes _result
        whenever it needs a payload. Nothing below is loop-carried in a
        local: every piece of state the sessions share lives on self._."""
        algo = self.algo
        sessions = self._setup()
        for day in sessions:
            self._begin_session(day)
            if not self._day_bars:
                continue                     # no session at all for this day
            # quiet-bar fast path (batch only): a live driver never calls
            # it, because it reads "last bar" from what is AVAILABLE and a
            # partial day would make that the wrong bar
            resume_after = self._fast_forward(day)
            if resume_after is not None:
                for t in sorted(self._ends_map):
                    self._step_bar(day, t, self._ends_map[t])
                    if algo._quit:
                        break
            self._end_session(day)
            if algo._quit:
                break

        resolve_hook(algo, "on_end_of_algorithm", "OnEndOfAlgorithm")()
        if sessions:
            self._emit_progress(sessions[-1], 1.0, force=True)
        return self._result()

    def _setup(self) -> list[date]:
        """initialize(), overrides, sleeve/book/portfolio, the calendar and
        the session list, the warm-up flag, hook resolution, the fast-path
        decision. Returns the sessions to drive; everything the loop needs
        is left on self._."""
        algo = self.algo
        algo._store = self.store          # history() may be called in initialize
        resolve_hook(algo, "initialize", "Initialize")()

        ov = self.overrides
        if ov.start is not None:
            algo._start_date = ov.start
        if ov.end is not None:
            algo._end_date = ov.end
        if ov.cash is not None:
            algo._cash = float(ov.cash)
        if algo._start_date is None or algo._end_date is None:
            raise ValueError("set_start_date/set_end_date required")
        if not algo.securities:
            raise ValueError("no securities subscribed — call add_equity in initialize")

        start, end, cash = algo._start_date, algo._end_date, algo._cash
        self._start, self._end, self._cash = start, end, cash
        syms = self._syms = list(algo.securities)

        # margin ceiling: set_leverage / the add_equity leverage arg / the
        # security initializer / a margin account's 2x default, else 1.0.
        # Wired as a provider, not a snapshot, so leverage declared after
        # initialize (or on a subscription added mid-run) still counts.
        sleeve = self._sleeve = Sleeve(cash, margin_max=algo._effective_leverage(),
                                       entry_price_mode="last_fill")
        sleeve.margin_provider = algo._effective_leverage
        prices = self._prices = algo._prices
        book = self._book = OrderBook(sleeve, ledger=self.ledger,
                                      clock=lambda: (self._day, self._ms),
                                      events_out=self._make_event_dispatch(),
                                      prices=prices, securities=algo.securities)
        book.generated = self.generated
        algo._book = book
        algo.portfolio = PortfolioManager(sleeve, prices)
        algo.transactions = Transactions(book)
        algo._store = self.store

        # calendar over the union of available days for the subscriptions
        daily_mode = self._daily_mode = all(sec.resolution == Resolution.DAILY
                                            for sec in algo.securities.values())
        # the bar span is the RESOLUTION, never inferred from a symbol's
        # first two bars of the day: a thin ETF that prints at 09:30 and
        # again at 09:33 does not print three-minute bars, and inferring so
        # made its 15:57 bar "end" after a 15:59 fire -- the rebalance was
        # then sized off the 15:56 print, one bar behind the IR engine
        # (rotation sleeve, 2026-08-31, one share of REW)
        self._bar_ms = self._bar_ms_override or (
            1000 if any(sec.resolution == Resolution.SECOND
                        for sec in algo.securities.values()) else 60_000)
        if daily_mode:
            self._daily_map = {s: (self.store.load_daily(s) or {}) for s in syms}
            all_days = sorted(set().union(*[set(m) for m in self._daily_map.values()]))
        else:
            self._daily_map = {}
            all_days = sorted(set().union(*[set(self.store.minute_days(s))
                                            for s in syms]))
        if not all_days:
            raise ValueError(f"no data available for {syms}")
        # Extend past the data horizon with scheduled sessions (weekdays
        # minus rule-derived holidays), exactly as the IR engine does
        # (engine.py): week-boundary selectors at the LIVE horizon
        # (last_of_week, day_before_last_of_week) otherwise treat the
        # last data day as the end of the week, and a holiday week fires
        # them on the wrong day -- a divergence no backtest can show.
        store_days = list(all_days)
        from dqengine.runtime.core.data import project_sessions
        from datetime import timedelta as _td
        if getattr(self.overrides, "project_calendar", False):
            last = all_days[-1]
            all_days = store_days + project_sessions(last, last + _td(days=14))
        cal = self._cal = SessionCalendar(all_days)
        algo._calendar = cal
        for sec in algo.securities.values():
            sec.exchange.hours.calendar = cal
        # A missing session inside the store's range silently reshapes the
        # week (a Monday hole makes Tuesday first_of_week on BOTH engines).
        # Neither engine can fill it; both must be able to SEE it.
        expected = project_sessions(store_days[0], store_days[-1])
        self._calendar_holes = sorted(set(expected) - set(store_days))
        if self._calendar_holes:
            print(f"[calendar] store is missing {len(self._calendar_holes)} "
                  f"scheduled session(s) for {syms}: "
                  f"{[d.isoformat() for d in self._calendar_holes[:8]]}",
                  flush=True)

        # session range incl. warm-up pad -- from STORE days only. Projected
        # days are calendar (week-shape) entries, never sessions to RUN: a
        # projected day <= end with no bars yet would be run as a carried
        # fill-forward session and fire the whole day's schedule on stale
        # prices (the IR engine never runs projected days either).
        first_idx = next((i for i, d in enumerate(store_days) if d >= start),
                         len(store_days))
        warm_from = max(0, first_idx - algo._warmup_days)
        sessions = self._sessions = [d for d in store_days[warm_from:] if d <= end]
        self._session_pos = {d: i for i, d in enumerate(sessions)}

        algo.is_warming_up = algo._warmup_days > 0 and bool(sessions) and \
            sessions[0] < start
        book.allow_orders = not algo.is_warming_up

        self._equity_days: list[date] = []
        self._equity: list[float] = []
        self._flows: list[float] = []
        self._pending_deposits: dict[str, float] = {}
        for d_iso, amt in (ov.cash_events or []):
            self._pending_deposits[d_iso] = (
                self._pending_deposits.get(d_iso, 0.0) + float(amt))
        self._pending_flow = 0.0        # deposits since the last equity mark
        self._on_data = resolve_hook(algo, "on_data", "OnData")
        self._on_eod = resolve_hook(algo, "on_end_of_day", "OnEndOfDay")

        # quiet-bar fast path (fastpath.py): only when user code provably
        # consumes nothing per-bar; per-session and per-visit re-checked
        self._fast_on = os.environ.get("DQENGINE_FAST_PATH", "1") != "0" \
            and not daily_mode and fastpath.on_data_is_noop(algo) \
            and not fastpath.has_consolidators(algo)
        self._fast_sessions = 0

        # per-session state, so a _step_bar before any _begin_session is a
        # loud AttributeError rather than a quiet misfire
        self._day_bars: dict = {}
        self._spans: dict = {}
        self._day_events: list = []
        self._warm = False
        self._carried_day = False
        self._ends_map: dict[int, list[tuple[str, int]]] = {}
        # prev_t / first_bar_done gate scheduled-event firing and the
        # market-on-open fill. They are reset ONLY by _begin_session, never
        # by _step_bar, or a driver stepping one bar per call would fire
        # every event on every bar and re-fill MOO on each.
        self._prev_t = -1
        # Wall-clock fire cursor (spec 2026-09-18): events with fire_ms at
        # or before this were run by WarmPyEngine.prime() off live prices,
        # so the real bar must not run them again. -1 in every batch run.
        self._primed_through = -1
        self._first_bar_done = False

        self._last_emit = time.monotonic()   # throttle counts from run start
        return sessions

    def _emit_progress(self, day, pct, force=False):
        if self.progress_cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self.progress_every_s:
            return
        self._last_emit = now
        try:
            self.progress_cb({
                "sim_date": day.isoformat(),
                "pct": round(pct, 4),
                "equity_days": [d.isoformat() for d in self._equity_days],
                "equity": [round(v, 2) for v in self._equity],
                "fills": len(self._sleeve.fills),
                "logs_tail": list(self.algo._logs[-100:]),
            })
        except Exception:   # noqa: BLE001 — observation must never kill the sim
            pass

    def _session_span(self, day_bars: dict) -> int:
        finest = None
        for b in day_bars.values():
            if b.n > 1:
                g = int(np.min(np.diff(b.start_ms.astype(np.int64))))
                if g > 0 and (finest is None or g < finest):
                    finest = g
        return min(self._bar_ms, finest) if finest else self._bar_ms

    def _begin_session(self, day: date, preloaded: dict | None = None) -> None:
        """Open a session: finish warm-up if this is the first live day,
        load (or fill-forward) the day's bars, derive spans and the day's
        scheduled events, reset the per-session gates. Leaves
        self._day_bars empty when there is no session at all for the day
        (no data and nothing to carry) — the driver then skips it and must
        NOT call _end_session."""
        algo = self.algo
        book = self._book
        prices = self._prices
        syms = self._syms
        daily_mode = self._daily_mode
        start = self._start

        warm = self._warm = day < start
        if algo.is_warming_up and not warm:
            algo.is_warming_up = False
            book.allow_orders = True
            resolve_hook(algo, "on_warmup_finished", "OnWarmupFinished")()
        if not warm:
            # deposits land at (or before) this session's open -- the IR
            # engine's _deposits_for_day, on the same run days
            for d_iso in [k for k in self._pending_deposits
                          if k <= day.isoformat()]:
                amt = self._pending_deposits.pop(d_iso)
                self._sleeve.cash += amt
                self._pending_flow += amt

        open_ms = REG_OPEN_MS
        close_ms = close_time_ms(day)

        day_bars = {}
        if preloaded is not None:
            # the live driver PUSHED the day's bars (warm.py): they are the
            # session's data, the store is not consulted
            day_bars = {s: b for s, b in preloaded.items() if s in syms and b.n}
        elif daily_mode:
            # one synthetic session-spanning bar per symbol per day
            for s in syms:
                row = self._daily_map[s].get(day)
                if row:
                    o, h, l, c, v = row
                    day_bars[s] = DayBars(
                        day=day, start_ms=np.array([open_ms]),
                        open=np.array([float(o)]), high=np.array([float(h)]),
                        low=np.array([float(l)]), close=np.array([float(c)]),
                        volume=np.array([float(v)]))
        else:
            for s in syms:
                b = self.store.load_minute_day(s, day)
                if b is not None and b.n:
                    day_bars[s] = b
        carried_day = not day_bars      # calendar session, no real data
        if carried_day and not daily_mode:
            for s in syms:
                if s in prices:
                    day_bars[s] = _fill_forward_day(day, prices[s], close_ms)
        self._day_bars = day_bars
        self._carried_day = carried_day
        self._ends_map = {}
        self._prev_t = -1
        self._primed_through = -1
        self._first_bar_done = False
        if not day_bars:
            return
        self._day = day

        spans = {}
        if daily_mode:
            for s, b in day_bars.items():
                spans[s] = close_ms - int(b.start_ms[0])
        else:
            # ONE span for the session: the data's own spacing (the finest
            # gap any symbol printed today -- a 1s store steps per second
            # under a minute-configured driver), capped by the configured
            # resolution, and NEVER a symbol's own first-two-bar gap: a
            # thin ETF printing at 09:30 and 09:33 does not print 3-minute
            # bars (see _setup).
            span = self._session_span(day_bars)
            for s in day_bars:
                spans[s] = span
        self._spans = spans
        day_events = []
        if not warm:
            for ev in algo.schedule.events:
                if ev.date_rule.matches(day, self._cal):
                    day_events.append((ev.time_rule.fire_ms(open_ms, close_ms), ev))
        self._day_events = day_events

        # Carried (data-less) session: user code runs — bars stream at the
        # carried price and events fire — but nothing EXECUTES against
        # synthesized data. Market orders rest until the next real bar
        # (IR's LEAN-validated carried-day semantic); resting orders are
        # not evaluated at all.
        book.carried = carried_day
        # backtests follow LEAN across the close (see OrderBook.market); a
        # live run keeps filling at once, as it always has. `warm` here is a
        # WARM-UP day, not a live engine: live is self._live.
        book.after_close_to_moo = not warm and not self._live

    def _fast_forward(self, day: date):
        """Batch-only. Try the quiet-bar fast path over the session; build
        self._ends_map — the merged timeline of bar ENDS across symbols —
        for whatever the sequential walk still has to cover. Returns the
        bar end to resume after (-1 = walk the whole day), or None when
        the fast path completed the session and there is nothing to walk.
        A mid-day demotion hands the rest of the session to the walk
        (resume_after = last visited bar end, so nothing runs twice)."""
        algo = self.algo
        day_bars = self._day_bars
        spans = self._spans
        resume_after, fast_done = -1, False
        if self._fast_on and fastpath.minute_indicator_count(algo) == 0:
            resume_after, fast_done = self._fast_day(
                day, day_bars, spans, self._day_events, self._carried_day,
                self._warm)
            if fast_done:
                self._fast_sessions += 1

        ends_map: dict[int, list[tuple[str, int]]] = {}
        if not fast_done:
            for s, b in day_bars.items():
                span = spans[s]
                for i in range(b.n):
                    t = int(b.start_ms[i]) + span
                    if t > resume_after:
                        ends_map.setdefault(t, []).append((s, i))
        self._ends_map = ends_map
        # the walk's event window opens where the fast path stopped. The
        # fast path's first visit already made the market-on-open fill and
        # marked it done, so a demoted day's walk does not repeat it.
        self._prev_t = resume_after
        return None if fast_done else resume_after

    def _step_bar(self, day: date, t: int, entries: list[tuple[str, int]]) -> None:
        """One merged bar-end: `entries` is every (symbol, bar index into
        self._day_bars[symbol]) whose bar ends at t — two symbols at one
        timestamp are ONE step, one slice, one on_data. Order: prices,
        resting pass 1 (same-bar exclusion only when a wall-clock prime
        covers t), on_data, scheduled events in (prev_t, t] not already
        primed, resting pass 2 with the same-bar exclusion, invested flags,
        MOO on the session's first stepped bar."""
        algo = self.algo
        book = self._book
        prices = self._prices
        sleeve = self._sleeve
        day_bars = self._day_bars
        spans = self._spans
        carried_day = self._carried_day

        self._ms = t
        algo.time = _dt(day, t)
        slice_bars = Bars()
        for s, i in entries:
            b = day_bars[s]
            o, h, l, c = (float(b.open[i]), float(b.high[i]),
                          float(b.low[i]), float(b.close[i]))
            sec = algo.securities[s]
            sec.open, sec.high, sec.low, sec.close = o, h, l, c
            sec.price = c
            sec.volume = float(b.volume[i])
            prices[s] = c
            span = spans[s]
            bar = TradeBar(sec.symbol, _dt(day, t - span), algo.time,
                           o, h, l, c, float(b.volume[i]))
            dict.__setitem__(slice_bars, s, bar)
            self._feed_indicators(s, bar, minute=True)
            self._feed_consolidators(s, bar)
        if (not self._first_bar_done and not self._warm and not self._live
                and not carried_day):
            # market-on-open: LEAN fills it at the session's first OPEN and
            # before the algorithm is handed that bar -- a strategy that
            # reads its position in on_data must already see the fill, or it
            # orders a second time. (Live keeps the old place, below.)
            self._fill_market_on_open(entries)
        # resting orders see the just-completed bars (real bars only).
        # A bar that closed at or before the primed frontier landed AFTER
        # WarmPyEngine.prime() ran the callbacks due at its end and placed
        # orders stamped (day, t); a bar cannot fill an order that did not
        # exist when it closed, so those wait for the next bar exactly as
        # the batch walk would have them wait (pass 2 already excludes the
        # ones created during this bar's handlers). Batch runs never prime
        # (_primed_through stays -1), so the condition is never true there
        # and pass 1 is unchanged — including for a cross-symbol
        # on_order_event cascade inside this loop, which keeps filling
        # against this bar as it always did. (Spec 2026-09-18 §4.2.)
        late_behind_prime = t <= self._primed_through
        if not carried_day:
            for s, i in entries:
                b = day_bars[s]
                book.check_resting(s, float(b.open[i]), float(b.high[i]),
                                   float(b.low[i]), float(b.close[i]),
                                   exclude_created=(day, t) if late_behind_prime else None)
        algo._current_slice = Slice(slice_bars, algo.time)
        algo._market_open = True
        self._on_data(algo._current_slice)
        if algo._quit:
            return
        if not self._warm:
            prev_t = self._prev_t
            primed = self._primed_through
            for fire_ms, ev in self._day_events:
                if prev_t < fire_ms <= t and fire_ms > primed:
                    ev.callback()
        # LEAN re-evaluates resting orders the moment they're placed
        # or updated, against the last completed bar — but a bar that
        # ended before the order existed can't fill it. So orders
        # UPDATED during this bar's handlers get a second look at
        # this bar; orders CREATED at this bar wait for the next.
        if not carried_day:
            for s, i in entries:
                b = day_bars[s]
                book.check_resting(s, float(b.open[i]), float(b.high[i]),
                                   float(b.low[i]), float(b.close[i]),
                                   exclude_created=(day, t))
        for s in self._syms:
            algo.securities[s].invested = sleeve.qty.get(s, 0) != 0
        if not self._first_bar_done:
            self._first_bar_done = True
            if (self._warm or self._live) and not carried_day:
                # live: unchanged -- against the first bar, after the handlers
                from .enums import OrderType as _OT
                book.fill_at_session_edge(_OT.MARKET_ON_OPEN, prices)
        self._prev_t = t

    def _fill_market_on_open(self, entries) -> None:
        """Fill resting market-on-open tickets at the OPEN of the bars in
        `entries` (the session's first stepped bars)."""
        from .enums import OrderType as _OT
        opens = {s: float(self._day_bars[s].open[i]) for s, i in entries}
        self._book.fill_at_session_edge(_OT.MARKET_ON_OPEN, opens)

    def _end_session(self, day: date) -> None:
        """Close a session: market-on-close, consolidator flush, daily
        indicator feed, on_end_of_time_step / on_end_of_day, and (live days
        only) the equity mark."""
        algo = self.algo
        book = self._book
        prices = self._prices
        sleeve = self._sleeve
        day_bars = self._day_bars

        # market-on-close: the clock is the trigger, not a price, so it
        # cannot live in check_resting. Fill at each symbol's last close.
        from .enums import OrderType as _OT
        if not self._carried_day:
            book.fill_at_session_edge(_OT.MARKET_ON_CLOSE, prices)

        # session close
        for s, b in day_bars.items():
            self._flush_consolidators(s)
            self._feed_indicators(s, None, minute=False, day_bars=b)
        resolve_hook(algo, "on_end_of_time_step", "OnEndOfTimeStep")()
        try:
            self._on_eod()
        except TypeError:
            for s in day_bars:
                self._on_eod(algo.securities[s].symbol)
        if not self._warm:
            self._equity_days.append(day)
            self._equity.append(sleeve.equity(prices))
            self._flows.append(self._pending_flow)
            self._pending_flow = 0.0
            pos = self._session_pos.get(day)
            pct = (pos + 1) / len(self._sessions) if pos is not None else 1.0
            self._emit_progress(day, pct)

        algo._market_open = False

    def _result(self, since: dict | None = None) -> dict:
        """The payload, from the current state. Safe to call mid-session:
        it reads and never mutates — no synthetic today-equity point is
        appended to the engine's own equity list.

        `since` = {"fills": n, "equity": n, "logs": [a, r, u]} returns only
        the entries past those counts for the append-only lists (fills,
        equity_days/equity/flows, and the three log sources), with
        `counts` carrying the totals so the receiver can verify its merge;
        `orders` is None on a delta (see below). Everything else (stats, position, ...) is always
        whole. The live driver asks for this every bar: the full lists grow
        with deployment age (a 5-year deployment's fills + equity series
        is ~100 KB per tick), the delta is a few hundred bytes."""
        algo = self.algo
        sleeve, book, syms = self._sleeve, self._book, self._syms
        start, end, cash = self._start, self._end, self._cash
        equity_days, equity, flows = self._equity_days, self._equity, self._flows

        # stats depend only on the equity series, which changes at
        # end_session and nowhere else. Recomputing them per bar was 200 µs
        # on a 5-year deployment -- more than the rest of the snapshot and
        # the whole socket hop together. Keyed on what they are a function
        # of; a stale key is impossible to hit without the series changing.
        key = (len(equity_days), len(equity), len(flows), cash)
        cached = getattr(self, "_stats_cache", None)
        if cached is None or cached[0] != key:
            cached = (key, stats_from_equity(equity_days, equity, flows, cash))
            self._stats_cache = cached
        stats = dict(cached[1])
        stats["fills"] = len(sleeve.fills)
        stats["orders"] = len(sleeve.orders)
        resolutions = {sec.resolution for sec in algo.securities.values()}
        if resolutions == {Resolution.DAILY}:
            res_name = "daily"
        elif Resolution.SECOND in resolutions:
            res_name = "second"
        else:
            res_name = "minute"
        # Final position, for the live path: the sandbox returns JSON, so
        # anything a deployment payload needs has to travel inside it. Sorted
        # by absolute exposure so a short ranks by size, not by sign.
        px = dict(algo._prices)
        holdings = []
        for s, q in sorted(sleeve.qty.items(),
                           key=lambda kv: -abs(kv[1] * px.get(kv[0], 0.0))):
            if q == 0:
                continue
            last = px.get(s, 0.0)
            holdings.append({
                "symbol": s, "qty": int(q), "last_price": round(last, 2),
                "market_value": round(q * last, 2),
                "entry_price": round(sleeve.get_entry_price(s) or 0.0, 4)})
        position = {
            "cash": round(sleeve.cash, 2),
            "holdings": holdings,
            "last_prices": {s: round(px.get(s, 0.0), 2) for s in syms
                            if px.get(s, 0.0) > 0},
            # order_id is the live layer's IDENTITY for a resting order, not
            # decoration: live_python derives the intent id (and therefore the
            # broker cid prefix) from it. It has to be the ticket's own
            # monotonic id and not this list's position, because a fill
            # removes a ticket and every later order would otherwise renumber
            # -- renaming a real resting protective stop into a cancel and a
            # resubmit. See tests/test_python_identity.py.
            "open_orders": [{
                "symbol": t.symbol, "qty": int(t.quantity),
                "type": t.order_type.value,
                "limit_price": t.limit_price, "stop_price": t.stop_price,
                "tag": t.tag, "order_id": int(t.order_id),
                # the trail a trailing_stop rests on — the live layer cannot
                # project one without it, and an exit it cannot project is
                # an exit that never reaches the broker
                "trail_pct": (float(t.trailing_amount)
                              if (t.trailing_amount is not None
                                  and t.trailing_as_percentage) else None)}
                for t in book._open],
        }
        s_fills = s_eq = 0
        s_logs = [0, 0, 0]
        if since:
            s_fills, s_eq = int(since.get("fills", 0)), int(since.get("equity", 0))
            s_logs = [int(x) for x in (since.get("logs") or [0, 0, 0])]
        missed = [f"missed fill: {m}" for m in book.unfilled_log]
        counts = {"fills": len(sleeve.fills), "orders": len(sleeve.orders),
                  "equity": len(equity),
                  "logs": [len(algo._logs), len(book.refused_log), len(missed)]}
        for k in ("fills", "equity"):
            if {"fills": s_fills, "equity": s_eq}[k] > counts[k]:
                raise ValueError(f"snapshot since {k}={since[k]} but only "
                                 f"{counts[k]} exist")
        log_parts = {"algo": list(algo._logs[s_logs[0]:]),
                     "refused": list(book.refused_log[s_logs[1]:]),
                     "missed": missed[s_logs[2]:]}
        return {
            "stats": stats,
            "position": position,
            "equity_days": [d.isoformat() for d in equity_days[s_eq:]],
            "equity": equity[s_eq:],
            "flows": flows[s_eq:],
            # confirmed/fees/model_px are NOT decoration: broker_exec builds
            # its fold rail from `confirmed` (model_folded_side), and with
            # the flag missing every fill counts as unfolded -- which
            # freezes the market-delta pass for that symbol after any
            # same-day fill, so the exit never goes out and the position
            # rides overnight. The IR payload has always carried them.
            "counts": counts,
            "log_parts": log_parts,
            "fills": [{"day": f.day.isoformat(), "ms": f.time_ms, "sym": f.symbol,
                       "qty": f.qty, "px": round(f.price, 4),
                       # the `ir:` prefix is the engine's identity marker;
                       # the payload shows the rule id, as the IR path does
                       "tag": (f.tag[3:] if isinstance(f.tag, str)
                               and f.tag.startswith("ir:") else f.tag),
                       "fees": round(getattr(f, "fees", 0.0) or 0.0, 4),
                       "model_px": (round(f.model_price, 4)
                                    if getattr(f, "model_price", None) is not None
                                    else None),
                       "confirmed": bool(getattr(f, "confirmed", True))}
                      for f in sleeve.fills[s_fills:]],
            # orders are NOT delta-able: a ticket's status mutates in place
            # after creation (filled/cancelled on a later bar), so a merged
            # list would carry stale statuses. Whole on a full snapshot,
            # None on a delta; nothing on the live path reads it.
            "orders": ([{"day": o.day.isoformat(), "ms": o.time_ms, "sym": o.symbol,
                         "qty": o.qty, "kind": o.kind, "status": o.status,
                         "px": o.price}
                        for o in sleeve.orders] if since is None else None),
            # unfilled_log: fills the broker CONFIRMED it did not make. The IR
            # path journals these as {"missed": True}; dropping them here
            # made a trade vanish between the model and the account with no
            # trace, which is the exact silence the ledger exists to end.
            "logs": (log_parts["algo"] + log_parts["refused"] + log_parts["missed"]
                     if since is None else None),
            "benchmark": algo._benchmark,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cash": cash,
            "leverage": sleeve.margin_max,   # the ceiling the run actually ran under
            # rejections are otherwise invisible — no log line, just an order
            # row — and "everything was refused" is exactly what a leverage
            # mismatch looks like from the outside
            "rejections": dict(book.rejections),
            "subscriptions": syms,
            "resolution": res_name,
            "calendar_holes": [d.isoformat() for d in
                               getattr(self, "_calendar_holes", [])],
            "fast_path": {"eligible": self._fast_on, "sessions": self._fast_sessions,
                          "of": len(self._sessions)},
        }

    def _fast_day(self, day, day_bars, spans, day_events, carried, warm):
        """Quiet-bar session: visit only the first/last bar, scheduled-event
        firing bars, and resting-order trigger candidates from vectorized
        scans (fastpath.py). Each visit runs the REAL per-bar machinery in
        the sequential order — price roll-forward, check_resting pass 1,
        events, pass 2 with the same-bar exclusion, invested flags — then
        rescans, so orders placed/updated/cancelled by handlers are seen
        exactly as the bar-by-bar walk would see them. Returns
        (last_visited_ms, completed); completed=False demotes the rest of
        the day to the sequential walk (a handler registered a
        minute-resolution indicator mid-day)."""
        algo = self.algo
        book = algo._book
        prices = algo._prices
        sleeve = book.sleeve
        syms = list(algo.securities)

        ends_by = {s: b.start_ms.astype(np.int64) + spans[s]
                   for s, b in day_bars.items()}
        merged = np.unique(np.concatenate(list(ends_by.values())))

        # an event fires at the first merged end >= fire_ms — the same bar
        # the sequential rule (prev_t < fire_ms <= t) lands on; one past
        # the last end never fires there either
        fire_at: dict[int, list] = {}
        for fire_ms, ev in day_events:
            i = int(np.searchsorted(merged, fire_ms, side="left"))
            if i < len(merged):
                fire_at.setdefault(int(merged[i]), []).append(ev)

        forced = sorted({int(merged[0]), int(merged[-1]), *fire_at})
        applied = dict.fromkeys(day_bars, -1)
        ind_count = fastpath.minute_indicator_count(algo)
        last_t = -1
        fi = 0

        while True:
            cand = None if carried else fastpath.next_candidate_ms(
                book, ends_by, day_bars, last_t)
            nxt = forced[fi] if fi < len(forced) else None
            if nxt is None and cand is None:
                return last_t, True
            t = nxt if cand is None else cand if nxt is None else min(nxt, cand)

            self._ms = t
            algo.time = _dt(day, t)
            at_t = []
            for s, b in day_bars.items():
                ends_s = ends_by[s]
                idx = int(np.searchsorted(ends_s, t, side="right")) - 1
                if idx < 0:
                    continue
                if idx > applied[s]:
                    sec = algo.securities[s]
                    o, h, l, c = (float(b.open[idx]), float(b.high[idx]),
                                  float(b.low[idx]), float(b.close[idx]))
                    sec.open, sec.high, sec.low, sec.close = o, h, l, c
                    sec.price = c
                    sec.volume = float(b.volume[idx])
                    prices[s] = c
                    applied[s] = idx
                if int(ends_s[idx]) == t:
                    at_t.append((s, idx))
            if (not carried and not warm and not self._live
                    and not self._first_bar_done):
                # same rule as the walk: market-on-open at the first open,
                # before any handler runs. Marked done so a mid-day demotion
                # to the walk does not fill a second time.
                self._first_bar_done = True
                self._fill_market_on_open(at_t)
            if not carried:
                for s, i in at_t:
                    b = day_bars[s]
                    book.check_resting(s, float(b.open[i]), float(b.high[i]),
                                       float(b.low[i]), float(b.close[i]))
            for ev in fire_at.get(t, ()):
                ev.callback()
            if not carried:
                for s, i in at_t:
                    b = day_bars[s]
                    book.check_resting(s, float(b.open[i]), float(b.high[i]),
                                       float(b.low[i]), float(b.close[i]),
                                       exclude_created=(day, t))
            for s in syms:
                algo.securities[s].invested = sleeve.qty.get(s, 0) != 0
            if self._live and not self._first_bar_done:
                # a live replay: where the walk fills it for a live run,
                # against the first bar, after the handlers
                self._first_bar_done = True
                if not carried:
                    from .enums import OrderType as _OT
                    book.fill_at_session_edge(_OT.MARKET_ON_OPEN, prices)
            last_t = t
            while fi < len(forced) and forced[fi] <= t:
                fi += 1
            if fastpath.minute_indicator_count(algo) != ind_count:
                return last_t, False

    def _make_event_dispatch(self):
        hook = resolve_hook(self.algo, "on_order_event", "OnOrderEvent")
        return hook

    def _feed_consolidators(self, sym: str, bar):
        for c in self.algo._consolidators.get(sym, ()):
            c.update(bar)

    def _flush_consolidators(self, sym: str):
        """Session end: emit the trailing partial bucket so a day never
        leaks one into the next."""
        for c in self.algo._consolidators.get(sym, ()):
            c.scan()

    def _feed_indicators(self, sym: str, bar, minute: bool, day_bars=None):
        regs = self.algo._indicators.get(sym)
        if not regs:
            return
        for resolution, ind in regs:
            if hasattr(ind, "update_symbol"):
                # dual-symbol: it needs to know WHICH stream this bar is
                if minute and bar is not None:
                    ind.update_symbol(sym, bar.close, bar.end_time)
                elif not minute and day_bars is not None:
                    ind.update_symbol(sym, day_bars.session_close,
                                      day_bars.day)
                continue
            bar_based = hasattr(ind, "update_bar")
            if minute and resolution in (Resolution.MINUTE, Resolution.SECOND) \
                    and bar is not None:
                if bar_based:
                    ind.update_bar(bar.end_time, bar.high, bar.low, bar.close,
                                   getattr(bar, "volume", 0.0),
                                   getattr(bar, "open", None))
                else:
                    ind.update(bar.end_time, bar.close)
            elif not minute and resolution == Resolution.DAILY and day_bars is not None:
                t = _dt(day_bars.day, close_time_ms(day_bars.day))
                if bar_based:
                    ind.update_bar(t, float(day_bars.high.max()),
                                   float(day_bars.low.min()),
                                   day_bars.session_close,
                                   float(day_bars.volume.sum()),
                                   float(day_bars.open[0]))
                else:
                    ind.update(t, day_bars.session_close)


def _parse_date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _script_advice(code: str) -> str:
    """A pasted standalone script (shebang, __main__ guard, yfinance
    downloads…) defines no QCAlgorithm — say what the engine actually
    runs instead of a bare 'no subclass found' (first real-user report,
    2026-08-30). A caller with something to offer (a converter, a wizard)
    keys off the `hint` the error carries, not off this text."""
    base = "no QCAlgorithm subclass found in the code"
    scripty = any(marker in code for marker in
                  ("__main__", "argparse", "yfinance", "requests.get",
                   "urllib")) or code.lstrip().startswith("#!")
    if not scripty:
        return base
    return (base + " — this looks like a standalone Python script, but the "
            "engine runs python trading algorithms:\n\n"
            "    class MyStrategy(QCAlgorithm):\n"
            "        def initialize(self):\n"
            "            self.set_start_date(2021, 1, 4)\n"
            "            self.set_cash(10000)\n"
            "            self.sym = self.add_equity(\"SPY\").symbol\n\n"
            "Market data comes from the engine's bar store (add_equity + "
            "self.history()), not from the network: yfinance/requests "
            "downloads don't belong in an algorithm. Code under "
            "`if __name__ == \"__main__\":` never runs.")


def _find_algorithm_class(ns: dict, code: str = "") -> type:
    seen = []
    for v in ns.values():
        if isinstance(v, type) and issubclass(v, QCAlgorithm) \
                and v is not QCAlgorithm and v not in seen:
            seen.append(v)
    if not seen:
        raise ValueError(_script_advice(code))
    if len(seen) == 1:
        return seen[0]
    with_init = [c for c in seen
                 if c.initialize is not QCAlgorithm.initialize
                 or getattr(c, "Initialize", None) is not
                 getattr(QCAlgorithm, "Initialize", None)]
    if len(with_init) == 1:
        return with_init[0]
    raise ValueError(
        f"multiple QCAlgorithm subclasses found ({', '.join(c.__name__ for c in seen)}) "
        f"— keep one algorithm class per file")


def run_python_backtest(code: str, data_root: str,
                        overrides: dict | None = None,
                        second_from: date | None = None,
                        manifest_only: bool = False,
                        progress_cb=None) -> dict:
    """Entry point for the sandbox runner and the API worker: source in,
    result dict out. Never raises for user-code problems — they come back
    as {"error": {...}}.

    manifest_only: run initialize() alone (history() is empty under
    DQENGINE_MANIFEST_PASS) and report what the algorithm needs — the sandbox
    runner's data-discovery pass, before any bars exist locally."""
    import os as _os

    from .algorithm_imports import register_algorithm_imports
    register_algorithm_imports()
    overrides = dict(overrides or {})

    def _err(e: BaseException) -> dict:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        return {"error": {"type": type(e).__name__, "message": str(e),
                          "traceback": tb}, "logs": []}

    try:
        compiled = compile(code, "<algorithm>", "exec")
    except SyntaxError as e:
        return _err(e)

    ns = {"__name__": "user_algorithm"}
    try:
        exec(compiled, ns)  # noqa: S102 — sandboxed by the runner container
        algo_cls = _find_algorithm_class(ns, code)
        algo = algo_cls()
    except BaseException as e:  # noqa: BLE001
        r = _err(e)
        if "standalone Python script" in str(e):
            r["error"]["hint"] = "standalone_script"
        if isinstance(e, ImportError):
            # "runs on my Mac, fails here" — say what the sandbox offers
            r["error"]["hint"] = "missing_import"
            r["error"]["message"] += (
                "\n\nThat import isn't available where this algorithm runs. "
                "Market data comes from the engine's bar store via "
                "add_equity() / self.history(); anything that fetches data "
                "over the internet (yfinance, API clients) doesn't belong in "
                "an algorithm, and the sandbox has no network access.")
        return r

    # Live only: rows the broker actually executed. Popped rather than read
    # so it never reaches RunOverrides, which is a pure value object.
    ledger = overrides.pop("ledger", None)
    ro = RunOverrides(start=_parse_date(overrides.get("start")),
                      end=_parse_date(overrides.get("end")),
                      project_calendar=bool(overrides.get("project_calendar")),
                      cash=overrides.get("cash"),
                      cash_events=list(overrides.get("cash_events") or []))

    if manifest_only:
        had = _os.environ.get("DQENGINE_MANIFEST_PASS")
        _os.environ["DQENGINE_MANIFEST_PASS"] = "1"
        try:
            resolve_hook(algo, "initialize", "Initialize")()
        except BaseException as e:  # noqa: BLE001
            return _err(e)
        finally:
            if had is None:
                _os.environ.pop("DQENGINE_MANIFEST_PASS", None)
            else:
                _os.environ["DQENGINE_MANIFEST_PASS"] = had
        if ro.start is not None:
            algo._start_date = ro.start
        if ro.end is not None:
            algo._end_date = ro.end
        if ro.cash is not None:
            algo._cash = float(ro.cash)
        resolutions = {sec.resolution for sec in algo.securities.values()}
        res_name = ("daily" if resolutions == {Resolution.DAILY}
                    else "second" if Resolution.SECOND in resolutions
                    else "minute")
        return {"manifest": {
            "subscriptions": list(algo.securities),
            "resolution": res_name,
            "start": algo._start_date.isoformat() if algo._start_date else None,
            "end": algo._end_date.isoformat() if algo._end_date else None,
            "cash": algo._cash,
            "warmup_days": algo._warmup_days,
        }}

    store = overrides.pop("store", None)
    if store is None:
        from dqengine.runtime.core.data import DataStore
        store = DataStore(data_root)
    return PyBacktester(algo, store, overrides=ro, ledger=ledger,
                        bar_ms=overrides.get("bar_ms"),
                        generated=bool(ns.get("__STRATEGY_LAB_GENERATED__")),
                        progress_cb=progress_cb).run()
