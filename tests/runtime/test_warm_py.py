"""A warm session, stepped bar by bar, must land exactly where a full replay
lands. If these ever differ, live is trading on numbers the backtest never
produced.

The live day is stepped in PIECES here -- several advance() calls with the
clock moving forward, the way a real tick sequence arrives -- because that
is the case the batch driver never exercises and the one where a
re-loaded day could double-step or skip a bar.
"""
import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime.core.data import DataStore                              # noqa: E402
from dqengine.runtime import run_python_backtest                        # noqa: E402
from dqengine.runtime.warm import WarmPyEngine                          # noqa: E402

from dqengine.config import data_root                                  # noqa: E402

ENGINE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = data_root()
ALGO = os.path.join(ENGINE, "dqengine", "examples", "tqqq_weekly.py")

needs_data = pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "tqqq")),
    reason="local TQQQ minute data not present")

WINDOW = {"start": "2021-01-04", "end": "2021-03-01", "cash": 1000.0}
LAST = date(2021, 3, 1)
CLOSE_MS = 16 * 3600 * 1000


def _fills(res):
    return [(f["day"], f["sym"], f["qty"], f["px"]) for f in res["fills"]]


def _engine(code):
    return WarmPyEngine(code, store=DataStore(DATA), overrides=dict(WINDOW))


@needs_data
def test_warm_then_one_advance_matches_full_replay():
    code = open(ALGO).read()
    ref = run_python_backtest(code, data_root=DATA, overrides=dict(WINDOW))

    eng = _engine(code)
    eng.warm(through=date(2021, 2, 26))
    assert eng.advance(CLOSE_MS, LAST) is True
    eng.end_session(LAST)
    got = eng.snapshot()

    assert eng.dead is None
    assert _fills(got) == _fills(ref)
    assert got["stats"]["end_equity"] == ref["stats"]["end_equity"]
    assert got["equity"] == ref["equity"]


@needs_data
def test_the_live_day_stepped_in_pieces_matches_the_replay():
    """Several ticks through the day. Each advance must step exactly the
    bars that closed since the last one -- never twice, never skipped."""
    code = open(ALGO).read()
    ref = run_python_backtest(code, data_root=DATA, overrides=dict(WINDOW))

    eng = _engine(code)
    eng.warm(through=date(2021, 2, 26))
    before = eng.bars_stepped
    for hh, mm in ((9, 45), (11, 0), (13, 30), (15, 59), (16, 0)):
        eng.advance((hh * 3600 + mm * 60) * 1000, LAST)
    eng.end_session(LAST)
    got = eng.snapshot()

    assert eng.bars_stepped - before == 390, "a full minute session is 390 bars"
    assert _fills(got) == _fills(ref)
    assert got["stats"]["end_equity"] == ref["stats"]["end_equity"]


@needs_data
def test_an_advance_with_nothing_new_steps_nothing():
    code = open(ALGO).read()
    eng = _engine(code)
    eng.warm(through=date(2021, 2, 26))
    eng.advance((10 * 3600) * 1000, LAST)
    n = eng.bars_stepped
    assert eng.advance((10 * 3600) * 1000, LAST) is False
    assert eng.bars_stepped == n


@needs_data
def test_a_snapshot_mid_session_does_not_disturb_the_run():
    """The driver snapshots after every advance. That must be a pure read."""
    code = open(ALGO).read()
    ref = run_python_backtest(code, data_root=DATA, overrides=dict(WINDOW))
    eng = _engine(code)
    eng.warm(through=date(2021, 2, 26))
    for ms in ((10 * 3600) * 1000, (14 * 3600) * 1000, CLOSE_MS):
        eng.advance(ms, LAST)
        eng.snapshot()
    eng.end_session(LAST)
    assert _fills(eng.snapshot()) == _fills(ref)


@needs_data
def test_advance_before_warm_dies():
    eng = _engine(open(ALGO).read())
    with pytest.raises(RuntimeError):
        eng.advance(CLOSE_MS, LAST)
    assert eng.dead


@needs_data
def test_a_day_that_was_already_completed_dies():
    eng = _engine(open(ALGO).read())
    eng.warm(through=date(2021, 2, 26))
    with pytest.raises(RuntimeError, match="already completed"):
        eng.advance(CLOSE_MS, date(2021, 2, 26))


@needs_data
def test_rolling_without_end_session_dies():
    eng = _engine(open(ALGO).read())
    eng.warm(through=date(2021, 2, 25))
    eng.advance(CLOSE_MS, date(2021, 2, 26))
    with pytest.raises(RuntimeError, match="without end_session"):
        eng.advance(CLOSE_MS, LAST)


