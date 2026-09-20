"""The warm engine's step granularity is a property of the data, not an
assumption: second bars step one per second, and an UNCLOSED bar is never
stepped -- acting on an OHLC that can still change is the failure that rule
exists to prevent.

This is what makes "orders out sub-second" a config change (bar size) rather
than a rewrite: the driver steps whatever closed since the last call, at
whatever spacing the store holds.
"""
import os
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from conftest_helpers import OPEN_MS, SynthStore                  # noqa: E402
from dqengine.runtime.core.data import DayBars                                # noqa: E402
from dqengine.runtime.warm import WarmPyEngine                          # noqa: E402

CODE = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
        self.seen = 0
    def on_data(self, data):
        self.seen += 1
"""


def _second_day(day, n, spacing_ms=1000):
    c = np.linspace(100.0, 101.0, n)
    return DayBars(day=day,
                   start_ms=np.array([OPEN_MS + i * spacing_ms for i in range(n)]),
                   open=c - 0.1, high=c + 0.2, low=c - 0.2, close=c,
                   volume=np.ones(n))


def _engine(store, bar_ms):
    return WarmPyEngine(CODE, store=store, bar_ms=bar_ms,
                        overrides={"start": "2026-08-24", "end": "2026-08-24",
                                   "cash": 1000.0})


def test_second_bars_step_one_per_second():
    d = date(2026, 8, 24)
    store = SynthStore({d: _second_day(d, 600)})
    eng = _engine(store, bar_ms=1000)
    eng.warm(through=date(2026, 8, 23))
    # five closed 1s bars: ends at +1s .. +5s
    assert eng.advance(OPEN_MS + 5_000, d) is True
    assert eng.bars_stepped == 5
    assert eng._bt.algo.seen == 5


def test_an_unclosed_bar_is_never_stepped():
    """end <= now, strictly. A bar whose end is 500ms in the future is still
    forming."""
    d = date(2026, 8, 24)
    store = SynthStore({d: _second_day(d, 600)})
    eng = _engine(store, bar_ms=1000)
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 500, d)
    assert eng.bars_stepped == 0
    eng.advance(OPEN_MS + 1_000, d)
    assert eng.bars_stepped == 1


def test_minute_bars_step_one_per_minute_with_the_same_driver():
    d = date(2026, 8, 24)
    store = SynthStore({d: _second_day(d, 390, spacing_ms=60_000)})
    eng = _engine(store, bar_ms=60_000)
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3 * 60_000, d)
    assert eng.bars_stepped == 3


def test_the_span_comes_from_the_data_not_the_config():
    """A store of 1s bars stepped by an engine configured for minutes must
    still step per second: the data is the truth about its own spacing."""
    d = date(2026, 8, 24)
    store = SynthStore({d: _second_day(d, 600)})
    eng = _engine(store, bar_ms=60_000)
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3_000, d)
    assert eng.bars_stepped == 3
