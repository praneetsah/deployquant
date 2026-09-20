"""The run loop as sessions: _setup / _begin_session / _step_bar /
_end_session / _result must reproduce `_run` exactly.

Behavioural, not textual: the same days are driven two ways — by `_run`,
and by a hand-written driver that walks the store itself and calls the
session methods — and fills, orders, equity and the resting book have to
be identical. Run under DQENGINE_FAST_PATH=0 and =1: under =1 the batch run may
take the quiet-bar fast path, which the driver never calls, so equality
there is the fast path's own parity promise restated through the split.

The store is two-symbol and ragged on purpose — one symbol has fewer bars
than the other on some days and none on one day — because a merged
multi-symbol timeline has two entries at one timestamp, and one bar index
cannot express that.
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime.core.data import DayBars                       # noqa: E402
from dqengine.runtime.algorithm import QCAlgorithm             # noqa: E402
from dqengine.runtime.backtester import PyBacktester           # noqa: E402

OPEN_MS = 9 * 3600_000 + 30 * 60_000
SPAN = 60_000


def _rw_day(day, rng, p0, n):
    steps = rng.normal(0, 0.25, n)
    c = np.maximum(1.0, p0 + np.cumsum(steps))
    o = np.concatenate([[p0], c[:-1]])
    h = np.maximum(o, c) + rng.uniform(0, 0.5, n)
    l = np.minimum(o, c) - rng.uniform(0, 0.5, n)
    return DayBars(day=day,
                   start_ms=np.array([OPEN_MS + i * SPAN for i in range(n)]),
                   open=o, high=h, low=l, close=c, volume=np.ones(n)), float(c[-1])


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class RaggedStore:
    """Per-symbol minute store. A day in the calendar with no bars for a
    symbol loads as None (a real empty zip)."""

    def __init__(self, per_sym):
        self.per_sym = per_sym

    def minute_days(self, sym):
        return sorted(set().union(*[set(m) for m in self.per_sym.values()]))

    def load_minute_day(self, sym, day):
        return self.per_sym.get(sym, {}).get(day)


DAYS = _weekdays(date(2026, 3, 2), 6)


def build_store(seed=7):
    rng = np.random.default_rng(seed)
    a, b = {}, {}
    pa, pb = 100.0, 40.0
    for k, d in enumerate(DAYS):
        a[d], pa = _rw_day(d, rng, pa, 390)
        if k == 3:
            continue                       # B absent: carried for B only
        n = 200 if k in (1, 4) else 390    # B ends early on two days
        b[d], pb = _rw_day(d, rng, pb, n)
    return RaggedStore({"AAA": a, "BBB": b})


class _Base(QCAlgorithm):
    def initialize(self):
        self.set_start_date(DAYS[1].year, DAYS[1].month, DAYS[1].day)
        self.set_end_date(DAYS[-1].year, DAYS[-1].month, DAYS[-1].day)
        self.set_cash(50_000)
        self.set_warm_up(1)
        self.a = self.add_equity("AAA").symbol
        self.b = self.add_equity("BBB").symbol
        self.schedule.on(self.date_rules.every_day(self.a),
                         self.time_rules.after_market_open(self.a, 30),
                         self.morning)
        self.schedule.on(self.date_rules.every_day(self.a),
                         self.time_rules.before_market_close(self.a, 5),
                         self.evening)
        self.n_morning = 0

    def morning(self):
        self.n_morning += 1
        pa, pb = self.securities[self.a].price, self.securities[self.b].price
        # some of these fill, some rest — a book with levels in it at the end
        self.limit_order(self.a, 10, round(pa * 0.998, 2), tag="a-lim")
        self.limit_order(self.b, -15, round(pb * 1.002, 2), tag="b-lim")
        self.stop_market_order(self.a, -5, round(pa * 0.97, 2), tag="a-stop")
        self.limit_order(self.b, 20, round(pb * 0.90, 2), tag="b-far")
        if self.n_morning % 2 == 0:
            self.market_order(self.b, 7, tag="b-mkt")

    def evening(self):
        if self.portfolio[self.a].quantity > 40:
            self.market_order(self.a, -20, tag="a-trim")


class Quiet(_Base):
    """No on_data: fast-path eligible under DQENGINE_FAST_PATH=1."""


class Busy(_Base):
    """on_data does work: never fast-path eligible; every bar is stepped."""

    def initialize(self):
        super().initialize()
        self.bars_seen = 0

    def on_data(self, data):
        self.bars_seen += 1
        if self.is_warming_up:
            return
        if self.b in data and data[self.b].close < self.securities[self.a].price * 0.4 \
                and self.bars_seen % 97 == 0:
            self.market_order(self.a, 3, tag="a-od")


def _levels(bt):
    return [(t.symbol, t.quantity, t.order_type.value, t.limit_price, t.stop_price,
             t.quantity_filled, t.status.name)
            for t in bt.algo._book._open]


def _drive_by_session(algo, store):
    """What a live driver will do: set up once, then per day begin, step
    each closed bar-end with the entries of EVERY symbol ending there, end.
    The timeline is built from the store, not from the engine — except a
    symbol the store has no bars for that day, which the session begins
    as a fill-forward stream the driver steps like any other."""
    bt = PyBacktester(algo, store)
    days = bt._setup()
    for day in days:
        bt._begin_session(day)
        if not bt._day_bars:
            continue                        # nothing at all for this day
        ends = {}
        for s in list(algo.securities):
            b = store.load_minute_day(s, day)
            if b is None:
                b = bt._day_bars.get(s)
                if b is None:
                    continue
            for i in range(b.n):
                ends.setdefault(int(b.start_ms[i]) + SPAN, []).append((s, i))
        for t in sorted(ends):
            bt._step_bar(day, t, ends[t])
            if algo._quit:
                break
        bt._end_session(day)
        if algo._quit:
            break
    return bt, bt._result()


@pytest.mark.parametrize("fast", ["0", "1"])
@pytest.mark.parametrize("cls", [Quiet, Busy])
def test_driver_over_session_methods_equals_the_batch_run(cls, fast, monkeypatch):
    monkeypatch.setenv("DQENGINE_FAST_PATH", fast)
    store = build_store()

    ref_bt = PyBacktester(cls(), store)
    ref = ref_bt.run()
    assert "error" not in ref, ref.get("error", {}).get("traceback", "")[:3000]
    if fast == "1" and cls is Quiet:
        assert ref["fast_path"]["sessions"] > 0     # the batch run really fast-pathed

    got_bt, got = _drive_by_session(cls(), store)

    # the run did something worth comparing
    assert ref["fills"]
    assert _levels(ref_bt), "expected resting levels at the end"
    assert len(ref["equity"]) == len(DAYS) - 1           # warm-up day unmarked

    assert got["fills"] == ref["fills"]
    assert got["orders"] == ref["orders"]
    assert got["equity"] == ref["equity"]
    assert got["equity_days"] == ref["equity_days"]
    assert got["position"] == ref["position"]
    assert _levels(got_bt) == _levels(ref_bt)
    assert got["logs"] == ref["logs"]


def test_two_symbols_at_one_timestamp_are_one_step(monkeypatch):
    """A merged timeline: both symbols' bars end at 9:31 and both must
    reach on_data in the SAME slice, in one _step_bar call."""
    monkeypatch.setenv("DQENGINE_FAST_PATH", "0")
    store = build_store()
    algo = Busy()
    seen = []
    algo.on_data = lambda data: seen.append(sorted(data.bars))
    bt = PyBacktester(algo, store)
    days = bt._setup()
    day = days[1]
    bt._begin_session(day)
    bt._step_bar(day, OPEN_MS + SPAN, [("AAA", 0), ("BBB", 0)])
    assert seen[-1] == ["AAA", "BBB"]
    assert algo.time.hour == 9 and algo.time.minute == 31
