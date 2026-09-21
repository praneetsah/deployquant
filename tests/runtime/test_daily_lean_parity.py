"""Daily-resolution parity with LEAN: what a scheduled event sees, and what
a market order placed from one becomes.

A strategy whose every subscription is daily gets ONE bar per session, and
that bar lands at the close. LEAN still runs a check scheduled for 15:45 at
15:45, on the previous session's close, and converts any market order that
check places: market-on-close while there is more than 15.5 minutes of
session left, market-on-open after that. This module pins both halves
against recordings from the LEAN CLI.

The recordings are in fixtures/lean_daily/ (see the README there for how
they were produced). Each case below runs the same algorithm on this engine
and compares, to the cent: every scheduled callback's (time, price seen,
indicator value, is_ready), every order's converted type, and every fill's
(time, symbol, quantity, price).
"""
import json
import os

import pytest

from dqengine.config import data_root
from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.core.data import DataStore
from dqengine.runtime.enums import OrderStatus, Resolution

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "lean_daily")
DATA = data_root()


def _has_daily(*syms):
    return all(os.path.exists(os.path.join(DATA, "equity", "usa", "daily",
                                           f"{s.lower()}.zip")) for s in syms)


needs_daily = pytest.mark.skipif(
    not _has_daily("spy", "sgov"),
    reason=f"needs the shipped SPY and SGOV daily files under {DATA}")


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".json")) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- recorders

class Recorder(QCAlgorithm):
    """Common shell: the probe strategies below record what LEAN's probes
    recorded, in the same shape, so a comparison is a list == list."""
    SYMS = ("SPY", "SGOV")
    WARMUP = 120
    CASH = 100000

    def _setup_common(self, start, end):
        self.set_start_date(*start)
        self.set_end_date(*end)
        self.set_cash(self.CASH)
        self.rows = []
        self.orders_seen = []
        self.fills_seen = []
        self.syms = {}
        self.inds = {}
        for s in self.SYMS:
            self.syms[s] = self.add_equity(s, Resolution.DAILY).symbol
            self.inds[s] = self.sma(self.syms[s], 5, Resolution.DAILY)
        if self.WARMUP:
            self.set_warm_up(self.WARMUP, Resolution.DAILY)

    def _schedule(self, times):
        spy = self.syms["SPY"]
        for name, minutes in times:
            rule = (self.time_rules.after_market_open(spy, -minutes)
                    if minutes < 0 else
                    self.time_rules.before_market_close(spy, minutes))
            self.schedule.on(self.date_rules.every_day(spy), rule,
                             self._cb(name))

    def _cb(self, name):
        def run():
            self.at(name)
        return run

    def check(self, where):
        if self.is_warming_up:
            return        # the fixtures drop LEAN's warm-up rows
        row = {"at": where, "time": str(self.time)}
        for s in self.SYMS:
            ind = self.inds[s]
            row[s] = {"price": round(float(self.securities[self.syms[s]].price), 4),
                      "sma": round(float(ind.current.value), 6),
                      "ready": bool(ind.is_ready)}
        self.rows.append(row)

    def on_order_event(self, e):
        t = self.transactions.get_order_by_id(e.order_id)
        if e.status == OrderStatus.SUBMITTED:
            self.orders_seen.append({"tag": t.tag, "time": str(self.time),
                                     "sym": t.symbol,
                                     "type": t.order_type.value})
        elif e.status == OrderStatus.FILLED:
            self.fills_seen.append({"tag": t.tag, "time": str(self.time),
                                    "sym": t.symbol,
                                    "qty": int(e.fill_quantity),
                                    "price": round(float(e.fill_price), 4)})


FIVE = [("open30", -30), ("c60", 60), ("c20", 20), ("c16", 16), ("c15", 15)]


class ChecksMarch(Recorder):
    def initialize(self):
        self._setup_common((2025, 3, 3), (2025, 3, 14))
        self._schedule(FIVE)

    def at(self, name):
        self.check(name)

    def on_data(self, data):
        self.check("ondata")


class OrdersMarket(Recorder):
    SYMS = ("SPY",)
    CASH = 1000000

    def initialize(self):
        self._setup_common((2025, 3, 3), (2025, 3, 14))
        self._schedule(FIVE)

    def at(self, name):
        if self.is_warming_up:
            return
        self.market_order(self.syms["SPY"], 1, tag="mkt_" + name)

    def on_data(self, data):
        if self.is_warming_up:
            return
        self.market_order(self.syms["SPY"], 1, tag="mkt_ondata")


