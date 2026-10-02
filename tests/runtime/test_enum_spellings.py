"""LEAN's python accepts both spellings of its enum members: the older
mixed case (Resolution.Minute, OrderStatus.Filled) and the upper case. A
strategy written either way runs here; before 2026-10-02 `Resolution.Minute`
raised AttributeError in initialize."""
from dqengine.runtime.enums import (OrderDirection, OrderStatus, OrderType,
                                    Resolution)


def test_mixed_case_names_are_the_same_members():
    assert Resolution.Minute is Resolution.MINUTE
    assert Resolution.Daily is Resolution.DAILY
    assert Resolution.Second is Resolution.SECOND
    assert Resolution.Hour is Resolution.HOUR
    assert OrderStatus.Filled is OrderStatus.FILLED
    assert OrderType.MarketOnClose is OrderType.MARKET_ON_CLOSE
    assert OrderType.StopMarket is OrderType.STOP_MARKET
    assert OrderDirection.Buy is OrderDirection.BUY


def test_iteration_still_lists_each_member_once():
    assert [r.name for r in Resolution] == ["SECOND", "MINUTE", "HOUR",
                                            "DAILY"]


def test_a_strategy_using_the_mixed_case_spelling_initializes():
    from dqengine.runtime.algorithm import QCAlgorithm

    class A(QCAlgorithm):
        def initialize(self):
            self.add_equity("SPY", Resolution.Minute)
    a = A()
    a.initialize()
    assert "SPY" in {str(s).upper() for s in a.securities.keys()}
