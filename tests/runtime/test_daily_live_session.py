"""A daily strategy, live: the same session a backtest runs, one tick at a
time.

A daily session hands the algorithm one bar and it lands at the close, so
between 09:30 and close+60s the day exists and its bar does not. The run
opens today as a PARTIAL session: the checks already due run at their own
clock time on the previous session's state, orders convert the way LEAN
converts them, and nothing else happens -- no bar, no on_data, no equity
mark. When the bar lands, the same replay runs the whole day exactly as a
backtest does.

The property that matters, and the one every case here is about: the fills
a scripted live day produces are the fills a plain backtest of the same days
produces, to the cent. Every replay is a pure function of the data it can
see, so a tick at 15:41 and a tick at 15:46 place the same order once, and
the settled history behind them never moves.
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dqengine.live import determinism                             # noqa: E402
from dqengine.runtime.algorithm import QCAlgorithm                # noqa: E402
from dqengine.runtime.backtester import PyBacktester, RunOverrides  # noqa: E402
from dqengine.runtime.core.data import close_time_ms              # noqa: E402
from dqengine.runtime.core.ledger import (ExecutionLedger, LedgerFill,  # noqa: E402
                                          LiveCappedLedger)
from dqengine.runtime.enums import Resolution                     # noqa: E402

OPEN_MS = 34_200_000
CLOSE_MS = 57_600_000


def at(h, m, s=0):
    return ((h * 60 + m) * 60 + s) * 1000


# ------------------------------------------------------------------- store

class DailyStore:
    """A daily store that hides a day until its session has settled -- what
    pydata's completeness rule does to the zip. `rows` is
    {symbol: {day: (o, h, l, c, v)}}."""

    def __init__(self, rows: dict):
        self.rows = rows
        self.complete = {d for m in rows.values() for d in m}

    def hide(self, *days):
        for d in days:
            self.complete.discard(d)
        return self

    def reveal(self, *days):
        self.complete.update(days)
        return self

    def minute_days(self, sym):
        return sorted(self.rows.get(sym, {}))

    def load_minute_day(self, sym, day):
        return None

    def load_daily(self, sym):
        return {d: r for d, r in self.rows.get(sym, {}).items()
                if d in self.complete}


WEEK = [date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31),
        date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5),
        date(2026, 8, 6), date(2026, 8, 7)]
D = date(2026, 8, 6)            # the scripted day
D1 = date(2026, 8, 7)           # and the one after it


def _rows(days, base):
    """One clean daily row per day: open = base + i, close = open + 2."""
    out = {}
    for i, d in enumerate(days):
        o = base + i
        out[d] = (float(o), float(o + 3), float(o - 1), float(o + 2), 1000.0)
    return out


def store(days=WEEK, extra=None):
    rows = {"AAA": _rows(days, 100)}
    if extra:
        rows.update(extra)
    return DailyStore(rows)


# ---------------------------------------------------------------- strategy

class Checks(QCAlgorithm):
    """Two checks either side of LEAN's 15.5-minute submission buffer: the
    one at close-20 becomes a market-on-close and fills at this session's
    close, the one at close-15 becomes a market-on-open and fills at the
    next session's open."""
    SYMS = ("AAA",)
    START = (2026, 8, 5)
    END = (2026, 8, 7)
    WARMUP = 3

    def initialize(self):
        self.set_start_date(*self.START)
        self.set_end_date(*self.END)
        self.set_cash(100000)
        self.syms = {s: self.add_equity(s, Resolution.DAILY).symbol
                     for s in self.SYMS}
        self.seen = []
        anchor = self.syms[self.SYMS[0]]
        for name, mins in (("c20", 20), ("c15", 15)):
            self.schedule.on(self.date_rules.every_day(anchor),
                             self.time_rules.before_market_close(anchor, mins),
                             self._cb(name))
        if self.WARMUP:
            self.set_warm_up(self.WARMUP, Resolution.DAILY)

    def _cb(self, name):
        def run():
            if self.is_warming_up:
                return
            self.seen.append((name, str(self.time)))
            self.trade(name)
        return run

    def trade(self, name):
        self.market_order(self.syms["AAA"], 1, tag=name)


# ----------------------------------------------------------------- harness

