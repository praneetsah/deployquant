import pytest


def test_entry_point_resolves_and_passes_the_shape_check():
    from dqengine import brokers
    cls = brokers.load_class("schwab")
    assert cls.__name__ == "SchwabAdapter"
    assert cls.caps.order_types


# --------------------------------------------------- the on-close order body
#
# Schwab is the one venue on the roster whose caps claim a native
# MARKET_ON_CLOSE, and a daily deployment publishes that ticket to it when
# the decision is made. Nothing had ever built the request for that order
# type. These cases run the builder only -- no network, no account.

def _body(**kw):
    from dqengine_schwab.adapter import _order_body
    args = dict(symbol="spy", qty=5, side="buy", order_type="market_on_close",
                tif="day", limit_price=None)
    args.update(kw)
    return _order_body(**args)


def test_a_market_on_close_order_is_built_as_schwab_spells_it():
    body = _body()
    assert body["orderType"] == "MARKET_ON_CLOSE"
    assert body["duration"] == "DAY"
    assert body["session"] == "NORMAL"
    assert body["orderStrategyType"] == "SINGLE"
    leg = body["orderLegCollection"][0]
    assert leg["instruction"] == "BUY" and leg["quantity"] == 5
    assert leg["instrument"] == {"symbol": "SPY", "assetType": "EQUITY"}


def test_it_carries_no_price_of_any_kind():
    """An on-close order has no price; sending one is a rejection."""
    body = _body()
    assert "price" not in body and "stopPrice" not in body


def test_a_sell_side_on_close_order_reads_as_a_sell():
    leg = _body(side="sell", qty=3)["orderLegCollection"][0]
    assert leg["instruction"] == "SELL" and leg["quantity"] == 3


def test_a_limit_on_close_order_carries_its_price_as_a_string():
    body = _body(order_type="limit_on_close", limit_price=412.345)
    assert body["orderType"] == "LIMIT_ON_CLOSE"
    assert body["price"] == "412.35"


def test_the_pre_flight_check_accepts_the_on_close_types():
    from dqengine import brokers
    from dqengine.adapters import base

    caps = brokers.load_class("schwab").caps
    for kind in ("market_on_close", "limit_on_close"):
        base.validate_order(
            caps, kind, "day",
            limit_price=1.0 if kind.startswith("limit") else None,
            qty=5, broker_name="Charles Schwab")


def test_a_fractional_on_close_order_is_refused_before_the_wire():
    """Schwab takes sub-1 quantities on market orders only. Catching it here
    names the broker instead of returning a cryptic venue rejection."""
    from dqengine import brokers
    from dqengine.adapters import base

    caps = brokers.load_class("schwab").caps
    with pytest.raises(base.OrderNotSupported, match="fractional"):
        base.validate_order(caps, "market_on_close", "day", qty=0.5,
                            broker_name="Charles Schwab")
