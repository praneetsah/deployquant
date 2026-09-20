from datetime import date, datetime

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester, RunOverrides

from conftest_helpers import OPEN_MS, two_day_store


class BuyOnce(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol
        self.seen = []

    def on_data(self, data):
        self.seen.append((self.time, data[self.sym].close))
        if not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 5, tag="entry")


def test_loop_fills_marks_equity_and_times():
    algo = BuyOnce()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    # market order on first bar fills at that bar's close = 100
    # confirmed/fees/model_px are part of the payload contract: the
    # executor's fold rail reads `confirmed`, and a fill without it counts
    # as unfolded and freezes that symbol's exits.
    assert res["fills"][0] == {"day": "2026-08-24", "ms": OPEN_MS + 60_000,
                               "sym": "TQQQ", "qty": 5, "px": 100.0,
                               "tag": "entry", "fees": 0.0, "model_px": None,
                               "confirmed": True}
    # equity marked at each session close: 500 + 5*102 = 1010 ; 500 + 5*105 = 1025
    assert res["equity_days"] == ["2026-08-24", "2026-08-25"]
    assert res["equity"] == [1010.0, 1025.0]
    assert res["stats"]["fills"] == 1
    # on_data got bar ENDS: first slice time is 9:31
    assert algo.seen[0][0] == datetime(2026, 8, 24, 9, 31)


class ScheduledSeller(BuyOnce):
    def initialize(self):
        super().initialize()
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.at(9, 32),
                         self.rebalance)
        self.fired = []

    def rebalance(self):
        self.fired.append(self.time)


def test_scheduled_event_fires_after_data_of_its_bar():
    algo = ScheduledSeller()
    PyBacktester(algo, two_day_store()).run()
    assert algo.fired == [datetime(2026, 8, 24, 9, 32), datetime(2026, 8, 25, 9, 32)]


def test_overrides_clobber_code_dates():
    algo = BuyOnce()
    res = PyBacktester(algo, two_day_store(),
                       overrides=RunOverrides(start=date(2026, 8, 25), cash=2000)).run()
    assert res["equity_days"] == ["2026-08-25"]
    assert res["cash"] == 2000


def test_user_exception_is_captured_not_raised():
    class Boom(BuyOnce):
        def on_data(self, data):
            raise ValueError("kaboom")

    res = PyBacktester(Boom(), two_day_store()).run()
    assert res["error"]["type"] == "ValueError" and "kaboom" in res["error"]["message"]


class WarmupProbe(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 25)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol
        self.set_warm_up(1)          # one trading day
        self.warm_bars = 0
        self.live_bars = 0
        self.events = []

    def on_data(self, data):
        if self.is_warming_up:
            self.warm_bars += 1
            self.market_order(self.sym, 1)      # must be refused
        else:
            self.live_bars += 1

    def on_order_event(self, e):
        self.events.append((e.status, e.fill_quantity))


def test_warmup_feeds_data_but_blocks_orders_and_equity():
    algo = WarmupProbe()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert algo.warm_bars == 3 and algo.live_bars == 3
    assert res["equity_days"] == ["2026-08-25"]
    assert res["fills"] == []
    assert algo.events and all(s.name == "INVALID" for s, _ in algo.events)


class EventProbe(BuyOnce):
    def initialize(self):
        super().initialize()
        self.events = []

    def on_order_event(self, e):
        self.events.append((e.status.name, e.fill_price, e.fill_quantity))


def test_on_order_event_fill():
    algo = EventProbe()
    PyBacktester(algo, two_day_store()).run()
    assert ("FILLED", 100.0, 5) in algo.events