def live(st, today, now_ms, cls=Checks, ledger=None, end=None):
    """One live replay, built the way the driver builds it."""
    algo = cls()
    res = PyBacktester(algo, st, ledger=ledger, overrides=RunOverrides(
        project_calendar=True, end=end or today,
        live_today=today, live_now_ms=now_ms)).run()
    assert "error" not in res, res.get("error")
    return algo, res


def backtest(st, cls=Checks, end=None):
    algo = cls()
    res = PyBacktester(algo, st, overrides=RunOverrides(end=end)).run()
    assert "error" not in res, res.get("error")
    return algo, res


def fills_of(res):
    return [(f["day"], f["ms"], f["qty"], f["px"]) for f in res["fills"]]


def kinds_of(res):
    return [(o["day"], o["kind"], o["status"]) for o in res["orders"]]


def state(res):
    """Everything a payload is built from, for an idempotence check."""
    return (fills_of(res), kinds_of(res), res["equity_days"],
            [round(v, 4) for v in res["equity"]],
            res["position"]["open_orders"], res["position"]["cash"],
            res["daily_live"])


# --------------------------------------------------- the scripted live day

def _script():
    """The day as the worker ticks it, and the same day again tomorrow.
    Returns [(label, today, now_ms, result)] in order."""
    st = store().hide(D, D1)
    out = []
    for label, ms in (("15:39", at(15, 39)), ("15:41", at(15, 41)),
                      ("15:46", at(15, 46)), ("15:59", at(15, 59))):
        out.append((label, D, ms, live(st, D, ms)[1]))
    st.reveal(D)
    out.append(("16:01", D, at(16, 1), live(st, D, at(16, 1))[1]))
    out.append(("D+1 09:31", D1, at(9, 31), live(st, D1, at(9, 31))[1]))
    st.reveal(D1)
    out.append(("D+1 16:01", D1, at(16, 1), live(st, D1, at(16, 1))[1]))
    return out


def test_the_checks_fire_when_they_are_due_and_not_before():
    st = store().hide(D, D1)
    assert live(st, D, at(15, 39))[0].seen[-2:] == [
        ("c20", "2026-08-05 15:40:00"), ("c15", "2026-08-05 15:45:00")]
    # 15:40 has passed, 15:45 has not
    assert live(st, D, at(15, 41))[0].seen[-1] == ("c20", "2026-08-06 15:40:00")
    assert live(st, D, at(15, 46))[0].seen[-2:] == [
        ("c20", "2026-08-06 15:40:00"), ("c15", "2026-08-06 15:45:00")]
    # ON the boundary the check is due: the worker's precision wake lands at
    # exactly the fire it was told about, and a tick that arrived then and
    # ran nothing would put the decision a whole bar late
    algo, res = live(st, D, at(15, 40))
    assert algo.seen[-1] == ("c20", "2026-08-06 15:40:00")
    assert res["daily_live"]["next_fire_ms"] == at(15, 45)
    assert live(st, D, at(15, 39, 59))[0].seen[-1] \
        == ("c15", "2026-08-05 15:45:00")


def test_a_partial_day_marks_no_equity_and_applies_no_bar():
    st = store().hide(D, D1)
    _, res = live(st, D, at(15, 46))
    assert res["equity_days"][-1] == "2026-08-05"       # D is not settled
    assert res["daily_live"] == {"today": "2026-08-06", "bar_applied": False,
                                 "next_fire_ms": None}
    # the price the checks saw is still the PREVIOUS session's close
    assert res["position"]["last_prices"]["AAA"] == 107.0


def test_the_next_fire_is_reported_while_the_day_is_open():
    """The worker's precision wake targets this; None once nothing is left."""
    st = store().hide(D, D1)
    assert live(st, D, at(15, 39))[1]["daily_live"]["next_fire_ms"] == at(15, 40)
    assert live(st, D, at(15, 41))[1]["daily_live"]["next_fire_ms"] == at(15, 45)
    assert live(st, D, at(15, 46))[1]["daily_live"]["next_fire_ms"] is None
    st.reveal(D)
    assert live(st, D, at(16, 1))[1]["daily_live"]["next_fire_ms"] is None


def test_the_orders_convert_on_the_buffer_as_they_do_in_a_backtest():
    st = store().hide(D, D1)
    _, res = live(st, D, at(15, 46))
    resting = [(o["type"], o["tag"], o["created_day"])
               for o in res["position"]["open_orders"]]
    assert ("market_on_close", "c20", "2026-08-06") in resting
    assert ("market_on_open", "c15", "2026-08-06") in resting