class OrdersMoc(OrdersMarket):
    def at(self, name):
        if self.is_warming_up:
            return
        self.market_on_close_order(self.syms["SPY"], 1, tag="moc_" + name)

    def on_data(self, data):
        if self.is_warming_up:
            return
        self.market_on_close_order(self.syms["SPY"], 1, tag="moc_ondata")


class Boundary(Recorder):
    """Exactly on the buffer and one second inside it."""
    SYMS = ("SPY",)
    CASH = 1000000

    def initialize(self):
        self._setup_common((2025, 3, 3), (2025, 3, 14))
        spy = self.syms["SPY"]
        for name, (h, m, s) in (("on", (15, 44, 30)), ("in", (15, 44, 31))):
            self.schedule.on(self.date_rules.every_day(spy),
                             self.time_rules.at(h, m, s), self._cb(name))

    def at(self, name):
        if self.is_warming_up:
            return
        self.market_order(self.syms["SPY"], 1, tag="mkt_" + name)
        self.market_on_close_order(self.syms["SPY"], 1, tag="moc_" + name)


class Combined(Recorder):
    """Checks and orders together, over a window with a holiday and an
    early close in it."""
    CASH = 1000000
    START = (2025, 6, 27)
    END = (2025, 7, 9)

    def initialize(self):
        self._setup_common(self.START, self.END)
        self._schedule(FIVE)

    def at(self, name):
        self.check(name)
        if self.is_warming_up:
            return
        self.market_order(self.syms["SPY"], 1, tag="mkt_" + name)
        if name in ("c20", "c15"):
            self.market_on_close_order(self.syms["SGOV"], 1, tag="moc_" + name)

    def on_data(self, data):
        self.check("ondata")
        if self.is_warming_up:
            return
        self.market_order(self.syms["SGOV"], 1, tag="mkt_ondata")


class CombinedThanks(Combined):
    START = (2025, 11, 21)
    END = (2025, 12, 3)


class CombinedTwoSym(Combined):
    """No warm-up, and SGOV's first bar falls inside the window: the run
    opens with neither name priced."""
    WARMUP = 0
    START = (2020, 5, 20)
    END = (2020, 6, 5)


class Sizing(Recorder):
    """set_holdings and liquidate from the two checks either side of the
    15.5-minute buffer."""
    def initialize(self):
        self._setup_common((2025, 3, 3), (2025, 3, 21))
        self._schedule([("c20", 20), ("c15", 15)])
        self.n = 0

    def snap(self, where):
        row = {"at": where, "time": str(self.time),
               "tpv": round(float(self.portfolio.total_portfolio_value), 4),
               "cash": round(float(self.portfolio.cash), 4)}
        for s in self.SYMS:
            sym = self.syms[s]
            row[s] = {"price": round(float(self.securities[sym].price), 4),
                      "held": int(self.portfolio[sym].quantity),
                      "calc_half": int(self.calculate_order_quantity(sym, 0.5))}
        self.rows.append(row)

    def at(self, name):
        if self.is_warming_up:
            return
        self.snap(name)
        if name == "c20":
            if self.n % 2 == 0:
                self.set_holdings(self.syms["SPY"], 0.5, tag="sh_spy_half")
            else:
                self.set_holdings(self.syms["SPY"], 0.0, tag="sh_spy_zero")
        else:
            if self.n % 2 == 0:
                self.set_holdings(self.syms["SGOV"], 0.3, tag="sh_sgov_30")
            else:
                self.liquidate(self.syms["SGOV"], tag="liq_sgov")
            self.n += 1

    def on_data(self, data):
        if self.is_warming_up:
            return
        self.snap("ondata")