@needs_data
def test_a_dead_engine_refuses_to_snapshot():
    eng = _engine(open(ALGO).read())
    eng.warm(through=date(2021, 2, 26))
    eng.stale("forced")
    with pytest.raises(RuntimeError, match="dead"):
        eng.snapshot()


@needs_data
def test_quit_marks_the_engine_dead():
    # inject quit() as the FIRST statement of the strategy's own on_data:
    # a separately defined on_data would be overridden by the real one
    # further down the class body
    src = open(ALGO).read()
    m = re.search(r"^(    def on_data\(self, [^)]*\):\n)", src, re.M)
    assert m, "no on_data in the fixture strategy"
    # only on the LIVE day: on_data runs during warm-up sessions too, and a
    # quit there would (correctly) kill the engine inside warm()
    code = (src[:m.end()]
            + "        if self.time.date().isoformat() == '2021-03-01':\n"
            + "            self.quit('done')\n" + src[m.end():])
    eng = _engine(code)
    eng.warm(through=date(2021, 2, 26))
    with pytest.raises(RuntimeError, match="quit"):
        eng.advance(CLOSE_MS, LAST)
    assert "quit" in (eng.dead or "")


def test_a_history_change_under_a_stepped_symbol_dies():
    """A different first bar for today than the one we stepped is history
    changing under us. Never absorbed."""
    from conftest_helpers import synth_day, SynthStore, OPEN_MS
    import numpy as np

    d = date(2026, 8, 24)
    store = SynthStore({d: synth_day(d, [100, 101, 102, 103, 104])})
    code = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
    def on_data(self, data): pass
"""
    eng = WarmPyEngine(code, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 2 * 60_000, d)
    shifted = synth_day(d, [100, 101, 102, 103, 104])
    shifted.start_ms = shifted.start_ms + 60_000       # first bar moved
    store.days[d] = shifted
    with pytest.raises(RuntimeError, match="history changed"):
        eng.advance(OPEN_MS + 5 * 60_000, d)


def _two_symbol_store(day, n_a, n_b):
    from conftest_helpers import synth_day, OPEN_MS
    import numpy as np

    class Store:
        def __init__(self):
            self.days = {"AAA": {day: synth_day(day, list(range(100, 100 + n_a)))},
                         "BBB": {day: synth_day(day, list(range(50, 50 + n_b)))}}
        def minute_days(self, sym):
            return sorted(self.days[sym])
        def load_minute_day(self, sym, d):
            return self.days[sym].get(d)
        def load_daily(self, sym):
            return {}
    return Store()


TWO = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(1000)
        self.add_equity("AAA", Resolution.MINUTE); self.add_equity("BBB", Resolution.MINUTE)
        self.fired = 0
        self.schedule.on(self.date_rules.every_day("AAA"),
                         self.time_rules.after_market_open("AAA", 3), self._go)
    def _go(self): self.fired += 1
"""


def test_a_late_symbol_behind_the_frontier_is_stepped_without_refiring_events():
    """Stepping a late symbol's earlier bars after a sibling's later ones
    moved algo.time backwards and a scheduled event whose window was already
    crossed FIRED AGAIN -- a second rebalance. That is why a late bar used
    to kill the engine. For a strategy with no per-bar code it is absorbed
    instead: the clock does not move, so the event cannot see its window
    twice (2026-10-05: 14 rebuilds in one session from exactly this)."""
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 6, 6)
    eng = WarmPyEngine(TWO, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.open_grace_ms = 0
    eng.warm(through=date(2026, 8, 23))
    # tick 1: BBB's refresh "failed" -- store shows only 2 bars for it
    store.days["BBB"][d] = synth_day(d, [50, 51])
    eng.advance(OPEN_MS + 5 * 60_000, d)                # AAA stepped to +5m
    assert eng._bt.algo.fired == 1
    clock = (eng._bt._prev_t, eng._bt._ms, eng._bt.algo.time)
    # tick 2: BBB's missing bars land -- ends at +3m,+4m,+5m are BEHIND the
    # frontier; +6m is in order for both
    store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53, 54, 55])
    assert eng.advance(OPEN_MS + 6 * 60_000, d) is True
    assert eng.dead is None
    assert eng.late_bars == 3
    assert eng._bt.algo.fired == 1, "the event must not have fired twice"
    assert eng._last_end == {"AAA": OPEN_MS + 6 * 60_000,
                             "BBB": OPEN_MS + 6 * 60_000}
    assert eng._bt._prev_t == OPEN_MS + 6 * 60_000 > clock[0]
    assert eng._bt.algo.time > clock[2]
    assert eng._bt.algo.securities["BBB"].close == 55.0


