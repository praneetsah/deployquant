from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester

from conftest_helpers import two_day_store


class Holder(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol
        self.add_equity("SPYY")          # second symbol, same synth data
        self.day = 0

    def on_data(self, data):
        if self.time.minute == 31 and self.time.hour == 9:
            self.day += 1
            if self.day == 1:
                self.set_holdings(self.sym, 0.5)          # ~5 sh @100
            elif self.day == 2:
                self.set_holdings(self.sym, 0.0)          # sell all
                self.liquidate()


def test_set_holdings_targets_and_liquidate():
    algo = Holder()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    fills = res["fills"]
    assert fills[0]["qty"] == 5 and fills[0]["px"] == 100.0
    assert sum(f["qty"] for f in fills) == 0          # ends flat
    assert res["equity"][-1] > 0


class LiquidateExisting(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 24)
        self.set_cash(1000)
        self.a = self.add_equity("AAA").symbol
        self.b = self.add_equity("BBB").symbol
        self.step = 0

    def on_data(self, data):
        self.step += 1
        if self.step == 1:
            self.set_holdings(self.a, 0.4)
        elif self.step == 2:
            # move everything to BBB in one call
            self.set_holdings(self.b, 0.5, liquidate_existing=True)


def test_set_holdings_liquidate_existing_flag():
    algo = LiquidateExisting()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    by_sym = {}
    for f in res["fills"]:
        by_sym[f["sym"]] = by_sym.get(f["sym"], 0) + f["qty"]
    assert by_sym["AAA"] == 0 and by_sym["BBB"] > 0


class ShortProbe(QCAlgorithm):
    """Shorts TQQQ and records the full Holding surface mid-run."""

    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol
        self.snap = None

    def on_data(self, data):
        if self.time.minute == 31 and self.time.hour == 9 and \
                not self.portfolio[self.sym].invested:
            self.market_order(self.sym, -3)
        h = self.portfolio[self.sym]
        if h.invested and self.snap is None and h.price != h.average_price:
            self.snap = {
                "is_long": h.is_long, "is_short": h.is_short,
                "abs_qty": h.absolute_quantity,
                "abs_val": h.absolute_holdings_value,
                "cost": h.holdings_cost,
                "upnl": h.unrealized_profit,
                "upnl_pct": h.unrealized_profit_percent,
                "IsShort": h.IsShort,                 # Pascal alias
                "pf_vals": [x.symbol for x in self.portfolio.values()],
                "pf_upnl": self.portfolio.total_unrealized_profit,
                "pf_hval": self.portfolio.total_holdings_value,
            }


def test_short_holding_surface():
    algo = ShortProbe()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    s = algo.snap
    assert s is not None
    assert s["is_short"] and s["IsShort"] and not s["is_long"]
    assert s["abs_qty"] == 3
    assert s["cost"] < 0 and s["abs_val"] > 0
    # short profit sign: price ROSE above the entry -> losing short, and
    # the percent must carry the same sign as the dollar pnl
    assert (s["upnl"] < 0) == (s["upnl_pct"] < 0)
    assert abs(s["upnl_pct"] - s["upnl"] / abs(s["cost"])) < 1e-12
    assert s["pf_vals"] == ["TQQQ"]
    assert abs(s["pf_upnl"] - s["upnl"]) < 1e-9