class UserLike(Recorder):
    """The shape that exposed the look-ahead: daily data on several names, a
    long warm-up, one check 15 minutes before the close, a rebalance counter
    that starts ready, weights across the names."""
    PERIOD = 30

    def initialize(self):
        self._setup_common((2025, 1, 2), (2025, 12, 31))
        for s in self.SYMS:
            self.inds[s] = self.roc(self.syms[s], 60, Resolution.DAILY)
        self.days = self.PERIOD
        self._schedule([("rebalance", 15)])

    def place(self, s, weight, tpv):
        self.set_holdings(self.syms[s], weight, tag="w_" + s)

    def at(self, name):
        if self.is_warming_up:
            return
        self.days += 1
        if self.days < self.PERIOD:
            return
        self.days = 0
        ranked = sorted(self.SYMS,
                        key=lambda s: float(self.inds[s].current.value),
                        reverse=True)
        weights = {ranked[0]: 0.7, ranked[1]: 0.3}
        tpv = float(self.portfolio.total_portfolio_value)
        self.rows.append({
            "at": "rebalance", "time": str(self.time), "tpv": round(tpv, 4),
            "mom": {s: round(float(self.inds[s].current.value), 6)
                    for s in self.SYMS},
            "price": {s: round(float(self.securities[self.syms[s]].price), 4)
                      for s in self.SYMS}})
        for s in self.SYMS:
            self.place(s, weights[s], tpv)

    def on_end_of_algorithm(self):
        self.rows.append({
            "at": "final",
            "tpv": round(float(self.portfolio.total_portfolio_value), 4),
            "cash": round(float(self.portfolio.cash), 4),
            "holdings": {s: int(self.portfolio[self.syms[s]].quantity)
                         for s in self.SYMS}})


class UserExplicit(UserLike):
    """The same rebalance, sized by the strategy itself. set_holdings rounds
    a partial adjustment differently here and on LEAN (see
    test_set_holdings_rounds_a_partial_adjustment_differently), which is a
    difference at every resolution and not this change's to make. Taking the
    share count out of the comparison leaves exactly what is: when the check
    runs, what it sees, what its orders become and when they fill."""

    def initialize(self):
        super().initialize()
        for s in self.SYMS:
            self.securities[self.syms[s]].set_leverage(2)

    def place(self, s, weight, tpv):
        sym = self.syms[s]
        px = float(self.securities[sym].price)
        if px <= 0:
            return
        delta = int(weight * tpv / px) - int(self.portfolio[sym].quantity)
        if delta != 0:
            self.market_order(sym, delta, tag="w_" + s)


# ------------------------------------------------------------------- runner

def run(cls):
    algo = cls()
    res = PyBacktester(algo, DataStore(DATA)).run()
    assert "error" not in res, res.get("error")
    return algo, res


def _drop(rows, *keys):
    out = []
    for r in rows:
        r = {k: v for k, v in r.items() if k not in keys}
        for k, v in list(r.items()):
            if isinstance(v, dict):
                r[k] = {kk: vv for kk, vv in v.items() if kk not in keys}
        out.append(r)
    return out


def compare(algo, name, *, checks=True, orders=True, fills=True, drop=()):
    fx = fixture(name)
    if checks:
        assert _drop(algo.rows, *drop) == _drop(fx["checks"], *drop)
    if orders:
        want = [o for o in fx["orders"] if not o.get("canceled")]
        got = [dict(o) for o in algo.orders_seen]
        for o in want:
            o.pop("qty", None)
        assert got == want
    if fills:
        assert algo.fills_seen == fx["fills"]


# -------------------------------------------------------------------- cases

@needs_daily
def test_scheduled_checks_see_the_previous_session():
    """Every check before the close reads the previous session's close and
    an indicator fed through the previous session, as LEAN's does."""
    algo, _ = run(ChecksMarch)
    compare(algo, "checks_march", orders=False, fills=False)


@needs_daily
def test_market_orders_convert_on_the_buffer():
    """More than 15.5 minutes of session left, a market order becomes
    market-on-close and fills at that session's close; inside the buffer it
    becomes market-on-open and fills at the next session's open."""
    algo, _ = run(OrdersMarket)
    compare(algo, "orders_market", checks=False)


@needs_daily
def test_market_on_close_inside_the_buffer_is_invalid():
    """LEAN refuses a market-on-close order placed inside the buffer and
    creates no order at all; one placed after the close belongs to the next
    session."""
    algo, _ = run(OrdersMoc)
    compare(algo, "orders_moc", checks=False)


@needs_daily
def test_the_buffer_boundary_itself():
    """00:15:30 before the close is still outside the buffer: at 15:44:30 a
    market-on-close order is accepted and a market order becomes one. One
    second later both go the other way."""
    algo, _ = run(Boundary)
    compare(algo, "boundary", checks=False)


