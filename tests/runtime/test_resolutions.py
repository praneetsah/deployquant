from datetime import date

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.enums import Resolution

from conftest_helpers import SynthStore, synth_day


class DailyAlgo(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ", Resolution.DAILY).symbol
        self.slices = 0

    def on_data(self, data):
        self.slices += 1
        if not self.portfolio[self.sym].invested:
            self.set_holdings(self.sym, 0.9)


def test_daily_loop_one_slice_per_session():
    store = SynthStore({date(2026, 8, 24): synth_day(date(2026, 8, 24), [100, 101, 102]),
                        date(2026, 8, 25): synth_day(date(2026, 8, 25), [103, 104, 105])})
    algo = DailyAlgo()
    res = PyBacktester(algo, store).run()
    assert "error" not in res, res.get("error")
    assert algo.slices == 2
    assert res["stats"]["fills"] == 1
    assert res["resolution"] == "daily"
    # The daily bar is delivered at the session close, when the exchange is
    # shut. LEAN says so itself ("market orders submitted while the market is
    # closed are automatically converted into MarketOnOpen orders") and fills
    # at the NEXT session's open; checked on SPY daily 2025-03-03/04: LEAN and
    # this engine both fill at 569.93, the 03-04 open. It used to fill at the
    # close it had just been shown (102.0), a price nobody can trade.
    assert (res["fills"][0]["day"], res["fills"][0]["px"]) == ("2026-08-25", 102.5)
    assert res["equity_days"] == ["2026-08-24", "2026-08-25"]


class TwoSym(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 24)
        self.set_cash(1000)
        self.a = self.add_equity("AAA").symbol
        self.b = self.add_equity("BBB").symbol
        self.b_seen = 0

    def on_data(self, data):
        if self.b in data:
            self.b_seen += 1


class HoleyStore(SynthStore):
    def load_minute_day(self, sym, day):
        if sym == "BBB":
            return None            # BBB has no data that day
        return super().load_minute_day(sym, day)


def test_missing_symbol_absent_from_slice():
    store = HoleyStore({date(2026, 8, 24): synth_day(date(2026, 8, 24), [100, 101, 102])})
    algo = TwoSym()
    res = PyBacktester(algo, store).run()
    assert "error" not in res, res.get("error")
    assert algo.b_seen == 0