def test_the_scripted_day_ends_where_the_backtest_ends():
    """The whole point. Every fill of the live sequence, to the cent, is a
    fill of a plain backtest of the same days."""
    script = _script()
    final = script[-1][3]
    _, bt = backtest(store(), end=D1)
    assert fills_of(final) == fills_of(bt)
    assert kinds_of(final) == kinds_of(bt)
    assert final["equity_days"] == bt["equity_days"]
    assert [round(v, 6) for v in final["equity"]] \
        == [round(v, 6) for v in bt["equity"]]
    assert final["position"]["cash"] == bt["position"]["cash"]
    # and the fills are the ones the rules describe. The daily bar is one
    # bar and it ends at the close, so every fill is stamped there; the
    # PRICE says which edge it took. D+1's open is 107, its close 109.
    assert fills_of(final)[-2:] == [("2026-08-07", CLOSE_MS, 1, 107.0),
                                    ("2026-08-07", CLOSE_MS, 1, 109.0)]


def test_every_replay_reproduces_itself():
    st = store().hide(D, D1)
    for today, ms in ((D, at(15, 39)), (D, at(15, 41)), (D, at(15, 46)),
                      (D, at(15, 59))):
        assert state(live(st, today, ms)[1]) == state(live(st, today, ms)[1])
    st.reveal(D)
    assert state(live(st, D, at(16, 1))[1]) == state(live(st, D, at(16, 1))[1])
    assert state(live(st, D1, at(9, 31))[1]) == state(live(st, D1, at(9, 31))[1])
    st.reveal(D1)
    assert state(live(st, D1, at(16, 1))[1]) == state(live(st, D1, at(16, 1))[1])


def test_the_settled_history_never_moves():
    """The determinism check the driver runs on every tick, applied to every
    pair of ticks in the sequence: a later replay must agree with an earlier
    one at the earlier one's horizon."""
    script = _script()
    for i, (li, ti, _, ri) in enumerate(script):
        want = determinism.history_fingerprint(ri["equity_days"], ri["equity"], ti)
        for lj, _, _, rj in script[i:]:
            got = determinism.history_fingerprint(rj["equity_days"],
                                                  rj["equity"], ti)
            assert got == want, f"{lj} disagrees with {li} at {ti}"


def test_a_fill_is_never_placed_twice_by_two_ticks_of_one_day():
    """Four ticks ran the 15:40 check on D. When D settles there is still
    ONE market-on-close fill for it, at D's close of 108 -- alongside the
    market-on-open the 15:45 check placed YESTERDAY, which takes D's open
    of 106."""
    script = _script()
    settled = script[4][3]              # the 16:01 replay of D
    d_fills = [f for f in fills_of(settled) if f[0] == "2026-08-06"]
    assert d_fills == [("2026-08-06", CLOSE_MS, 1, 106.0),
                       ("2026-08-06", CLOSE_MS, 1, 108.0)]


# -------------------------------------------------------- the day's own row

def test_a_row_for_today_is_ignored_until_the_session_has_settled(capsys):
    """The second defence. Even with the day's row in the store, a replay
    before close+60s runs the day as a partial session."""
    st = store()                        # nothing hidden: D's row is there
    _, res = live(st, D, at(15, 46))
    assert res["daily_live"]["bar_applied"] is False
    assert res["equity_days"][-1] == "2026-08-05"
    assert "still open" in capsys.readouterr().out
    _, late = live(st, D, at(16, 1))
    assert late["daily_live"]["bar_applied"] is True
    assert late["equity_days"][-1] == "2026-08-06"


def test_the_guard_uses_the_real_close_on_an_early_close():
    early = date(2026, 11, 27)          # the day after Thanksgiving, 13:00
    days = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), early]

    class Nov(Checks):
        START = (2026, 11, 25)
        END = (2026, 11, 27)
        WARMUP = 2

    st = DailyStore({"AAA": _rows(days, 100)})
    assert close_time_ms(early) == at(13, 0)
    _, res = live(st, early, at(13, 0, 59), cls=Nov)
    assert res["daily_live"]["bar_applied"] is False
    _, res = live(st, early, at(13, 1), cls=Nov)
    assert res["daily_live"]["bar_applied"] is True