@needs_daily
def test_early_close_and_holiday_july():
    """On 2025-07-03 the session ends at 13:00: the checks move with it and
    the buffer is measured from 13:00. The 07-04 holiday sends the
    market-on-open fill to 07-07."""
    algo, _ = run(Combined)
    compare(algo, "combined_july")


@needs_daily
def test_early_close_and_holiday_thanksgiving():
    algo, _ = run(CombinedThanks)
    compare(algo, "combined_thanks")


@needs_daily
def test_two_symbols_with_different_first_bars():
    """A symbol with no bar yet has price 0, and LEAN places no order for
    it. SGOV's first bar is 2020-05-28."""
    algo, _ = run(CombinedTwoSym)
    compare(algo, "combined_twosym")


@needs_daily
def test_set_holdings_sizes_off_the_price_the_strategy_can_see():
    """A check at 15:40 sizes on the previous close, and the order it places
    fills at this session's close; one at 15:45 sizes on the same price and
    fills at the next session's open. Cash and holdings at every snapshot
    follow. `calc_half` is out of the comparison: see the case below."""
    algo, _ = run(Sizing)
    compare(algo, "sizing", drop=("calc_half",))


@needs_daily
def test_user_shaped_rebalance_matches_lean():
    """The demonstration for the owner: the pattern the report came from,
    fill for fill."""
    algo, _ = run(UserExplicit)
    compare(algo, "userexplicit")


@needs_daily
def test_user_shaped_rebalance_with_set_holdings_agrees_on_timing_and_prices():
    """The same strategy sized with set_holdings. Every rebalance runs at
    15:45 on the previous session's prices, and every order is
    market-on-open, as on LEAN. Share counts drift from LEAN's after the
    first partial adjustment, for the reason the next case pins."""
    algo, _ = run(UserLike)
    fx = fixture("userlike")
    assert ([r["time"] for r in algo.rows if r["at"] == "rebalance"]
            == [r["time"] for r in fx["checks"] if r["at"] == "rebalance"])
    assert ([r["price"] for r in algo.rows if r["at"] == "rebalance"]
            == [r["price"] for r in fx["checks"] if r["at"] == "rebalance"])
    assert {o["type"] for o in algo.orders_seen} == {"market_on_open"}
    # the look-ahead is gone either way: the first rebalance is identical,
    # and it is the one no earlier rounding can have touched
    assert algo.orders_seen[:2] == [
        {"tag": "w_SPY", "time": "2025-01-02 15:45:00", "sym": "SPY",
         "type": "market_on_open"},
        {"tag": "w_SGOV", "time": "2025-01-02 15:45:00", "sym": "SGOV",
         "type": "market_on_open"}]
    assert algo.fills_seen[:2] == fx["fills"][:2]


@needs_daily
def test_set_holdings_rounds_a_partial_adjustment_differently():
    """A known difference, at every resolution, that this change does not
    touch: LEAN sizes set_holdings so the resulting HOLDING is the largest
    that does not exceed the target value, and this engine truncates the
    delta instead. Measured on LEAN 2026-09-21: holding 121 SPY at 599.41
    against a 71948.53 target, LEAN sends -1 (120 x 599.41 = 71929.20 fits,
    121 does not) and this engine sends 0, because the delta is -0.97 of a
    share. calculate_order_quantity, a third answer again, returns LEAN's
    own -1 here and 0 where LEAN's set_holdings would send -1. Changing
    either would move minute-resolution and live sizing, so it is the
    owner's call, not this commit's."""
    fx = fixture("userlike")
    first = [o for o in fx["orders"] if o["time"] == "2025-02-18 15:45:00"]
    assert [o["qty"] for o in first] == [-1, 7]

    algo, _ = run(UserLike)
    same_day = [o for o in algo.orders_seen
                if o["time"] == "2025-02-18 15:45:00"]
    assert [o["sym"] for o in same_day] == ["SGOV"]      # LEAN: SPY, SGOV


# ------------------------------------------------------------- the gate
#
# Everything above is gated on daily mode. A minute-resolution backtest is
# outside it and may not move. A live run in daily mode is INSIDE it as of
# phase 2 -- the owner's decision is that a daily deployment fills where its
# backtest fills -- and it needs the driver's clock to be there.

