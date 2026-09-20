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
    def on_data(self, data): pass
"""


def test_a_late_symbol_behind_the_frontier_dies_rather_than_stepping_backwards():
    """Per-symbol frontiers let a late symbol's earlier bars be stepped after
    a sibling's later ones: algo.time goes backwards and a scheduled event
    whose window was already crossed FIRES AGAIN -- a second rebalance."""
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
    # tick 2: BBB's missing bars land -- ends at +3m,+4m are BEHIND the frontier
    store.days["BBB"][d] = synth_day(d, [50, 51, 52, 53, 54, 55])
    with pytest.raises(RuntimeError, match="behind the frontier"):
        eng.advance(OPEN_MS + 6 * 60_000, d)
    assert eng._bt.algo.fired == 1, "the event must not have fired twice"


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
