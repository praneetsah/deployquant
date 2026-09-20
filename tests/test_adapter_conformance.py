"""One suite every broker adapter must pass.

Adding a venue should be: implement the interface, declare capabilities, run
this. No executor changes. That is only true if the contract is enforced
somewhere, and this is that somewhere.

Every check here exists because something went wrong without it:

* `tifs` was a flat set and could not say "GTC, except trailing stops",
  which silently refused every trailing stop at Webull for weeks.
* `cancel` swallowed BrokerUnavailable, so "we do not know whether the
  cancel landed" was recorded as "the order is gone" — and the next pass
  submitted a replacement alongside a stop that was still resting.
* `trail_percent` is a PERCENT to adapters and a FRACTION to Webull; a 100x
  error on a protective exit.

The companion check — that every capability an adapter can DECLARE has a
consumer in the executor (the `supports_short` defect) — reads the
platform's private executor sources, so it lives with them:
platform/api/tests/test_adapter_caps_consumed.py. Everything here reads
only the adapter side, which is open.

Which venues run: every catalog entry marked `implemented`, resolved through
the plugin loader. An implemented id whose distribution is not installed
here SKIPS by name rather than failing, so a checkout without the plugins
still runs the suite for the venues it has.

Where an adapter is knowingly deficient the gap is an xfail NAMING it, so
the suite stays green and removing the xfail is the todo. A permanently red
suite teaches people to ignore it.
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dqengine import brokers                                          # noqa: E402
from dqengine.adapters import base                                    # noqa: E402
from dqengine.adapters import catalog as registry                     # noqa: E402
from dqengine.adapters.base import OrderNotSupported                  # noqa: E402

IMPLEMENTED = [e.id for e in registry.BROKERS.values() if e.implemented]


def _adapter(broker_id):
    """An adapter INSTANCE for a catalog id, or a skip naming the plugin
    this checkout lacks."""
    try:
        cls = registry.adapter_class(broker_id)
    except brokers.UnknownBroker:
        pytest.skip(f"{broker_id} plugin not installed")
    return cls()


# ------------------------------------------------- capabilities are honest

@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_caps_are_internally_consistent(broker_id):
    caps = _adapter(broker_id).caps
    assert caps.order_types, "an adapter that can express nothing is not usable"
    assert caps.tifs, "no time-in-force at all"
    for order_type, tifs in caps.tif_by_type.items():
        assert order_type in caps.order_types, (
            f"{order_type} narrowed in tif_by_type but not offered at all")
        assert tifs, f"{order_type} declares an EMPTY tif set"
        assert tifs <= caps.tifs, (
            f"{order_type} claims tifs the broker does not support: "
            f"{tifs - caps.tifs}")
    for order_type in caps.order_types:
        assert order_type in base.ALL_ORDER_TYPES, (
            f"{order_type} is not a known order type")


# ------------------------------------------- refuse locally, never send

@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_submit_refuses_an_unsupported_type_before_touching_the_network(broker_id):
    """Dummy creds: if the adapter validated AFTER building a session, this
    would raise a connection/auth error instead of OrderNotSupported."""
    adapter = _adapter(broker_id)
    unsupported = [t for t in base.ALL_ORDER_TYPES
                   if t not in adapter.caps.order_types]
    if not unsupported:
        pytest.skip(f"{broker_id} supports every known order type")
    with pytest.raises(OrderNotSupported):
        adapter.submit({}, "TQQQ", 1, "buy", order_type=unsupported[0],
                       tif=sorted(adapter.caps.tifs)[0])


@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_submit_refuses_an_unsupported_tif_before_touching_the_network(broker_id):
    adapter = _adapter(broker_id)
    bad = next((t for t in ("day", "gtc", "ioc", "fok", "opg", "cls")
                if t not in adapter.caps.tifs), None)
    if bad is None:
        pytest.skip(f"{broker_id} supports every tif we know about")
    with pytest.raises(OrderNotSupported):
        adapter.submit({}, "TQQQ", 1, "buy", order_type=base.MARKET, tif=bad)


@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_submit_refuses_an_opening_short_it_cannot_take(broker_id):
    adapter = _adapter(broker_id)
    if adapter.caps.supports_short:
        pytest.skip(f"{broker_id} declares short support")
    with pytest.raises(OrderNotSupported):
        adapter.submit({}, "TQQQ", 1, "sell", order_type=base.MARKET,
                       tif=sorted(adapter.caps.tifs)[0], opens_short=True)


@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_submit_accepts_opens_short_in_its_signature(broker_id):
    """The executor passes it on every entry. An adapter without it raises
    TypeError mid-sweep, which is a refusal nobody can read."""
    sig = inspect.signature(_adapter(broker_id).submit)
    assert "opens_short" in sig.parameters


# ---------------------------------------- "gone" is not the same as "unknown"

@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_cancel_does_not_swallow_an_unavailable_venue(broker_id):
    """A 429 or a 5xx means we do not know whether the cancel landed, and
    the order may still be resting. Recording it as cancelled lets the next
    pass submit a replacement ALONGSIDE it — two protective stops on the
    same shares. Webull did exactly this."""
    src = inspect.getsource(inspect.getmodule(type(_adapter(broker_id))))
    if "def cancel" not in src:
        pytest.skip(f"{broker_id} inherits cancel")
    body = src[src.index("def cancel"):]
    body = body[:body.find("\n    def ", 1)] if "\n    def " in body[1:] else body
    if "BrokerUnavailable" not in body:
        return                        # never catches it — cannot swallow it
    caught_and_passed = ("except BrokerUnavailable" in body
                         and "pass" in body.split("except BrokerUnavailable")[1][:200])
    assert not caught_and_passed, (
        f"{broker_id}.cancel swallows BrokerUnavailable — 'unknown' recorded "
        f"as 'gone'")


# ------------------------------------------------- units, stated and tested

@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_the_trailing_unit_convention_is_written_down(broker_id):
    """trail_percent is a PERCENT to adapters (5 == 5%). Venues disagree:
    Webull's trailing_stop_step is a FRACTION (0.01 == 1%). A 100x error
    here is a protective exit at the wrong distance, so any adapter offering
    trailing stops must say which it sends."""
    adapter = _adapter(broker_id)
    if base.TRAILING_STOP not in adapter.caps.order_types:
        pytest.skip(f"{broker_id} does not offer trailing stops")
    # MODULE source, not the class: normalizers and payload builders are
    # module-level functions in these adapters.
    src = inspect.getsource(inspect.getmodule(type(adapter)))
    assert "trail_percent" in src
    low = src.lower()
    # An adapter must state which unit it puts on the wire — including when
    # the answer is "the same one, unchanged". Silence is the ambiguity that
    # produced the Webull 100x.
    stated = ("fraction" in low or "/ 100" in low
              or "already a percent" in low or "no conversion" in low)
    assert "percent" in low and stated, (
        f"{broker_id} offers trailing stops without stating the unit it "
        f"sends — say so even if no conversion is needed")


# ------------------------------------------------- the executor's matching

@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_open_orders_is_documented_to_return_the_fields_matching_needs(broker_id):
    """`_match_no_cid` adopts a resting order by (symbol, side, type, qty,
    level). An adapter that omits any of them turns adoption into
    cancel+resubmit of a live protective order."""
    adapter = _adapter(broker_id)
    src = inspect.getsource(inspect.getmodule(type(adapter)))
    if "def open_orders" not in src:
        pytest.skip(f"{broker_id} inherits open_orders")
    for field in ("symbol", "side", "qty", "limit_price", "stop_price",
                  "client_order_id"):
        assert f'"{field}"' in src, (
            f"{broker_id}.open_orders never mentions {field}, which "
            f"_match_no_cid matches on")


@pytest.mark.parametrize("broker_id", IMPLEMENTED)
def test_entitlement_only_explains_types_the_venue_does_not_offer(broker_id):
    """It explains an ABSENCE. An entitlement note on a type the adapter
    already offers is a contradiction, and would read as a warning about
    something that works."""
    caps = _adapter(broker_id).caps
    for order_type in caps.entitlement_by_type:
        assert order_type not in caps.order_types, (
            f"{broker_id} declares an entitlement reason for {order_type}, "
            f"which it also offers natively")
        assert order_type in base.ALL_ORDER_TYPES


# ------------------------------------------------- the bundled family

def test_alpaca_and_alpaca_paper_resolve_to_the_same_family():
    from dqengine import brokers
    live = brokers.load_class("alpaca")
    paper = brokers.load_class("alpaca-paper")
    assert issubclass(paper, live) and paper is not live