def test_the_cursors_are_back_after_a_tick_that_only_stepped_late_bars():
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 6, 6)
    eng = WarmPyEngine(TWO, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.open_grace_ms = 0
    eng.warm(through=date(2026, 8, 23))
    store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53])
    eng.advance(OPEN_MS + 5 * 60_000, d)                # BBB to +4m, AAA to +5m
    clock = (eng._bt._prev_t, eng._bt._ms, eng._bt.algo.time)
    # the same minute's bar for BBB, after AAA's was stepped (the QID case)
    store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53, 54])
    assert eng.advance(OPEN_MS + 5 * 60_000, d) is True
    assert eng.late_bars == 1 and eng.dead is None
    assert (eng._bt._prev_t, eng._bt._ms, eng._bt.algo.time) == clock
    assert eng._frontier == OPEN_MS + 5 * 60_000
    assert eng._bt.algo.fired == 1


def test_a_symbol_whose_first_bars_land_after_the_open_joins_the_session():
    """A thin ETF that has not traded yet when the session opens (the
    2026-10-05 'symbols joined mid-session' rebuilds, ten of them)."""
    from conftest_helpers import OPEN_MS, synth_day
    import numpy as np
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 12, 1)                 # BBB: nothing usable
    eng = WarmPyEngine(TWO, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.open_grace_ms = 0
    eng.warm(through=date(2026, 8, 23))
    store.days["BBB"].pop(d)
    eng.advance(OPEN_MS + 6 * 60_000, d)
    assert eng._day == d and "BBB" not in eng._bt._day_bars
    # BBB's first trades of the day: bars starting +4m and +7m
    late = synth_day(d, [50, 51])
    late.start_ms = np.array([OPEN_MS + 4 * 60_000, OPEN_MS + 7 * 60_000])
    store.days["BBB"][d] = late
    assert eng.advance(OPEN_MS + 8 * 60_000, d) is True
    assert eng.dead is None
    assert eng.joined_late == ["BBB"]
    assert eng.late_bars == 1                           # the +5m end
    assert eng._last_end["BBB"] == OPEN_MS + 8 * 60_000
    assert eng._bt.algo.securities["BBB"].close == 51.0
    assert eng._bt.algo.fired == 1
    # and wrong data is still fatal for it: a first bar that moves
    moved = synth_day(d, [50, 51, 52])
    moved.start_ms = np.array([OPEN_MS + 3 * 60_000, OPEN_MS + 7 * 60_000,
                               OPEN_MS + 8 * 60_000])
    store.days["BBB"][d] = moved
    with pytest.raises(RuntimeError, match="history changed"):
        eng.advance(OPEN_MS + 9 * 60_000, d)


SCHEDULED = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(1000)
        self.add_equity("AAA", Resolution.MINUTE); self.add_equity("BBB", Resolution.MINUTE)
        self.sma = self.sma("BBB", 3, Resolution.MINUTE)
        self.fired = []
        self.schedule.on(self.date_rules.every_day("AAA"),
                         self.time_rules.after_market_open("AAA", 9), self._go)
    def _go(self):
        self.fired.append(str(self.time))
        if self.sma.is_ready and self.securities["BBB"].close > self.sma.current.value:
            self.set_holdings("BBB", 0.5)
        else:
            self.set_holdings("AAA", 0.5)
"""


def test_a_scheduled_decision_is_the_same_with_bars_that_came_late():
    """The parity that matters for a strategy that decides on a schedule:
    by the time it fires, every late bar has been stepped, so its
    indicators and its orders are what an on-time session gives."""
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    closes_b = [50, 49, 51, 52, 50, 53, 54, 52, 55, 56, 57, 58]

    def run(delayed: bool):
        store = _two_symbol_store(d, 12, 12)
        full = synth_day(d, closes_b)
        store.days["BBB"][d] = full
        eng = WarmPyEngine(SCHEDULED, store=store,
                           overrides={"start": "2026-08-24",
                                      "end": "2026-08-24", "cash": 1000.0})
        eng.open_grace_ms = 0
        eng.warm(through=date(2026, 8, 23))
        for m in range(2, 13):
            if delayed:
                # BBB runs two minutes behind until +8m, then catches up
                n = m - 2 if m < 8 else m
                store.days["BBB"][d] = synth_day(d, closes_b[:max(n, 1)])
            eng.advance(OPEN_MS + m * 60_000, d)
        assert eng.dead is None
        snap = eng.snapshot()
        return (eng, [(f["sym"], f["qty"], f["px"]) for f in snap["fills"]],
                list(eng._bt.algo.fired), float(eng._bt.algo.sma.current.value))

    on_time, late = run(False), run(True)
    assert late[0].late_bars > 0 and on_time[0].late_bars == 0
    assert late[1:] == on_time[1:]
    assert len(late[2]) == 1 and late[1], "it fired once and it traded"


def test_the_session_waits_briefly_for_a_late_symbol_at_the_open():
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 6, 6)
    eng = WarmPyEngine(TWO, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.warm(through=date(2026, 8, 23))
    store.days["BBB"][d] = synth_day(d, [50])           # BBB not here yet
    assert eng.advance(OPEN_MS + 60_000, d) is False    # inside the grace
    assert eng._day is None
    store.days["BBB"][d] = synth_day(d, [50, 51, 52])
    assert eng.advance(OPEN_MS + 2 * 60_000, d) is True
    assert eng.dead is None


# --------------------------------- late bars: where they are still fatal
# A first version stepped a late bar at its own earlier time (review: it
# filled a stop placed after the bar closed, and ran on_data twice for one
# minute); a second absorbed it for every strategy (review: on_data and
# paired indicators silently lost bars). Absorbing is therefore limited to
# strategies where a bar does nothing but move a price and an indicator.

RESTING = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(10000)
        self.add_equity("AAA", Resolution.MINUTE); self.add_equity("BBB", Resolution.MINUTE)
        self.schedule.on(self.date_rules.every_day("AAA"),
                         self.time_rules.after_market_open("AAA", 5), self._go)
    def _go(self):
        self.market_order("BBB", 10)
        self.stop_market_order("BBB", -10, 48.0)
"""


def test_a_late_bar_for_a_symbol_with_a_resting_order_still_rebuilds():
    """BBB's bar ending +4m traded down to 44 and arrives late, after the
    +5m event bought BBB and rested a stop at 48. Whether that bar fills
    the stop is the replay's question, not a guess for the warm engine."""
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 8, 8)
    eng = WarmPyEngine(RESTING, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 10000.0})
    eng.open_grace_ms = 0
    eng.warm(through=date(2026, 8, 23))
    store.days["BBB"][d] = synth_day(d, [50, 51, 52])           # to +3m
    eng.advance(OPEN_MS + 5 * 60_000, d)                        # event fires
    store.days["BBB"][d] = synth_day(d, [50, 51, 52, 45, 52])   # +4m low = 44
    with pytest.raises(RuntimeError, match="resting order is open on BBB"):
        eng.advance(OPEN_MS + 5 * 60_000, d)
    assert eng.dead and eng.late_bars == 0
    # AAA has no resting order: its late bars would still be absorbed
    assert eng._bt.late_bar_refusal("AAA") is None


