import pandas as pd

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.enums import Resolution

from conftest_helpers import two_day_store


class H(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 25)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol
        self.first = None

    def on_data(self, data):
        if self.first is None:
            self.first = self.history(self.sym, 1, Resolution.DAILY)


def test_daily_history_prior_session_only():
    algo = H()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    df = algo.first
    assert isinstance(df, pd.DataFrame) and len(df) == 1
    assert df.iloc[0]["close"] == 102.0            # 8/24's close, not 8/25's


class HMin(H):
    def on_data(self, data):
        if self.first is None and self.time.minute == 32:
            self.first = self.history(self.sym, 3, Resolution.MINUTE)


def test_minute_history_walks_back_across_sessions():
    algo = HMin()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    df = algo.first
    # at 8/25 09:32 the last 3 completed minute bars are 102 (8/24), 103, 104
    assert list(df["close"]) == [102.0, 103.0, 104.0]


def test_multi_symbol_history_has_symbol_level():
    class H2(QCAlgorithm):
        def initialize(self):
            self.set_start_date(2026, 8, 25)
            self.set_end_date(2026, 8, 25)
            self.set_cash(1000)
            self.a = self.add_equity("AAA").symbol
            self.b = self.add_equity("BBB").symbol
            self.first = None

        def on_data(self, data):
            if self.first is None:
                self.first = self.history([self.a, self.b], 1, Resolution.DAILY)

    algo = H2()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    df = algo.first
    assert set(df.index.get_level_values(0)) == {"AAA", "BBB"}
    assert df.loc["AAA"].iloc[0]["close"] == 102.0


def test_unsubscribed_symbol_rejected():
    class Bad(H):
        def on_data(self, data):
            self.history("SPY", 5)

    res = PyBacktester(Bad(), two_day_store()).run()
    assert res["error"]["type"] == "UnsupportedApiError"


def test_manifest_pass_returns_empty(monkeypatch):
    monkeypatch.setenv("DQENGINE_MANIFEST_PASS", "1")
    algo = H()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert algo.first is not None and algo.first.empty