def test_an_early_close_moves_the_checks_with_it():
    early = date(2026, 11, 27)
    days = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), early]

    class Nov(Checks):
        START = (2026, 11, 25)
        END = (2026, 11, 27)
        WARMUP = 2

    st = DailyStore({"AAA": _rows(days, 100)}).hide(early)
    algo, res = live(st, early, at(12, 41), cls=Nov)
    assert algo.seen[-1] == ("c20", "2026-11-27 12:40:00")
    assert res["daily_live"]["next_fire_ms"] == at(12, 45)


# ------------------------------------------------------- days with no session

def test_a_holiday_runs_no_session():
    labor_day = date(2026, 9, 7)
    days = [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]

    class Sep(Checks):
        START = (2026, 9, 4)
        END = (2026, 9, 7)
        WARMUP = 2

    st = DailyStore({"AAA": _rows(days, 100)})
    algo, res = live(st, labor_day, at(15, 46), cls=Sep)
    assert [s for s in algo.seen if "09-07" in s[1]] == []
    assert res["daily_live"] == {"today": "2026-09-07", "bar_applied": False,
                                 "next_fire_ms": None}
    assert res["equity_days"][-1] == "2026-09-04"


def test_nothing_runs_before_the_opening_bell():
    """Before 09:30 there is no session to be in, so there is no partial
    session and nothing to wake for. The engine must not report yesterday's
    fire times as today's."""
    st = store().hide(D, D1)
    algo, res = live(st, D, at(9, 29))
    assert [s for s in algo.seen if "08-06" in s[1]] == []
    assert res["daily_live"] == {"today": "2026-08-06", "bar_applied": False,
                                 "next_fire_ms": None}
    algo, res = live(st, D, at(9, 30))
    assert res["daily_live"]["next_fire_ms"] == at(15, 40)


# ------------------------------------------------------------- two symbols

class TwoSyms(Checks):
    SYMS = ("AAA", "BBB")

    def trade(self, name):
        for s in self.SYMS:
            self.market_order(self.syms[s], 1, tag=f"{name}_{s}")


def test_a_symbol_with_no_bar_yet_is_refused_and_the_other_trades():
    """LEAN refuses an order for a security that has no price; the symbol
    that does have one is unaffected."""
    late = [date(2026, 8, 7)]           # BBB's first bar is D+1
    st = store(extra={"BBB": _rows(late, 50)}).hide(D, D1)
    algo, res = live(st, D, at(15, 46), cls=TwoSyms)
    resting = {(o["tag"], o["type"]) for o in res["position"]["open_orders"]}
    assert ("c20_AAA", "market_on_close") in resting
    assert ("c15_AAA", "market_on_open") in resting
    assert not [t for t, _ in resting if t.endswith("BBB")]
    assert any("does not have an accurate price" in log
               for log in res["logs"])


def test_two_symbols_end_where_the_backtest_ends():
    late = [date(2026, 8, 7)]
    st = store(extra={"BBB": _rows(late, 50)}).hide(D, D1)
    for ms in (at(15, 41), at(15, 46), at(15, 59)):
        live(st, D, ms, cls=TwoSyms)
    st.reveal(D)
    live(st, D, at(16, 1), cls=TwoSyms)
    live(st, D1, at(9, 31), cls=TwoSyms)
    st.reveal(D1)
    _, res = live(st, D1, at(16, 1), cls=TwoSyms)
    _, bt = backtest(store(extra={"BBB": _rows(late, 50)}), cls=TwoSyms, end=D1)
    assert fills_of(res) == fills_of(bt)


# ----------------------------------------------------------------- ledger

def _ledger(fills, reconciled_from, live_from=None):
    inner = ExecutionLedger(fills, reconciled_from=reconciled_from)
    return LiveCappedLedger(inner, live_from) if live_from else inner


def test_a_broker_row_for_the_day_settles_the_at_close_ticket():
    """The market-on-close ticket fills at the clock (D, close). The
    executor's market delta went out at about 15:59 and its execution row
    is what the ticket takes: the model books the BROKER's price."""
    st = store().hide(D1)
    led = _ledger([LedgerFill(day=D, time_ms=at(15, 59, 3), symbol="AAA",
                              qty=1, price=107.91)],
                  reconciled_from=date(2026, 8, 1))
    _, res = live(st, D, at(16, 1), ledger=led)
    assert ("2026-08-06", at(15, 59, 3), 1, 107.91) in fills_of(res)
    assert ("2026-08-06", CLOSE_MS, 1, 108.0) not in fills_of(res)


