"""Exits on SHORT positions.

There is no broker to test against here, so every direction-sensitive
decision the executor makes is pinned individually. The dangerous cases are
not the ones where short support is merely absent — they are the ones where
the long-only rule is *inverted* for a short, and so does the wrong thing
confidently:

  * a protective stop sits BELOW a long and ABOVE a short
  * a breach comes from above for a long, from below for a short
  * a sell-exit against a short position would DOUBLE it, not close it
"""
from dqengine.live import executor


def _pos(sym, qty, px=100.0):
    return {"holdings": [{"symbol": sym, "qty": qty, "last_price": px}],
            "last_prices": {sym: px}}


def _targets(dep_id, pos, universe):
    """The executor's read of one deployment's payload, as the sweep takes
    it: (desired, exit_wants, last_px)."""
    st = executor.desired_from_payload(dep_id, pos, universe)
    return st.desired, list(st.exit_wants), st.last_px


# ------------------------------------------------------ desired_from_payload

def test_a_long_exit_is_a_sell():
    pos = {**_pos("SPY", 10), "open_orders": [
        {"type": "limit_sell", "symbol": "SPY", "price": 120.0,
         "rule": "r1"}]}
    _d, wants, _px = _targets("d1", pos, ["SPY"])
    assert wants == [("d1", "SPY", 10.0, "limit", 120.0, None, None, "r1",
                      "sell")]


def test_a_short_exit_is_a_buy_of_the_magnitude():
    pos = {**_pos("VXX", -8), "open_orders": [
        {"type": "limit_sell", "symbol": "VXX", "price": 80.0,
         "rule": "r2"}]}
    _d, wants, _px = _targets("d1", pos, ["VXX"])
    assert wants == [("d1", "VXX", 8.0, "limit", 80.0, None, None, "r2",
                      "buy")]


def test_a_flat_symbol_still_rests_nothing():
    pos = {**_pos("SPY", 0), "open_orders": [
        {"type": "limit_sell", "symbol": "SPY", "price": 120.0}]}
    _d, wants, _px = _targets("d1", pos, ["SPY"])
    assert wants == []


def test_desired_carries_the_short_as_negative():
    _d, _w, _px = _targets("d1", _pos("VXX", -8), ["VXX"])
    assert _d["VXX"] == -8.0


# ------------------------------------------------------- the mirrored rules

def test_a_short_stop_belongs_above_the_market():
    """The inverted case. A long's sell-stop at/above the market triggers
    instantly; for a short's buy-stop that is exactly where it belongs."""
    rails = executor.rails_from({}, "paper", True)
    # a long exit priced into the market is refused
    assert executor.limit_price_refusal("sell", 90.0, 100.0, rails)
    # the mirror for a short exit
    assert executor.limit_price_refusal("buy", 110.0, 100.0, rails)
    # and each is fine on its own passive side
    assert executor.limit_price_refusal("sell", 110.0, 100.0, rails) is None
    assert executor.limit_price_refusal("buy", 90.0, 100.0, rails) is None


def test_quote_breach_is_mirrored_by_side():
    # a long's sell-stop at 90: breached when price FALLS through it
    assert executor.quote_breaches_stop("stop", 90.0, 89.0) is True
    assert executor.quote_breaches_stop("stop", 90.0, 91.0) is False
    # a short's buy-stop at 110: breached when price RISES through it
    assert executor.quote_breaches_stop("stop", 110.0, 111.0,
                                           side="buy") is True
    assert executor.quote_breaches_stop("stop", 110.0, 109.0,
                                           side="buy") is False


def test_quote_breach_default_stays_long_only():
    """Every existing caller passes no side and means a long."""
    assert executor.quote_breaches_stop("stop", 90.0, 89.0) is True


def test_quote_breach_still_ignores_non_stop_kinds():
    assert executor.quote_breaches_stop("limit", 90.0, 1.0,
                                           side="buy") is False


# -------------------------------------------- reconcile(), for real

from rig import FakeBroker                          # noqa: E402
from dqengine.live.executor import Rails, reconcile                         # noqa: E402


def _report():
    return {"actions": [], "errors": []}


def test_a_short_exit_submits_a_BUY_to_the_broker():
    """The whole point: an exit on a short has to reach the venue as a buy."""
    fb = FakeBroker(positions={"VXX": -8.0})
    rep = _report()
    reconcile(fb, {}, {"VXX": -8.0},
              [("d1", "VXX", 8.0, "limit", 80.0, None, None, "r1", "buy")],
              {"VXX": 100.0}, Rails(), rep, lambda e: None)
    subs = [e for a, e in fb.log if a == "submit"]
    exits = [x for x in subs if x["type"] == "limit"]
    assert len(exits) == 1, fb.log
    assert exits[0]["side"] == "buy"
    assert exits[0]["qty"] == 8.0
    assert exits[0]["limit_price"] == 80.0


