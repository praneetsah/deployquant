from datetime import datetime, date

import pytest

from dqengine.runtime.symbol import Symbol, Security, Exchange, ExchangeHours
from dqengine.runtime.bars import TradeBar, Bars, Slice
from dqengine.runtime.models import ConstantSlippageModel, NullSlippageModel


def test_symbol_is_stringy_and_uppercased():
    s = Symbol("tqqq")
    assert s == "TQQQ" and s.value == "TQQQ"
    assert {s: 1}[Symbol("TQQQ")] == 1      # hashable, dict-key interop
    assert {"TQQQ": 2}[s] == 2               # plain-str keyed dicts too


def test_security_defaults_and_setters():
    sec = Security(Symbol("TQQQ"), None, Exchange(ExchangeHours(None)))
    assert sec.price == 0.0
    sec.set_leverage(1.33)
    assert sec.leverage == 1.33
    sec.set_slippage_model(ConstantSlippageModel(0.0001))
    sec.price = 100.0

    class _O:
        pass

    assert abs(sec.slippage_model.get_slippage_approximation(sec, _O()) - 0.01) < 1e-12
    sec.SetLeverage(2.0)                     # Pascal alias works
    assert sec.leverage == 2.0


def test_next_trading_day_weekend_fallback():
    h = ExchangeHours(None)
    nxt = h.get_next_trading_day(datetime(2026, 8, 28, 15, 59))  # a Friday
    assert nxt.date() == date(2026, 8, 31)                        # Monday


def test_next_trading_day_with_calendar():
    class Cal:
        days = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 28)]

    h = ExchangeHours(Cal())
    assert h.get_next_trading_day(date(2026, 8, 25)).date() == date(2026, 8, 28)


def test_tradebar_and_slice_access():
    t0, t1 = datetime(2026, 8, 27, 9, 30), datetime(2026, 8, 27, 9, 31)
    bar = TradeBar(Symbol("TQQQ"), t0, t1, 10.0, 11.0, 9.5, 10.5, 1000.0)
    assert bar.Close == 10.5 and bar.price == 10.5 and bar.end_time == t1
    bars = Bars({Symbol("TQQQ"): bar})
    sl = Slice(bars, t1)
    assert sl.bars["TQQQ"].close == 10.5
    assert sl["tqqq".upper()].open == 10.0
    assert sl.contains_key("TQQQ") and "SPY" not in sl
    assert sl.Bars.ContainsKey("TQQQ")