def test_no_row_yet_books_the_model_fill():
    """Between the close and the sweep, absence is UNKNOWN, not a refusal:
    the model books its own fill at the derived close and the executor
    reconciles to it."""
    st = store().hide(D1)
    led = _ledger([], reconciled_from=date(2026, 8, 1), live_from=D)
    _, res = live(st, D, at(16, 1), ledger=led)
    assert ("2026-08-06", CLOSE_MS, 1, 108.0) in fills_of(res)
    # today is not decidable, so nothing about today is called a no-fill
    assert not any("broker confirmed no fill" in log and "2026-08-06" in log
                   for log in res["logs"])


def test_a_decided_no_fill_cancels_the_at_close_ticket():
    """Once the day is decidable (close + 10 min of clean sweeps), an absent
    row means the broker did not fill. The model books nothing -- and the
    at-close ticket is CANCELLED rather than left resting.

    A resting ticket is normally kept, because a protective stop the broker
    says did not fill is still live there. An at-close order is not: its one
    moment has passed, and resting it would fire it at TOMORROW's close, a
    day after the strategy asked for it and at a price nobody chose. The
    journal keeps the line either way."""
    st = store().hide(D1)
    led = _ledger([], reconciled_from=date(2026, 8, 1),
                  live_from=D + timedelta(days=1))
    _, res = live(st, D, at(16, 15), ledger=led)
    assert [f for f in fills_of(res) if f[0] == "2026-08-06"] == []
    assert any("broker confirmed no fill" in log for log in res["logs"])
    tags = {o["tag"] for o in res["position"]["open_orders"]}
    assert "c20" not in tags            # the at-close ticket, cancelled
    assert "c15" in tags                # the at-open one, still due tomorrow


def test_only_a_live_daily_run_turns_the_cancel_on():
    """The flag's one expression, read off the book each run sets up. A
    backtest of the same strategy, and every minute or second run, leave it
    off -- so an at-close ticket there keeps the behaviour it had."""
    algo, _ = live(store().hide(D1), D, at(16, 1))
    assert algo._book.cancel_edge_on_no_fill is True

    algo, _ = backtest(store())
    assert algo._book.cancel_edge_on_no_fill is False

    from conftest_helpers import SynthStore, synth_day
    days = [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)]
    minute = SynthStore({d: synth_day(d, [100, 101, 102]) for d in days})

    class Minute(Checks):
        def initialize(self):
            self.set_start_date(2026, 8, 5)
            self.set_end_date(2026, 8, 7)
            self.set_cash(100000)
            self.syms = {"AAA": self.add_equity("AAA",
                                                Resolution.MINUTE).symbol}
            self.seen = []

    algo = Minute()
    PyBacktester(algo, minute, overrides=RunOverrides(
        project_calendar=True, end=date(2026, 8, 7),
        live_today=date(2026, 8, 7), live_now_ms=at(16, 1))).run()
    assert algo._book.cancel_edge_on_no_fill is False


def test_the_cancel_reads_the_order_type_and_leaves_a_stop_resting():
    """Everything whose trigger is a price rather than a clock is untouched,
    in daily live as everywhere else: that stop IS still live at the
    broker."""
    from dqengine.runtime.core.portfolio import Sleeve
    from dqengine.runtime.orders import OrderBook

    sleeve = Sleeve(cash=10_000.0)
    sleeve.qty["AAA"] = 100
    book = OrderBook(sleeve, lambda: (D, CLOSE_MS), lambda e: None,
                     prices={"AAA": 100.0})
    book.cancel_edge_on_no_fill = True
    book.ledger = ExecutionLedger([], reconciled_from=date(2026, 8, 1))
    t = book.stop_market("AAA", -100, 90.0, tag="stop")
    book.check_resting("AAA", o=95.0, h=96.0, l=89.0, c=91.0)
    assert t.is_open()
    assert sleeve.qty["AAA"] == 100


# ------------------------------------------------- the warm engine refuses

DAILY_SRC = """
from AlgorithmImports import *


class Daily(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 5)
        self.set_end_date(2026, 8, 7)
        self.set_cash(100000)
        self.add_equity("AAA", Resolution.DAILY)
"""

MINUTE_SRC = DAILY_SRC.replace("Resolution.DAILY", "Resolution.MINUTE")


