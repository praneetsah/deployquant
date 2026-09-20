import statistics
from datetime import datetime

from dqengine.runtime.indicators import (AverageTrueRange, ExponentialMovingAverage,
                                   Maximum, Minimum, RelativeStrengthIndex,
                                   SimpleMovingAverage, StandardDeviation)

T = datetime(2026, 8, 24, 16, 0)


def feed(ind, vals):
    for v in vals:
        ind.update(T, float(v))
    return ind


def test_sma():
    s = feed(SimpleMovingAverage(3), [1, 2, 3, 4])
    assert s.is_ready and abs(s.current.value - 3.0) < 1e-12
    assert abs(s.Current.Value - 3.0) < 1e-12          # Pascal chain


def test_sma_not_ready():
    s = feed(SimpleMovingAverage(3), [1, 2])
    assert not s.is_ready


def test_ema_is_seeded_with_the_sma_of_the_first_period():
    """LEAN seeds an EMA with the SMA of its first `period` samples and
    reports 0 until then (ExponentialMovingAverage.cs). Seeding with the
    first value instead diverges whenever the period is close to the sample
    count."""
    from dqengine.runtime.indicators import ExponentialMovingAverage
    e = ExponentialMovingAverage(3)
    for v in (3.0, 6.0):
        e.update(None, v)
        assert e.value == 0.0 and not e.is_ready
    e.update(None, 9.0)
    assert e.is_ready and e.value == 6.0        # SMA of 3, 6, 9

def test_std_matches_pstdev():
    vals = [3, 1, 4, 1, 5, 9, 2, 6]
    s = feed(StandardDeviation(5), vals)
    assert abs(s.current.value - statistics.pstdev(vals[-5:])) < 1e-12


def test_rsi_wilder_oscillates_around_50_on_alternating():
    # Wilder RSI(2) on equal alternating moves converges to 33.3 after a
    # down-move and 66.7 after an up-move (the smoothed averages oscillate;
    # only their mean is 50)
    r = feed(RelativeStrengthIndex(2), [10, 11, 10, 11, 10, 11, 10, 11, 10])
    assert r.is_ready and 30 < r.current.value < 40      # last move was down
    feed(r, [11])
    assert 60 < r.current.value < 72                     # and back up


def test_rsi_all_gains_is_100():
    r = feed(RelativeStrengthIndex(3), [1, 2, 3, 4, 5])
    assert r.is_ready and r.current.value == 100.0


def test_max_min():
    assert feed(Maximum(3), [5, 9, 2, 6]).current.value == 9
    assert feed(Minimum(3), [5, 9, 2, 6]).current.value == 2


def test_atr_wilder():
    a = AverageTrueRange(2)
    a.update_bar(T, high=12, low=10, close=11)      # TR seed: 2
    a.update_bar(T, high=13, low=11, close=12)      # TR = max(2, |13-11|, |11-11|) = 2
    a.update_bar(T, high=16, low=12, close=15)      # TR = max(4, 4, 0) = 4
    assert a.is_ready
    # Wilder: after seed avg of first 2 TRs (2,2) = 2 → (2*1 + 4)/2 = 3
    assert abs(a.current.value - 3.0) < 1e-12


def test_algorithm_daily_indicator_updates_on_session_close():
    from dqengine.runtime.algorithm import QCAlgorithm
    from dqengine.runtime.backtester import PyBacktester
    from conftest_helpers import two_day_store

    class A(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 25)
            self.set_cash(1000)
            self.sym = self.add_equity("TQQQ").symbol
            self.s = self.sma(self.sym, 2)

    algo = A()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    # session closes were 102 and 105
    assert algo.s.is_ready and abs(algo.s.current.value - 103.5) < 1e-9


def test_algorithm_minute_indicator_updates_every_bar():
    from dqengine.runtime.algorithm import QCAlgorithm
    from dqengine.runtime.backtester import PyBacktester
    from dqengine.runtime.enums import Resolution
    from conftest_helpers import two_day_store

    class A(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 24)
            self.set_cash(1000)
            self.sym = self.add_equity("TQQQ").symbol
            self.s = self.sma(self.sym, 3, Resolution.MINUTE)

    algo = A()
    PyBacktester(algo, two_day_store()).run()
    # minute closes on 8/24 were 100, 101, 102
    assert algo.s.is_ready and abs(algo.s.current.value - 101.0) < 1e-9