class Scheduled(QCAlgorithm):
    RES = Resolution.MINUTE

    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 24)
        self.set_cash(100000)
        self.sym = self.add_equity("TQQQ", self.RES).symbol
        self.seen = []
        self.schedule.on(self.date_rules.every_day(),
                         self.time_rules.before_market_close(self.sym, 15),
                         self.at_close_15)

    def at_close_15(self):
        self.seen.append((str(self.time), self._prices.get("TQQQ", 0.0)))
        self.market_order(self.sym, 1, tag="check")


def _minute_session(day, close_ms):
    """One minute bar a minute from 09:30 to the close, close = 100 + i."""
    from conftest_helpers import OPEN_MS, np
    from dqengine.runtime.core.data import DayBars
    n = (close_ms - OPEN_MS) // 60_000
    c = np.array([100.0 + i for i in range(n)])
    return DayBars(day=day,
                   start_ms=np.array([OPEN_MS + i * 60_000 for i in range(n)]),
                   open=c - 0.5, high=c + 1.0, low=c - 1.0, close=c,
                   volume=np.ones(n))


def test_minute_resolution_is_untouched():
    """A minute strategy with the same schedule: the check still runs on the
    15:45 bar it has just been shown, and its market order still fills there
    and then, at that bar's close. Nothing about the daily rules reaches it."""
    from datetime import date

    from conftest_helpers import SynthStore

    day = date(2026, 8, 24)
    algo = Scheduled()
    res = PyBacktester(algo, SynthStore({day: _minute_session(day, 57_600_000)})).run()
    assert "error" not in res, res.get("error")
    # 15:45 is the bar ending at 15:45, whose close is the 375th step
    assert algo.seen == [("2026-08-24 15:45:00", 474.0)]
    assert [(f["day"], f["ms"], f["px"]) for f in res["fills"]] == [
        ("2026-08-24", 56_700_000, 474.0)]
    assert [o["kind"] for o in res["orders"]] == ["market"]


def _live_daily_days():
    from datetime import date

    from conftest_helpers import synth_day
    return {date(2026, 8, 24): synth_day(date(2026, 8, 24), [100, 101, 102]),
            date(2026, 8, 25): synth_day(date(2026, 8, 25), [103, 104, 105])}


def test_a_live_daily_run_without_a_clock_is_refused():
    """The old rule ran a daily live check after the bar, at the close, and
    filled its market order there. Falling back to that silently would make
    a deployment's fills stop matching its backtest, so a live daily run
    with no clock does not run at all."""
    from conftest_helpers import SynthStore
    from dqengine.runtime.backtester import RunOverrides

    class Daily(Scheduled):
        RES = Resolution.DAILY

    res = PyBacktester(Daily(), SynthStore(_live_daily_days()),
                       overrides=RunOverrides(project_calendar=True)).run()
    assert "error" in res
    assert "live_today" in res["error"]["message"]


def test_a_live_daily_run_with_a_clock_follows_the_lean_order():
    """With the clock it is the backtest: the check runs at 15:45 on the
    previous session's close, and the market order it places converts to
    market-on-open and fills at the next session's open."""
    from datetime import date

    from conftest_helpers import SynthStore
    from dqengine.runtime.backtester import RunOverrides

    class Daily(Scheduled):
        RES = Resolution.DAILY

    algo = Daily()
    res = PyBacktester(
        algo, SynthStore(_live_daily_days()),
        overrides=RunOverrides(project_calendar=True,
                               end=date(2026, 8, 25),
                               live_today=date(2026, 8, 25),
                               live_now_ms=86_400_000)).run()
    assert "error" not in res, res.get("error")
    # 15:45, not 16:00, and on the previous session's close
    assert algo.seen == [("2026-08-24 15:45:00", 0.0),
                         ("2026-08-25 15:45:00", 102.0)]
    # 15:45 is inside the 15.5-minute buffer, so the market order becomes a
    # market-on-open for the NEXT session's open: it rests, it does not fill
    # at today's close, and the payload says which day placed it
    assert res["fills"] == []
    assert [(o["type"], o["created_day"])
            for o in res["position"]["open_orders"]] == [("market_on_open",
                                                          "2026-08-25")]
    assert res["daily_live"] == {"today": "2026-08-25", "bar_applied": True,
                                 "next_fire_ms": None}
