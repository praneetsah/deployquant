"""A withdrawal that sells shares: a cash event (iso_day, amount, {"sells":
{SYM: qty}}). The cash leaves the sleeve at the session's open, the sells
go out at the session's first bar as market orders tagged "withdraw", and
a symbol the sleeve no longer holds is a notice, not an order."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from parity import DATA, run_py                                  # noqa: E402
from test_codegen import needs_data                              # noqa: E402

CODE = '''
from AlgorithmImports import *

class BuyOnce(QCAlgorithm):
    def initialize(self):
        self.add_equity("SPY", Resolution.MINUTE)
        self.done = False

    def on_data(self, data):
        if not self.done and self.securities["SPY"].price > 0:
            self.market_order("SPY", 10)
            self.done = True
'''


@needs_data
def test_a_withdrawal_sells_the_planned_shares_and_takes_the_cash():
    res = run_py(CODE, "2024-01-16", "2024-01-24", 10000.0, DATA, None, None,
                 [("2024-01-22", -2000.0, {"sells": {"SPY": 3, "QQQ": 1}})])
    fills = res["fills"]
    sale = [f for f in fills if f["day"] == "2024-01-22"]
    assert [(f["sym"], f["qty"], f["tag"]) for f in sale] == [("SPY", -3, "withdraw")]
    assert res["position"]["holdings"][0]["qty"] == 7
    assert res["flows"][res["equity_days"].index("2024-01-22")] == -2000.0
    # nothing else traded: the strategy itself only ever bought once
    assert [f["qty"] for f in fills] == [10, -3]
    # the sleeve's cash: 10,000 - the buy - 2,000 + the sale's proceeds
    buy, sell = fills[0], sale[0]
    assert abs(res["position"]["cash"]
               - (10000.0 - 10 * buy["px"] - 2000.0 + 3 * sell["px"]
                  - buy["fees"] - sell["fees"])) < 0.02


@needs_data
def test_a_plain_withdrawal_sells_nothing():
    res = run_py(CODE, "2024-01-16", "2024-01-24", 10000.0, DATA, None, None,
                 [("2024-01-22", -500.0)])
    assert [f["qty"] for f in res["fills"]] == [10]
    assert res["flows"][res["equity_days"].index("2024-01-22")] == -500.0
