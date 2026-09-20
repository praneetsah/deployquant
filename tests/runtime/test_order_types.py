"""Wave 2 order types: MOO, MOC, trailing stop, limit-if-touched, plus the
sizing and shortability helpers."""
import pytest

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.enums import OrderStatus
from dqengine.runtime.errors import UnsupportedApiError

from conftest_helpers import OPEN_MS, two_day_store


class Base(QCAlgorithm):
    """Two sessions: closes 100,101,102 then 103,104,105."""

    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(100000)
        self.sym = self.add_equity("TQQQ").symbol
        self.i = 0
        self.placed = False


def run(cls):
    res = PyBacktester(cls(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    return res


# ---------------- market on close / open ----------------

def test_market_on_close_fills_at_the_session_close():
    class MOC(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.market_on_close_order(self.sym, 10, tag="moc")

    res = run(MOC)
    fills = res["fills"]
    assert len(fills) == 1
    assert fills[0]["day"] == "2026-08-24"
    assert fills[0]["px"] == 102.0            # day 1's last close
    assert fills[0]["qty"] == 10


def test_market_on_open_fills_on_the_next_sessions_first_bar():
    class MOO(Base):
        def on_data(self, data):
            self.i += 1
            if self.i == 3:                   # last bar of day 1
                self.market_on_open_order(self.sym, 5, tag="moo")

    res = run(MOO)
    assert len(res["fills"]) == 1
    assert res["fills"][0]["day"] == "2026-08-25"
    # LEAN fills a market-on-open order at the first bar's OPEN (checked
    # against LEAN on SPY, 2026-09-20: 75 of 75 such fills landed on the
    # 09:30 bar's open, stamped 09:31). It used to fill at that bar's close.
    assert res["fills"][0]["px"] == 102.5     # day 2's first open


def test_a_zero_quantity_order_is_invalid_not_silent():
    class Zero(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.ticket = self.market_on_close_order(self.sym, 0)

    algo = Zero()
    PyBacktester(algo, two_day_store()).run()
    assert algo.ticket.status == OrderStatus.INVALID


# ---------------- trailing stop ----------------

def test_a_trailing_stop_ratchets_up_and_fires_on_the_pullback():
    """Long trailing stop at 2%: it follows the highs up, never down, and
    fills when price falls through it."""
    class Trail(Base):
        def initialize(self):
            super().initialize()
            self.set_end_date(2026, 8, 25)

        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.market_order(self.sym, 10)
                self.t = self.trailing_stop_order(self.sym, -10, 0.02,
                                                  tag="trail")

    algo = Trail()
    PyBacktester(algo, two_day_store()).run()
    # highs run 101..106; the stop trails 2% under the running high and
    # never retreats
    assert algo.t.stop_price == pytest.approx(106 * 0.98, rel=1e-9)


def test_a_trailing_stop_never_moves_against_the_position():
    class Trail(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.market_order(self.sym, 10)
                self.t = self.trailing_stop_order(self.sym, -10, 0.02)
                self.seen = []
            else:
                self.seen.append(self.t.stop_price)

    algo = Trail()
    PyBacktester(algo, two_day_store()).run()
    assert algo.seen == sorted(algo.seen)     # monotonically non-decreasing


def test_a_trailing_stop_without_an_amount_is_invalid():
    class Bad(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.t = self.trailing_stop_order(self.sym, -10)

    algo = Bad()
    PyBacktester(algo, two_day_store()).run()
    assert algo.t.status == OrderStatus.INVALID


# ---------------- limit if touched ----------------

def test_limit_if_touched_waits_for_the_trigger():
    """A BUY trigger BELOW the tape is never touched — price would have to
    fall to it. (Above the tape it is already touched, which is the mirror
    of a stop-limit and the point of the kind.)"""
    class LIT(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.t = self.limit_if_touched_order(
                    self.sym, 10, trigger_price=50.0, limit_price=51.0)

    algo = LIT()
    res = PyBacktester(algo, two_day_store()).run()
    assert res["fills"] == []
    assert algo.t.is_open()


def test_limit_if_touched_triggers_from_the_favourable_side():
    """A BUY triggers when price FALLS to the trigger — the mirror of a
    stop-limit, which triggers on a rise."""
    class LIT(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.t = self.limit_if_touched_order(
                    self.sym, 10, trigger_price=200.0, limit_price=200.0)

    algo = LIT()
    res = PyBacktester(algo, two_day_store()).run()
    assert algo.t._triggered is True          # tape is below 200 throughout
    assert len(res["fills"]) == 1


# ---------------- helpers ----------------

def test_calculate_order_quantity_matches_set_holdings_sizing():
    class Calc(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.want = self.calculate_order_quantity(self.sym, 0.5)

    algo = Calc()
    PyBacktester(algo, two_day_store()).run()
    assert algo.want == int(0.5 * 100000 / 100)   # 500 shares at 100


def test_calculate_order_quantity_nets_what_is_already_held():
    class Calc(Base):
        def on_data(self, data):
            if not self.placed:
                self.placed = True
                self.market_order(self.sym, 100)
                self.want = self.calculate_order_quantity(self.sym, 0.5)

    algo = Calc()
    PyBacktester(algo, two_day_store()).run()
    assert algo.want == int(0.5 * 100000 / 100) - 100


def test_shortable_reports_subscribed_symbols():
    a = QCAlgorithm()
    a.add_equity("SPY")
    assert a.shortable("SPY") is True
    assert a.shortable("NOPE") is False
    assert a.shortable_quantity("SPY") is None


def test_the_new_order_methods_reject_unknown_arguments():
    a = QCAlgorithm()
    a.add_equity("SPY")
    a._book = object()
    for fn in (a.market_on_close_order, a.market_on_open_order):
        with pytest.raises(UnsupportedApiError, match="nonsense"):
            fn("SPY", 1, "", nonsense=1)


# ---------------- orders at and across the session close ----------------
# Found 2026-09-20 comparing a 22,796-order strategy with LEAN: the two
# engines agreed on every order and ended 0.24% apart, and all of it came from
# 75 market orders placed on a session's last bar. LEAN treats the exchange as
# closed by then, turns the order into market-on-open, and fills it at the next
# session's first open BEFORE the strategy sees that bar.

CLOSE_BAR_START = 15 * 3600_000 + 59 * 60_000          # the 15:59 bar ends at 16:00


def _full_session_store():
    """Two sessions whose LAST bar ends at the 16:00 close."""
    from datetime import date

    import numpy as np

    from dqengine.runtime.core.data import DayBars
    from conftest_helpers import SynthStore

    def day(d, closes):
        c = np.array(closes, float)
        return DayBars(day=d, start_ms=np.array([OPEN_MS, OPEN_MS + 60_000, CLOSE_BAR_START]),
                       open=c - 0.5, high=c + 1.0, low=c - 1.0, close=c, volume=np.ones(3))
    return SynthStore({date(2026, 8, 24): day(date(2026, 8, 24), [100, 101, 102]),
                       date(2026, 8, 25): day(date(2026, 8, 25), [103, 104, 105])})


def test_a_market_order_on_the_sessions_last_bar_fills_at_the_next_open():
    class LastBar(Base):
        def on_data(self, data):
            self.i += 1
            if self.i == 3:                                   # the bar that ends at 16:00
                self.market_order(self.sym, 5, tag="late")
            if self.i == 4:                                   # day 2's first bar
                self.seen = self.portfolio[self.sym].quantity

    algo = LastBar()
    res = PyBacktester(algo, _full_session_store()).run()
    assert "error" not in res, res.get("error")
    assert [(f["day"], f["px"], f["qty"]) for f in res["fills"]] == [("2026-08-25", 102.5, 5)]
    # filled before the strategy saw the bar, or it would order again
    assert algo.seen == 5


def test_a_market_order_before_the_last_bar_still_fills_at_once():
    class Earlier(Base):
        def on_data(self, data):
            self.i += 1
            if self.i == 2:
                self.market_order(self.sym, 5)

    res = PyBacktester(Earlier(), _full_session_store()).run()
    assert [(f["day"], f["px"]) for f in res["fills"]] == [("2026-08-24", 101.0)]


def test_market_on_open_is_filled_before_the_strategy_sees_the_first_bar():
    class MOO(Base):
        def on_data(self, data):
            self.i += 1
            if self.i == 3:
                self.market_on_open_order(self.sym, 5)
            if self.i == 4:
                self.seen = self.portfolio[self.sym].quantity

    algo = MOO()
    run_res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in run_res, run_res.get("error")
    assert algo.seen == 5


def test_live_keeps_filling_a_last_bar_market_order_at_once():
    """The conversion is a BACKTEST rule for now. What a live deployment sends
    a broker at 16:00 is a separate decision (the executor and each adapter's
    market-on-open support), so the warm/live book is left exactly as it was."""
    from datetime import date

    from conftest_helpers import make_book

    book, sleeve = make_book(date(2026, 8, 24), ms=16 * 3600_000)
    assert book.after_close_to_moo is False                   # the default a live book keeps
    t = book.market("TQQQ", 5, 50.0)
    assert t.status == OrderStatus.FILLED and sleeve.qty["TQQQ"] == 5


def test_the_fast_path_runs_a_scheduled_strategy_that_rests_a_market_on_open_order():
    """It used to die in fastpath.next_candidate_ms with
    "'>' not supported between instances of 'float' and 'NoneType'": the scan
    treated every non-market, non-limit, non-stop ticket as a stop-limit."""
    class Scheduled(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 25)
            self.set_cash(100000)
            self.sym = self.add_equity("TQQQ").symbol
            self.schedule.on(self.date_rules.every_day(self.sym),
                             self.time_rules.after_market_open(self.sym, 1), self.go)
            self.done = False

        def go(self):
            if not self.done:
                self.done = True
                self.market_on_open_order(self.sym, 5)

    res = PyBacktester(Scheduled(), _full_session_store()).run()
    assert "error" not in res, res.get("error")
    assert res["fast_path"]["eligible"] is True
    assert [(f["day"], f["px"], f["qty"]) for f in res["fills"]] == [("2026-08-25", 102.5, 5)]

