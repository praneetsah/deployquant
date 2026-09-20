"""The run result carries the final position. A live deployment reads its
holdings out of the sandbox's JSON — there is no engine object to inspect,
so anything the deployment payload needs has to travel inside the result."""
from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester

from conftest_helpers import two_day_store


class HoldsAndRests(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.done = False

    def on_data(self, data):
        if not self.done:
            self.done = True
            self.market_order(self.sym, 10, tag="entry")
            self.limit_order(self.sym, -10, 500.0, tag="tp")


def test_result_carries_final_position():
    res = PyBacktester(HoldsAndRests(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    pos = res["position"]
    assert pos["holdings"] == [{
        "symbol": "TQQQ", "qty": 10, "last_price": 105.0,
        "market_value": 1050.0, "entry_price": 100.0}]
    assert pos["cash"] == 9000.0
    assert pos["last_prices"] == {"TQQQ": 105.0}


def test_result_carries_resting_orders():
    """A limit sell at 500 never fills against a ~100 tape — it rests, and
    the executor has to be able to see it.

    `order_id` is part of the contract, not incidental: the live layer turns
    it into the order's intent id and therefore its broker cid prefix."""
    res = PyBacktester(HoldsAndRests(), two_day_store()).run()
    assert res["position"]["open_orders"] == [{
        "symbol": "TQQQ", "qty": -10, "type": "limit",
        "limit_price": 500.0, "stop_price": None, "tag": "tp",
        "order_id": 2, "trail_pct": None}]


def test_flat_strategy_reports_empty_position():
    class Flat(HoldsAndRests):
        def on_data(self, data):
            pass

    res = PyBacktester(Flat(), two_day_store()).run()
    assert res["position"]["holdings"] == []
    assert res["position"]["open_orders"] == []
    assert res["position"]["cash"] == 10000.0


def test_short_position_is_reported_with_negative_qty():
    """Plan 4 needs this; getting the sign right here costs nothing."""
    class Shorts(HoldsAndRests):
        def on_data(self, data):
            if not self.done:
                self.done = True
                self.market_order(self.sym, -5, tag="short")

    res = PyBacktester(Shorts(), two_day_store()).run()
    h = res["position"]["holdings"][0]
    assert h["qty"] == -5
    assert h["market_value"] == -525.0
    assert res["position"]["cash"] == 10500.0


def test_holdings_are_ordered_by_absolute_exposure():
    class Two(HoldsAndRests):
        def initialize(self):
            super().initialize()
            self.other = self.add_equity("SPY").symbol

        def on_data(self, data):
            if not self.done:
                self.done = True
                self.market_order(self.sym, 2, tag="small")
                self.market_order(self.other, -9, tag="big")

    res = PyBacktester(Two(), two_day_store()).run()
    assert [h["symbol"] for h in res["position"]["holdings"]] == ["SPY", "TQQQ"]
