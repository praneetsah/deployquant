"""ACCEPTANCE: ComposerWM74 (daily 29-ETF switcher; minute subscriptions,
daily history seeding, list-form set_holdings) either runs, or fails with a
clean UnsupportedApiError naming the call — never a confusing crash."""
import os
import re

import pytest

from dqengine.runtime import run_python_backtest
from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.portfolio_view import PortfolioTarget

from conftest_helpers import two_day_store

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
D = os.path.join(ROOT, "qc", "ComposerWM74")

needs_data = pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "daily"))
    or not os.path.isfile(os.path.join(D, "symphony.py")),
    reason="daily data or the ComposerWM74 source project not present")


def test_set_holdings_list_of_targets():
    class A(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 24)
            self.set_end_date(2026, 8, 24)
            self.set_cash(1000)
            self.a = self.add_equity("AAA").symbol
            self.b = self.add_equity("BBB").symbol
            self.done = False

        def on_data(self, data):
            if not self.done:
                self.done = True
                self.set_holdings([PortfolioTarget(self.a, 0.5),
                                   PortfolioTarget(self.b, 0.4)],
                                  liquidate_existing_holdings=True)

    algo = A()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    by_sym = {f["sym"]: f["qty"] for f in res["fills"]}
    assert by_sym["AAA"] == 5 and by_sym["BBB"] == 4      # @100 on 1000 equity


@needs_data
def test_composer_runs_or_fails_loudly():
    sym = open(os.path.join(D, "symphony.py")).read()
    main = re.sub(r"^from symphony import.*$", "",
                  open(os.path.join(D, "main.py")).read(), flags=re.M)
    res = run_python_backtest(sym + "\n" + main, data_root=DATA,
                              overrides={"start": "2024-01-02",
                                         "end": "2024-06-28",
                                         "cash": 1000.0})
    if "error" in res:
        # acceptable only as a clean unsupported-API report
        assert res["error"]["type"] == "UnsupportedApiError", res["error"]
    else:
        assert res["stats"]["days"] > 100 and res["stats"]["fills"] > 0
