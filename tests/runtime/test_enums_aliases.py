import pytest
from dqengine.runtime.errors import UnsupportedApiError, unsupported
from dqengine.runtime.enums import (Resolution, OrderType, OrderStatus,
                              OrderDirection, BrokerageName, AccountType,
                              UpdateOrderFields)
from dqengine.runtime.aliases import camel_to_snake, PascalMixin, alias_methods


def test_unsupported_raises_with_doc_pointer():
    with pytest.raises(UnsupportedApiError, match="add_option is not supported"):
        unsupported("add_option")


def test_resolutions_and_order_enums():
    assert Resolution.MINUTE != Resolution.SECOND
    assert Resolution.DAILY.name == "DAILY"
    assert OrderStatus.FILLED.name == "FILLED"
    assert OrderType.LIMIT.name == "LIMIT"
    assert OrderDirection.BUY.name == "BUY"


def test_brokerage_and_account_accept_any_name():
    # BrokerageName.WEBULL, .QUANT_CONNECT, .ANYTHING must not AttributeError:
    # they're accepted-and-ignored settings in v1.
    assert BrokerageName.WEBULL == "WEBULL"
    assert BrokerageName.INTERACTIVE_BROKERS_BROKERAGE == "INTERACTIVE_BROKERS_BROKERAGE"
    assert AccountType.MARGIN == "MARGIN"


def test_update_order_fields_defaults():
    f = UpdateOrderFields()
    assert f.quantity is None and f.limit_price is None and f.tag is None


def test_camel_to_snake():
    assert camel_to_snake("SetStartDate") == "set_start_date"
    assert camel_to_snake("OnData") == "on_data"
    assert camel_to_snake("total_portfolio_value") == "total_portfolio_value"
    assert camel_to_snake("RSI") == "rsi"
    assert camel_to_snake("IsReady") == "is_ready"


def test_pascal_mixin_and_method_aliases():
    @alias_methods
    class Thing(PascalMixin):
        def __init__(self):
            self.end_time = 5

        def do_stuff(self, x):
            return x * 2

    t = Thing()
    assert t.EndTime == 5          # attribute via __getattr__
    assert t.DoStuff(3) == 6       # method via class alias
    with pytest.raises(AttributeError):
        t.Nonexistent
