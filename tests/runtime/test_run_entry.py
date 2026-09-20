from dqengine.runtime import run_python_backtest

from conftest_helpers import two_day_store

CODE = '''
from AlgorithmImports import *

class My(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ", Resolution.MINUTE).symbol
    def on_data(self, data):
        if not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 5, tag="entry")
'''


def test_run_from_source_string():
    res = run_python_backtest(CODE, data_root="/nonexistent",
                              overrides={"store": two_day_store()})
    assert "error" not in res, res.get("error")
    assert res["subscriptions"] == ["TQQQ"] and res["resolution"] == "minute"
    assert res["stats"]["fills"] == 1


def test_pascal_case_code_runs_too():
    pascal = '''
from AlgorithmImports import *

class MyOld(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2026, 8, 24)
        self.SetEndDate(2026, 8, 25)
        self.SetCash(1000)
        self.sym = self.AddEquity("TQQQ", Resolution.MINUTE).Symbol
    def OnData(self, data):
        if not self.Portfolio[self.sym].Invested:
            self.MarketOrder(self.sym, 5)
'''
    res = run_python_backtest(pascal, data_root="/x",
                              overrides={"store": two_day_store()})
    assert "error" not in res, res.get("error")
    assert res["stats"]["fills"] == 1


def test_syntax_error_reported():
    res = run_python_backtest("def broken(:\n", data_root="/x")
    assert res["error"]["type"] == "SyntaxError"


def test_no_algorithm_class():
    res = run_python_backtest("x = 1\n", data_root="/x")
    assert "no QCAlgorithm subclass" in res["error"]["message"]


def test_user_traceback_points_at_algorithm_file():
    bad = CODE.replace('self.market_order(self.sym, 5, tag="entry")',
                       "raise RuntimeError('boom')")
    res = run_python_backtest(bad, data_root="/x",
                              overrides={"store": two_day_store()})
    assert res["error"]["type"] == "RuntimeError"
    assert "<algorithm>" in res["error"]["traceback"]


def test_overrides_dates_as_strings():
    res = run_python_backtest(CODE, data_root="/x",
                              overrides={"store": two_day_store(),
                                         "start": "2026-08-25", "cash": 5000})
    assert "error" not in res, res.get("error")
    assert res["equity_days"] == ["2026-08-25"] and res["cash"] == 5000


def test_standalone_script_gets_conversion_advice():
    script = '''#!/usr/bin/env python3
import argparse

def main():
    print("backtesting")

if __name__ == "__main__":
    main()
'''
    res = run_python_backtest(script, data_root="/nonexistent")
    msg = res["error"]["message"]
    assert "no QCAlgorithm subclass" in msg
    assert "standalone Python script" in msg
    assert res["error"]["hint"] == "standalone_script"
    # the engine names no product: a hosted caller adds its own tip off `hint`
    assert "AI builder" not in msg and "platform" not in msg


def test_missing_module_gets_sandbox_advice():
    res = run_python_backtest("import yfinance_definitely_missing\n",
                              data_root="/nonexistent")
    err = res["error"]
    assert err["type"] == "ModuleNotFoundError"
    assert err["hint"] == "missing_import"
    assert "bar store" in err["message"]
    assert "no network access" in err["message"]


def test_plain_missing_class_keeps_short_error():
    res = run_python_backtest("x = 1\n", data_root="/nonexistent")
    msg = res["error"]["message"]
    assert "no QCAlgorithm subclass" in msg
    assert "standalone" not in msg