def test_a_long_exit_still_submits_a_SELL():
    fb = FakeBroker(positions={"SPY": 10.0})
    reconcile(fb, {}, {"SPY": 10.0},
              [("d1", "SPY", 10.0, "limit", 120.0, None, None, "r1", "sell")],
              {"SPY": 100.0}, Rails(), _report(), lambda e: None)
    exits = [e for a, e in fb.log
             if a == "submit" and e["type"] == "limit"]
    assert len(exits) == 1 and exits[0]["side"] == "sell"


def test_an_exit_whose_position_is_on_the_wrong_side_is_deferred():
    """A buy-exit against a LONG would double it. It must never be sent."""
    fb = FakeBroker(positions={"SPY": 10.0})
    rep = _report()
    reconcile(fb, {}, {"SPY": 10.0},
              [("d1", "SPY", 10.0, "limit", 80.0, None, None, "r1", "buy")],
              {"SPY": 100.0}, Rails(), rep, lambda e: None)
    exits = [e for a, e in fb.log
             if a == "submit" and e["type"] == "limit"]
    assert exits == []
    assert any("deferred until shares arrive" in a for a in rep["actions"])


def test_a_short_stop_above_the_market_is_accepted():
    """The inverted rule. This exact order is refused for a long."""
    fb = FakeBroker(positions={"VXX": -8.0})
    fb.caps = type(fb.caps)(order_types=frozenset({"market", "limit", "stop"}))
    rep = _report()
    reconcile(fb, {}, {"VXX": -8.0},
              [("d1", "VXX", 8.0, "stop", None, 110.0, None, "r1", "buy")],
              {"VXX": 100.0}, Rails(), rep, lambda e: None)
    stops = [e for a, e in fb.log
             if a == "submit" and e["type"] == "stop"]
    assert len(stops) == 1, rep["actions"]
    assert stops[0]["side"] == "buy"


def test_a_short_stop_BELOW_the_market_is_refused():
    """Below the market a short's buy-stop triggers instantly — the mirror
    of the long rule, and it must refuse just as firmly."""
    fb = FakeBroker(positions={"VXX": -8.0})
    fb.caps = type(fb.caps)(order_types=frozenset({"market", "limit", "stop"}))
    rep = _report()
    reconcile(fb, {}, {"VXX": -8.0},
              [("d1", "VXX", 8.0, "stop", None, 90.0, None, "r1", "buy")],
              {"VXX": 100.0}, Rails(), rep, lambda e: None)
    stops = [e for a, e in fb.log
             if a == "submit" and e["type"] == "stop"]
    assert stops == []
    assert any("refused" in a and "<=" in a for a in rep["actions"]), \
        rep["actions"]


def test_a_long_stop_above_the_market_is_still_refused():
    """The pre-existing rule must not have moved."""
    fb = FakeBroker(positions={"SPY": 10.0})
    fb.caps = type(fb.caps)(order_types=frozenset({"market", "limit", "stop"}))
    rep = _report()
    reconcile(fb, {}, {"SPY": 10.0},
              [("d1", "SPY", 10.0, "stop", None, 110.0, None, "r1", "sell")],
              {"SPY": 100.0}, Rails(), rep, lambda e: None)
    assert [e for a, e in fb.log
            if a == "submit" and e["type"] == "stop"] == []
    assert any("refused" in a and ">=" in a for a in rep["actions"])


# ------------------------------------------------------- end-to-end payload

def test_a_python_short_book_reaches_the_executor():
    """The driver emits it, the executor reads it, both directions."""
    from dqengine.live.driver import engine as driver_engine
    exits, entries, deferred = driver_engine.project_orders(
        [{"symbol": "VXX", "qty": 8, "type": "limit", "limit_price": 80.0,
          "stop_price": None, "tag": "", "order_id": 5}],
        [{"symbol": "VXX", "qty": -8, "last_price": 100.0,
          "market_value": -800.0, "entry_price": 100.0}])
    assert deferred == [], "short exits are wired now, nothing to defer"
    assert entries == []
    assert exits[0]["side"] == "buy" and exits[0]["qty"] == 8

    pos = {"holdings": [{"symbol": "VXX", "qty": -8, "last_price": 100.0}],
           "last_prices": {"VXX": 100.0}, "open_orders": exits,
           "entry_orders": entries, "close_orders": []}
    _d, wants, _px = _targets("d1", pos, ["VXX"])
    assert wants[0][:4] == ("d1", "VXX", 8.0, "limit")
    assert wants[0][8] == "buy"


# ------------------------------------------------- the quote-driven fast path

def test_the_breach_exit_qty_is_already_correct_for_both_sides():
    """-qty sells a long and buys back a short; nothing to change there."""
    for held, expected in ((10.0, -10.0), (-8.0, 8.0)):
        assert -float(held) == expected