ONDATA = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(10000)
        self.add_equity("AAA", Resolution.MINUTE); self.add_equity("BBB", Resolution.MINUTE)
        self.calls = 0
    def on_data(self, data):
        self.calls += 1
        if self.time.hour == 9 and self.time.minute == 35:
            self.market_order("AAA", 1)
"""


def test_a_late_bar_under_an_on_data_strategy_still_rebuilds():
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    for case in ("behind", "joined"):
        store = _two_symbol_store(d, 8, 8)
        eng = WarmPyEngine(ONDATA, store=store,
                           overrides={"start": "2026-08-24",
                                      "end": "2026-08-24", "cash": 10000.0})
        eng.open_grace_ms = 0
        eng.warm(through=date(2026, 8, 23))
        if case == "behind":
            store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53])
        else:
            full = store.days["BBB"].pop(d)
        eng.advance(OPEN_MS + 5 * 60_000, d)
        calls = eng._bt.algo.calls
        store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53, 54])
        match = ("behind the frontier" if case == "behind"
                 else "joined mid-session")
        with pytest.raises(RuntimeError, match=match) as e:
            eng.advance(OPEN_MS + 5 * 60_000, d)
        assert "on_data handler" in str(e.value)
        assert eng._bt.algo.calls == calls, "on_data must not run again"


def test_a_two_symbol_indicator_on_the_late_symbol_still_rebuilds():
    from conftest_helpers import OPEN_MS, synth_day
    d = date(2026, 8, 24)
    store = _two_symbol_store(d, 8, 8)
    eng = WarmPyEngine(TWO, store=store,
                       overrides={"start": "2026-08-24", "end": "2026-08-24",
                                  "cash": 1000.0})
    eng.open_grace_ms = 0
    eng.warm(through=date(2026, 8, 23))

    class Paired:
        def update_symbol(self, *a, **k): return False
    from dqengine.runtime.enums import Resolution
    eng._bt.algo._indicators.setdefault("BBB", []).append(
        (Resolution.MINUTE, Paired()))
    assert "two-symbol indicator" in eng._bt.late_bar_refusal("BBB")
    assert eng._bt.late_bar_refusal("AAA") is None
