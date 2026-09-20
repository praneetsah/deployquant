"""Wave 3: consolidators.

The point of these is that a minute/daily-only resolution set can still run
an hourly strategy. Aggregation must be exact (open first, high max, low
min, close last, volume summed) and a session must never leak a partial
bucket into the next day.
"""
from datetime import datetime, timedelta

import pytest

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.bars import TradeBar
from dqengine.runtime.consolidators import TradeBarConsolidator, span_of
from dqengine.runtime.enums import Resolution
from dqengine.runtime.errors import UnsupportedApiError

from conftest_helpers import two_day_store


def bar(minute, o, h, l, c, v=1.0):
    t = datetime(2026, 1, 5, 9, 30) + timedelta(minutes=minute)
    return TradeBar("X", t, t, o, h, l, c, v)


# ---------------- the consolidator itself ----------------

def test_span_of_accepts_both_spellings():
    assert span_of(timedelta(hours=1)) == timedelta(hours=1)
    assert span_of(Resolution.MINUTE) == timedelta(minutes=1)
    assert span_of(Resolution.DAILY) == timedelta(days=1)
    with pytest.raises(ValueError):
        span_of("hourly")


def test_ohlcv_aggregation_is_exact():
    c = TradeBarConsolidator(timedelta(hours=1))
    out = []
    c.add_handler(out.append)
    for m, (o, h, l, cl) in enumerate([(10, 15, 8, 12), (12, 20, 11, 19),
                                       (19, 21, 5, 7)]):
        c.update(bar(m, o, h, l, cl, v=2.0))
    c.scan()
    assert len(out) == 1
    b = out[0]
    assert b.open == 10          # first
    assert b.high == 21          # max
    assert b.low == 5            # min
    assert b.close == 7          # last
    assert b.volume == 6.0       # summed


def test_a_bucket_emits_only_once_a_later_bar_proves_it_complete():
    """Emitting on a timer would hand user code a partial bar it cannot
    distinguish from a whole one. Buckets are WALL-CLOCK aligned (09:00-
    10:00), not relative to the first bar seen."""
    c = TradeBarConsolidator(timedelta(hours=1))
    out = []
    c.add_handler(out.append)
    for m in (0, 10, 20):                  # 09:30, 09:40, 09:50
        c.update(bar(m, 1, 1, 1, 1))
    assert out == []                       # still inside the 09:00 bucket
    c.update(bar(30, 2, 2, 2, 2))          # 10:00 — a new bucket
    assert len(out) == 1
    assert out[0].close == 1


def test_scan_flushes_the_trailing_partial_bucket():
    c = TradeBarConsolidator(timedelta(hours=1))
    out = []
    c.add_handler(out.append)
    c.update(bar(0, 1, 1, 1, 1))
    c.scan()
    assert len(out) == 1
    c.scan()                                # nothing left to emit
    assert len(out) == 1


def test_consolidated_holds_the_last_emitted_bar():
    c = TradeBarConsolidator(timedelta(minutes=1))
    c.update(bar(0, 1, 1, 1, 1))
    c.update(bar(1, 2, 2, 2, 2))
    assert c.consolidated.close == 1


# ---------------- wired into the algorithm ----------------

class Hourly(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.seen = []
        self.consolidate(self.sym, timedelta(hours=1), self.seen.append)


def test_consolidate_delivers_bars_to_the_handler():
    algo = Hourly()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    # three bars per session, all inside one hour -> one bar per day
    assert len(algo.seen) == 2
    assert algo.seen[0].open == 99.5 and algo.seen[0].close == 102.0
    assert algo.seen[1].close == 105.0


def test_a_session_never_leaks_a_partial_bucket_into_the_next_day():
    algo = Hourly()
    PyBacktester(algo, two_day_store()).run()
    assert algo.seen[0].close == 102.0     # day 1 ends at day 1's close
    assert algo.seen[1].open == 102.5      # day 2 starts fresh


class RegisteredIndicator(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.ind = self.sma(self.sym, 2)
        self.hourly = self.consolidate(self.sym, timedelta(hours=1))
        self.register_indicator(self.sym, self.ind, self.hourly)


def test_register_indicator_feeds_from_the_consolidator():
    algo = RegisteredIndicator()
    assert "error" not in PyBacktester(algo, two_day_store()).run()
    # two hourly bars, closes 102 and 105 -> SMA(2) = 103.5
    assert algo.ind.value == pytest.approx(103.5)


def test_register_indicator_accepts_a_bare_period():
    class A(RegisteredIndicator):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 25)
            self.set_cash(10000)
            self.sym = self.add_equity("TQQQ").symbol
            self.ind = self.sma(self.sym, 2)
            self.register_indicator(self.sym, self.ind, timedelta(hours=1))

    algo = A()
    PyBacktester(algo, two_day_store()).run()
    assert algo.ind.value == pytest.approx(103.5)


def test_a_bar_based_indicator_can_be_registered_too():
    class A(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 25)
            self.set_cash(10000)
            self.sym = self.add_equity("TQQQ").symbol
            self.ind = self.atr(self.sym, 2)
            self.register_indicator(self.sym, self.ind,
                                    self.consolidate(self.sym,
                                                     timedelta(hours=1)))

    algo = A()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert algo.ind.value > 0


def test_consolidate_rejects_an_unknown_argument():
    a = QCAlgorithm()
    a.add_equity("SPY")
    with pytest.raises(UnsupportedApiError, match="nonsense"):
        a.consolidate("SPY", timedelta(hours=1), None, nonsense=1)


def test_deregister_removes_an_indicator_from_the_feed():
    a = QCAlgorithm()
    a.add_equity("SPY")
    ind = a.sma("SPY", 5)
    assert a._indicators["SPY"]
    a.deregister_indicator(ind)
    assert a._indicators["SPY"] == []
