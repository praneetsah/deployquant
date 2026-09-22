"""Capabilities are per ORDER TYPE, not per broker.

`Caps.tifs` was a flat set, so the model could only say "this broker accepts
DAY and GTC". Reality is narrower and varies by venue: Webull accepts GTC
generally but a TRAILING_STOP_LOSS only in DAY. The executor hard-coded
tif="gtc" on every resting order, so a trailing stop was submitted in a
time-in-force its venue does not allow for that type — and was refused.

The general shape matters more than the instance. Adding a broker should be
describing what it can do, not editing the executor: a venue where MOC is a
retail feature and one where it is institutional-only differ in DATA, not in
code.
"""
import pytest

from dqengine.adapters import base
from dqengine.adapters.base import Caps, OrderNotSupported


def test_tifs_for_falls_back_to_the_broker_wide_set():
    caps = Caps(tifs=frozenset({"day", "gtc"}))
    assert caps.tifs_for(base.LIMIT) == frozenset({"day", "gtc"})


def test_a_type_can_narrow_the_tifs_it_accepts():
    caps = Caps(tifs=frozenset({"day", "gtc"}),
                tif_by_type={base.TRAILING_STOP: frozenset({"day"})})
    assert caps.tifs_for(base.TRAILING_STOP) == frozenset({"day"})
    assert caps.tifs_for(base.LIMIT) == frozenset({"day", "gtc"})


def test_validate_order_rejects_a_tif_the_TYPE_cannot_take():
    """Broker-wide GTC is not enough — the type has to accept it too."""
    caps = Caps(order_types=frozenset({base.TRAILING_STOP}),
                tifs=frozenset({"day", "gtc"}),
                tif_by_type={base.TRAILING_STOP: frozenset({"day"})})
    with pytest.raises(OrderNotSupported, match="time-in-force"):
        base.validate_order(caps, base.TRAILING_STOP, "gtc", None, None,
                            5.0, 10, False, "X")
    base.validate_order(caps, base.TRAILING_STOP, "day", None, None,
                        5.0, 10, False, "X")


def test_pick_tif_prefers_the_caller_and_degrades_rather_than_failing():
    """A resting order wants GTC. If the type cannot take it, fall back to
    what the venue does allow instead of submitting something it refuses."""
    from dqengine.live.executor import pick_tif

    caps = Caps(tifs=frozenset({"day", "gtc"}),
                tif_by_type={base.TRAILING_STOP: frozenset({"day"})})
    assert pick_tif(caps, base.LIMIT, "gtc") == "gtc"
    assert pick_tif(caps, base.TRAILING_STOP, "gtc") == "day"


def test_pick_tif_raises_when_nothing_is_available():
    from dqengine.live.executor import pick_tif

    caps = Caps(tifs=frozenset(), tif_by_type={})
    with pytest.raises(OrderNotSupported):
        pick_tif(caps, base.LIMIT, "gtc")


def test_the_executor_no_longer_hard_codes_gtc_on_resting_orders():
    from dqengine.live import executor
    src = open(executor.__file__).read()
    assert 'tif="gtc"' not in src, "a resting order's tif must be negotiated"
    assert "pick_tif(" in src


# ------------------------------------------------------- webull trailing

@pytest.fixture
def native_trailing(monkeypatch):
    """Webull's native trailing stop is behind WEBULL_NATIVE_TRAILING=1
    until a 1-share probe observes it against the live venue."""
    import importlib
    import dqengine_webull.adapter as wb
    monkeypatch.setenv("WEBULL_NATIVE_TRAILING", "1")
    importlib.reload(wb)
    yield wb
    monkeypatch.delenv("WEBULL_NATIVE_TRAILING", raising=False)
    importlib.reload(wb)


def test_webull_declares_trailing_stops_as_day_only(native_trailing):
    caps = native_trailing.WebullAdapter.caps
    assert base.TRAILING_STOP in caps.order_types
    assert caps.tifs_for(base.TRAILING_STOP) == frozenset({"day"})
    assert "gtc" in caps.tifs_for(base.LIMIT)


def test_webull_sends_the_trailing_fields_as_a_fraction(monkeypatch, native_trailing):
    """Adapters take trail_percent as a PERCENT (5 == 5%); Webull's
    trailing_stop_step is a FRACTION (0.01 == 1%). Getting this wrong by
    100x on a protective exit is not a rounding error."""
    sent = {}

    class _Api:
        class order:
            @staticmethod
            def place_order_v2(account_id, order):
                sent.update(order)
                return {"data": {}}

    WebullAdapter = native_trailing.WebullAdapter
    a = WebullAdapter()
    monkeypatch.setattr(a, "_api", lambda creds: _Api())
    monkeypatch.setattr(a, "_call", lambda fn, *args, **kw: fn(*args, **kw))
    monkeypatch.setattr(a, "_instrument_id", lambda api, sym: "iid")
    monkeypatch.setattr("dqengine_webull.adapter._payload", lambda r: r)

    a.submit({"account_id": "acct"}, "TQQQ", 10, "sell",
             order_type=base.TRAILING_STOP, tif="day", trail_percent=5.0)

    assert sent["order_type"] == "TRAILING_STOP_LOSS"
    assert sent["trailing_type"] == "PERCENTAGE"
    assert float(sent["trailing_stop_step"]) == pytest.approx(0.05)
