"""Which orders can wait at the broker past the session they were placed in.

A venue whose fill feed covers only orders placed today (Webull) cannot
report the fill of an order placed on an earlier day. Under broker-driven
accounting that fill reads as a confirmed no-fill, so the deploy endpoint
asks this question before it lets a deployment trust the broker's rows.
"""
from dqengine.adapters import base
from dqengine.adapters.base import Caps
from dqengine.live import capabilities


def test_market_and_at_close_orders_never_wait_overnight():
    assert capabilities.overnight_order_types(
        {base.MARKET, base.MARKET_ON_CLOSE}) == set()


def test_limit_stop_and_trailing_orders_can_wait_overnight():
    got = capabilities.overnight_order_types(
        {base.MARKET, base.LIMIT, base.STOP, base.TRAILING_STOP})
    assert got == {base.LIMIT, base.STOP, base.TRAILING_STOP}


def test_a_set_holdings_daily_strategy_has_none():
    code = ("class A(QCAlgorithm):\n"
            "    def rebalance(self):\n"
            "        self.SetHoldings('SPY', 0.5)\n"
            "        self.Liquidate('QQQ')\n")
    types = capabilities.python_order_types(code, daily=True)
    assert capabilities.overnight_order_types(types) == set()


def test_a_take_profit_block_has_one():
    ir = {"rules": [{"action": {"type": "managed_target"}}]}
    assert capabilities.overnight_order_types(
        capabilities.ir_order_types(ir)) == {base.LIMIT}


def test_venues_report_earlier_orders_unless_they_say_otherwise():
    assert Caps().fills_of_earlier_orders is True
    assert Caps(fills_of_earlier_orders=False).fills_of_earlier_orders \
        is False