def test_a_warm_engine_will_not_serve_a_daily_strategy():
    """A daily session is one bar, and a warm engine cannot step a session's
    lone first bar until a second arrives. The live path serves the replay
    for these; this is the belt to that pair of braces."""
    from dqengine.runtime.warm import WarmPyEngine

    eng = WarmPyEngine(DAILY_SRC, store=store(),
                       overrides={"project_calendar": True,
                                  "end": "2026-08-07"})
    with pytest.raises(RuntimeError, match="cannot run on a warm engine"):
        eng.warm(D)
    assert "cannot run on a warm engine" in (eng.dead or "")


def test_the_warm_refusal_does_not_depend_on_the_live_mark():
    """Built WITHOUT project_calendar the run is not a live one, so _setup
    itself has nothing to object to. The refusal is its own check, not a
    side effect of the missing clock."""
    from dqengine.runtime.warm import WarmPyEngine

    eng = WarmPyEngine(DAILY_SRC, store=store(),
                       overrides={"end": "2026-08-07"})
    with pytest.raises(RuntimeError, match="cannot run on a warm engine"):
        eng.warm(D)


def test_a_minute_strategy_still_warms():
    """The refusal reads the resolution and nothing else."""
    from conftest_helpers import SynthStore, synth_day
    from dqengine.runtime.warm import WarmPyEngine

    days = {d: synth_day(d, [100.0, 101.0, 102.0]) for d in WEEK}
    eng = WarmPyEngine(MINUTE_SRC, store=SynthStore(days),
                       overrides={"project_calendar": True,
                                  "end": "2026-08-07"})
    eng.warm(D)
    assert eng.last_completed == D
    assert eng.dead is None


# --------------------------------------- the recorded LEAN cases still hold
#
# Every case in test_daily_lean_parity is a BACKTEST against a recording from
# the LEAN CLI. Run each of them again as a LIVE replay whose clock says the
# window's last day has settled: a live daily run is the backtest, so every
# one must still equal its fixture.

import test_daily_lean_parity as lean                             # noqa: E402

LEAN_CASES = [
    ("checks_march", lean.ChecksMarch, dict(orders=False, fills=False), ()),
    ("orders_market", lean.OrdersMarket, dict(checks=False), ()),
    ("orders_moc", lean.OrdersMoc, dict(checks=False), ()),
    ("boundary", lean.Boundary, dict(checks=False), ()),
    ("combined_july", lean.Combined, {}, ()),
    ("combined_thanks", lean.CombinedThanks, {}, ()),
    ("combined_twosym", lean.CombinedTwoSym, {}, ()),
    ("sizing", lean.Sizing, {}, ("calc_half",)),
    ("userexplicit", lean.UserExplicit, {}, ()),
]


@lean.needs_daily
@pytest.mark.parametrize("name,cls,kw,drop", LEAN_CASES,
                         ids=[c[0] for c in LEAN_CASES])
def test_the_lean_cases_hold_as_live_replays(name, cls, kw, drop):
    from dqengine.runtime.core.data import DataStore

    # the window is the strategy's own; read its last day off initialize()
    probe = cls()
    probe.initialize()
    end = probe._end_date

    algo = cls()
    res = PyBacktester(algo, DataStore(lean.DATA), overrides=RunOverrides(
        project_calendar=True, live_today=end,
        live_now_ms=86_400_000)).run()
    assert "error" not in res, res.get("error")
    lean.compare(algo, name, drop=drop, **kw)


@lean.needs_daily
def test_the_userlike_case_holds_as_a_live_replay():
    """The one case whose share counts are out of the comparison (LEAN and
    this engine round a partial set_holdings adjustment differently, at
    every resolution). Its timing and prices are the comparison."""
    from dqengine.runtime.core.data import DataStore

    algo = lean.UserLike()
    res = PyBacktester(algo, DataStore(lean.DATA), overrides=RunOverrides(
        project_calendar=True, live_today=date(2025, 12, 31),
        live_now_ms=86_400_000)).run()
    assert "error" not in res, res.get("error")
    fx = lean.fixture("userlike")
    assert ([r["time"] for r in algo.rows if r["at"] == "rebalance"]
            == [r["time"] for r in fx["checks"] if r["at"] == "rebalance"])
    assert {o["type"] for o in algo.orders_seen} == {"market_on_open"}
    assert algo.fills_seen[:2] == fx["fills"][:2]
