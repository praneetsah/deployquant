"""Quiet-bar fast path: A/B self-parity gates.

Every case runs twice — DQENGINE_FAST_PATH=1 and =0 — and asserts the ENTIRE
result (fills with prices/times, orders, equity to the bit, logs) is
identical, plus that the fast run actually engaged (fast_path.sessions>0).
The battery targets each edge the scanner must preserve: strict-breach
limits/stops, stop-limit's next-bar limit leg across a skip, same-bar
created-order exclusion, carried-day deferred markets, gappy multi-symbol
data, warm-up, mid-day minute-indicator registration (demotion), handlers
mutating orders from on_order_event, and seeded-random order storms.
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime.core.data import DayBars                       # noqa: E402
from dqengine.runtime.backtester import run_python_backtest    # noqa: E402

OPEN_MS = 9 * 3600_000 + 30 * 60_000
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()


def mk_day(day, o, h, l, c, v=None):
    o, h, l, c = (np.asarray(a, float) for a in (o, h, l, c))
    return DayBars(day=day,
                   start_ms=np.array([OPEN_MS + i * 60_000 for i in range(len(c))]),
                   open=o, high=h, low=l, close=c,
                   volume=np.ones(len(c)) if v is None else np.asarray(v, float))


def rw_day(day, rng, p0, n=390):
    steps = rng.normal(0, 0.2, n)
    c = np.maximum(1.0, p0 + np.cumsum(steps))
    o = np.concatenate([[p0], c[:-1]])
    h = np.maximum(o, c) + rng.uniform(0, 0.4, n)
    l = np.minimum(o, c) - rng.uniform(0, 0.4, n)
    return mk_day(day, o, h, l, c), float(c[-1])


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class MultiStore:
    """Per-symbol synthetic minute store. carried_days appear in the
    calendar (like a real empty zip) but load as None."""

    def __init__(self, per_sym, carried_days=()):
        self.per_sym = per_sym
        self.carried = set(carried_days)

    def minute_days(self, sym):
        return sorted(set(self.per_sym.get(sym, {})) | self.carried)

    def load_minute_day(self, sym, day):
        return self.per_sym.get(sym, {}).get(day)


def run_ab(code, store, monkeypatch, expect_partial=False):
    res = {}
    for mode, env in (("fast", "1"), ("slow", "0")):
        monkeypatch.setenv("DQENGINE_FAST_PATH", env)
        r = run_python_backtest(code, data_root="",
                                overrides={"store": store})
        assert "error" not in r, r["error"].get("traceback", "")[:3000]
        res[mode] = r
    fp = res["fast"].pop("fast_path")
    res["slow"].pop("fast_path")
    assert fp["sessions"] > 0, f"fast path never engaged: {fp}"
    if expect_partial:
        assert fp["sessions"] < fp["of"], f"expected a demoted session: {fp}"
    for key in ("fills", "orders", "equity", "equity_days", "logs", "stats"):
        assert res["fast"][key] == res["slow"][key], f"{key} diverged"
    assert res["fast"] == res["slow"]
    return res["fast"], fp


# --------------------------------------------------------------- stores

def trending_store(n_days=12, syms=("AAA",)):
    days = weekdays(date(2026, 1, 5), n_days)
    per = {}
    for k, s in enumerate(syms):
        rng = np.random.default_rng(1234 + k)
        px, m = 100.0 + 10 * k, {}
        for d in days:
            m[d], px = rw_day(d, rng, px)
        per[s] = m
    return MultiStore(per), days


# --------------------------------------------------------------- cases

def test_limits_stops_and_stoplimits(monkeypatch):
    store, days = trending_store()
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 10),
                         self.act)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.before_market_close(self.sym, 5),
                         self.wind)

    def act(self):
        px = self.securities[self.sym].price
        self.limit_order(self.sym, 10, px * 0.999)
        self.limit_order(self.sym, -10, px * 1.001)
        self.stop_market_order(self.sym, 5, px * 1.002)
        self.stop_market_order(self.sym, -5, px * 0.998)
        self.stop_limit_order(self.sym, 8, px * 1.001, px * 0.9995)
        self.stop_limit_order(self.sym, -8, px * 0.999, px * 1.0005)

    def wind(self):
        for t in self.transactions.get_open_orders():
            t.cancel()
        self.log(f"eod open={{len(self.transactions.get_open_orders())}} "
                 f"qty={{self.portfolio[self.sym].quantity}}")
'''
    res, fp = run_ab(code, store, monkeypatch)
    assert res["stats"]["fills"] > 10
    assert fp["sessions"] == fp["of"]


def test_same_bar_exclusion_and_order_event_cascade(monkeypatch):
    store, days = trending_store()
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 30),
                         self.act)

    def act(self):
        px = self.securities[self.sym].price
        # a limit the CURRENT bar would already breach — must wait a bar
        t = self.limit_order(self.sym, 10, px * 1.5, tag="deep")
        # and an update in the same handler (update keeps created time)
        t.update(UpdateOrderFields(limit_price=px * 0.9998))

    def on_order_event(self, e):
        if e.status == OrderStatus.FILLED and e.fill_quantity > 0:
            self.limit_order(e.symbol, -e.fill_quantity, e.fill_price * 1.001,
                             tag="tp")
'''
    run_ab(code, store, monkeypatch)


def test_carried_day_deferred_market(monkeypatch):
    store, days = trending_store()
    carried = days[5]
    del store.per_sym["AAA"][carried]
    store.carried.add(carried)
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 60),
                         self.act)

    def act(self):
        px = self.securities[self.sym].price
        self.market_order(self.sym, 7, tag=f"d{{self.time.date()}}")
        self.limit_order(self.sym, -3, px * 1.0005)
'''
    res, _ = run_ab(code, store, monkeypatch)
    # the carried day's market order fills on the NEXT real session
    tags = {f["day"]: True for f in res["fills"]}
    assert carried.isoformat() not in tags


def test_gappy_multi_symbol(monkeypatch):
    store, days = trending_store(syms=("AAA", "BBB"))
    # BBB simply has no data on two mid-range days (real gap, not carried)
    del store.per_sym["BBB"][days[4]]
    del store.per_sym["BBB"][days[7]]
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.a = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.b = self.add_equity("BBB", Resolution.MINUTE).symbol
        self.schedule.on(self.date_rules.every_day(self.a),
                         self.time_rules.after_market_open(self.a, 45),
                         self.act)

    def act(self):
        for sym in (self.a, self.b):
            px = self.securities[sym].price
            if px > 0:
                self.limit_order(sym, 5, px * 0.9995)
                self.stop_market_order(sym, -5, px * 0.997)
'''
    run_ab(code, store, monkeypatch)


def test_warmup_and_on_end_of_day(monkeypatch):
    store, days = trending_store()
    start = days[4]
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({start.year}, {start.month}, {start.day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.set_warm_up(3)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.trend = self.sma(self.sym, 3)
        self._tried = False
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.before_market_close(self.sym, 10),
                         self.act)

    def act(self):
        if self.trend.is_ready and not self.portfolio[self.sym].invested:
            self.set_holdings(self.sym, 0.5)

    def on_end_of_day(self, symbol=None):
        self.log(f"eod {{self.time.date()}} px={{self.securities['AAA'].price:.2f}}")
        if not self._tried:
            self._tried = True   # fires on a warm-up day -> refused, logged
            self.market_order("AAA", 1, tag="too-early")
'''
    res, fp = run_ab(code, store, monkeypatch)
    assert any("refused" in ln for ln in res["logs"])
    assert fp["sessions"] == fp["of"]


def test_midday_minute_indicator_demotes(monkeypatch):
    store, days = trending_store()
    trigger = days[6]
    code = f'''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.fast_sma = None
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 15),
                         self.act)

    def act(self):
        if self.time.date() == datetime({trigger.year}, {trigger.month}, {trigger.day}).date() \\
                and self.fast_sma is None:
            self.fast_sma = self.sma(self.sym, 5, Resolution.MINUTE)
        if self.fast_sma is not None and self.fast_sma.is_ready:
            self.log(f"{{self.time.date()}} sma5={{self.fast_sma.current.value:.4f}}")
            px = self.securities[self.sym].price
            if px > self.fast_sma.current.value:
                self.limit_order(self.sym, 5, px * 0.9995)
'''
    res, fp = run_ab(code, store, monkeypatch, expect_partial=True)
    assert any("sma5=" in ln for ln in res["logs"])


@pytest.mark.parametrize("seed", [7, 21, 63])
def test_random_order_storm(monkeypatch, seed):
    store, days = trending_store(n_days=15, syms=("AAA", "BBB"))
    code = f'''
from AlgorithmImports import *
import random

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date({days[0].year}, {days[0].month}, {days[0].day})
        self.set_end_date({days[-1].year}, {days[-1].month}, {days[-1].day})
        self.set_cash(100000)
        self.a = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.b = self.add_equity("BBB", Resolution.MINUTE).symbol
        self.rng = random.Random({seed})
        for minutes in (5, 95, 200, 370):
            self.schedule.on(self.date_rules.every_day(self.a),
                             self.time_rules.after_market_open(self.a, minutes),
                             self.act)

    def act(self):
        rng = self.rng
        for sym in (self.a, self.b):
            px = self.securities[sym].price
            if px <= 0:
                continue
            r = rng.random()
            qty = rng.choice([-30, -10, 10, 30])
            off = 1 + rng.uniform(-0.01, 0.01)
            if r < 0.2:
                self.market_order(sym, qty)
            elif r < 0.45:
                self.limit_order(sym, qty, px * off)
            elif r < 0.7:
                self.stop_market_order(sym, qty, px * off)
            elif r < 0.85:
                self.stop_limit_order(sym, qty, px * off,
                                      px * (off + rng.uniform(-0.005, 0.005)))
            elif r < 0.95:
                for t in self.transactions.get_open_orders():
                    if rng.random() < 0.5:
                        t.cancel()
            else:
                self.liquidate()

    def on_order_event(self, e):
        if e.status == OrderStatus.FILLED and self.rng.random() < 0.15:
            self.limit_order(e.symbol, -e.fill_quantity, e.fill_price * 1.002,
                             tag="cascade")
'''
    res, _ = run_ab(code, store, monkeypatch)
    assert res["stats"]["fills"] > 5


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "spy")),
    reason="local SPY minute data not present")
def test_real_spy_scheduled(monkeypatch):
    code = '''
from AlgorithmImports import *

class T(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 3, 28)
        self.set_cash(10000)
        self.set_warm_up(10)
        self.sym = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.trend = self.sma(self.sym, 5)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 30),
                         self.act)

    def act(self):
        if not self.trend.is_ready:
            return
        px = self.securities[self.sym].price
        if not self.portfolio[self.sym].invested and px > self.trend.current.value:
            q = int(self.portfolio.cash / px)
            if q > 0:
                self.limit_order(self.sym, q, px * 0.9995)
        elif self.portfolio[self.sym].invested and px < self.trend.current.value:
            for t in self.transactions.get_open_orders():
                t.cancel()
            self.liquidate(self.sym)
'''
    res = {}
    for mode, env in (("fast", "1"), ("slow", "0")):
        monkeypatch.setenv("DQENGINE_FAST_PATH", env)
        r = run_python_backtest(code, data_root=DATA)
        assert "error" not in r, r["error"].get("traceback", "")[:3000]
        res[mode] = r
    fp = res["fast"].pop("fast_path")
    res["slow"].pop("fast_path")
    assert fp["sessions"] > 0
    assert res["fast"] == res["slow"]
