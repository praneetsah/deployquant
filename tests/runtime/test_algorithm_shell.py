from datetime import date, timedelta

import pytest

from dqengine.runtime.algorithm import QCAlgorithm, resolve_hook
from dqengine.runtime.enums import Resolution
from dqengine.runtime.errors import UnsupportedApiError


def test_config_setters_both_spellings():
    a = QCAlgorithm()
    a.set_start_date(2021, 1, 4)
    a.SetEndDate(2026, 6, 9)
    a.set_cash(1000)
    assert a._start_date == date(2021, 1, 4) and a._end_date == date(2026, 6, 9)
    assert a._cash == 1000
    a.set_benchmark("SPY")
    assert a._benchmark == "SPY"
    a.set_brokerage_model("whatever", "MARGIN")   # broker ignored, account type not
    assert a._account_leverage == 2.0            # see test_leverage.py
    a.settings.free_portfolio_value_percentage = 0.05  # accepted
    assert a.settings.liquidate_enabled is None        # unknown reads are None


def test_start_date_accepts_date_objects():
    a = QCAlgorithm()
    a.set_start_date(date(2021, 1, 4))
    a.set_end_date(date(2026, 6, 9))
    assert a._start_date == date(2021, 1, 4) and a._end_date == date(2026, 6, 9)


def test_add_equity_and_unsupported():
    a = QCAlgorithm()
    eq = a.add_equity("tqqq", Resolution.MINUTE)
    assert eq.symbol == "TQQQ" and a.securities["TQQQ"] is eq
    assert a.AddEquity("TQQQ") is eq
    with pytest.raises(UnsupportedApiError, match="add_option"):
        a.add_option("SPY")
    with pytest.raises(UnsupportedApiError):
        a.add_universe(lambda coarse: [])
    with pytest.raises(UnsupportedApiError):
        a.plot("chart", "series", 1.0)


def test_warmup_conversion():
    a = QCAlgorithm()
    a.set_warm_up(30)
    assert a._warmup_days == 30
    a.set_warm_up(timedelta(days=10))
    assert a._warmup_days == 10
    a.set_warm_up(390, Resolution.MINUTE)
    assert a._warmup_days == 1


def test_logs_bounded():
    a = QCAlgorithm()
    for i in range(10_050):
        a.log(i)
    assert len(a._logs) == 10_001 and a._logs[-1].endswith("truncated")


def test_resolve_hook_finds_pascal_overrides():
    class Old(QCAlgorithm):
        def Initialize(self):
            self.marker = "pascal"

        def OnData(self, data):
            self.marker = "ondata"

    class New(QCAlgorithm):
        def initialize(self):
            self.marker = "snake"

    o, n = Old(), New()
    resolve_hook(o, "initialize", "Initialize")()
    assert o.marker == "pascal"
    resolve_hook(n, "initialize", "Initialize")()
    assert n.marker == "snake"
    assert resolve_hook(o, "on_data", "OnData") == o.OnData
