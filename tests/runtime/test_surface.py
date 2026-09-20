"""The introspected surface manifest the AI porting loop is taught."""
from dqengine.runtime.enums import Resolution
from dqengine.runtime.surface import supported_surface
from dqengine.runtime.symbol import Exchange, ExchangeHours, Security, Symbol


def test_manifest_lists_the_real_surface():
    s = supported_surface()
    for needle in ("is_short", "unrealized_profit_percent", "market_order",
                   "set_holdings", "stop_limit_order", "total_portfolio_value",
                   "before_market_close", "after_market_open", "is_ready"):
        assert needle in s, needle


def test_unsupported_only_in_the_not_available_line():
    s = supported_surface()
    available, not_available = s.split("NOT AVAILABLE")
    for needle in ("cash_book", "add_universe", "add_option"):
        assert needle not in available, needle
        assert needle in not_available, needle


def test_pinned_security_instance_attrs_exist():
    sec = Security(Symbol("SPY"), Resolution.MINUTE,
                   Exchange(ExchangeHours(None)))
    for a in ("symbol", "resolution", "price", "open", "high", "low",
              "close", "volume", "invested", "leverage"):
        assert hasattr(sec, a), a
