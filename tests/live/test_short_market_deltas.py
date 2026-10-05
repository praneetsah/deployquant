"""Market orders that open a short (2026-10-05).

A customer's strategies set negative targets on a Webull account. Every
rebalance order went out as a plain SELL, which Webull refuses when it would
open a short (`GENERATE_NEW_SHORT_POSITION`): its side is BUY | SELL | SHORT.
The adapter already sent SHORT when told the order opens one; the market-delta
path never told it, and the two adapter wrappers dropped the argument. The
refused orders were then sent again every sweep -- 2,644 times that session.
"""
from dqengine.live import book as _book
from dqengine.live import executor
from dqengine.live.executor import (AuditAdapter, BookAdapter, Rails,
                                    reconcile)

from rig import FakeBroker

REFUSAL = "Webull refused the request: [GENERATE_NEW_SHORT_POSITION]"


class Venue(FakeBroker):
    """FakeBroker that also keeps what it was told about each sell."""

    def submit(self, creds, symbol, qty, side, opens_short=False, **kw):
        out = super().submit(creds, symbol, qty, side,
                             opens_short=opens_short, **kw)
        out["opens_short"] = opens_short
        return out


def _report():
    return {"actions": [], "errors": []}


def _run(fb, desired, px, adapter=None, refused=None, rep=None):
    rep = rep if rep is not None else _report()
    reconcile(adapter or fb, {}, desired, [], px, Rails(), rep,
              lambda e: None, refused_market=refused)
    return rep


def _subs(fb):
    return [(o["side"], o["qty"], o["opens_short"])
            for tag, o in fb.log if tag == "submit"]


def test_a_sell_from_flat_is_sent_as_an_opening_short():
    fb = Venue(positions={})
    _run(fb, {"KROP": -4.0}, {"KROP": 10.0})
    assert _subs(fb) == [("sell", 4.0, True)]


def test_adding_to_a_short_is_an_opening_short_too():
    fb = Venue(positions={"KROP": -4.0})
    _run(fb, {"KROP": -6.0}, {"KROP": 10.0})
    assert _subs(fb) == [("sell", 2.0, True)]


def test_a_sell_that_stays_long_is_a_plain_sell():
    fb = Venue(positions={"SPY": 10.0})
    _run(fb, {"SPY": 3.0}, {"SPY": 100.0})
    assert _subs(fb) == [("sell", 7.0, False)]


def test_closing_a_long_is_a_plain_sell():
    fb = Venue(positions={"SPY": 10.0})
    _run(fb, {"SPY": 0.0}, {"SPY": 100.0})
    assert _subs(fb) == [("sell", 10.0, False)]


def test_long_to_short_sells_to_flat_first_then_shorts():
    fb = Venue(positions={"TSN": 10.0})
    rep = _run(fb, {"TSN": -2.0}, {"TSN": 50.0})
    assert _subs(fb) == [("sell", 10.0, False)]
    assert any("selling to flat first" in a for a in rep["actions"])
    # the venue filled it; the next sweep sends the rest as a short
    fb.pos, fb.orders, fb.log = {}, [], []
    _run(fb, {"TSN": -2.0}, {"TSN": 50.0})
    assert _subs(fb) == [("sell", 2.0, True)]


def test_buying_back_a_short_is_untouched():
    fb = Venue(positions={"KROP": -4.0})
    _run(fb, {"KROP": 0.0}, {"KROP": 10.0})
    assert _subs(fb) == [("buy", 4.0, False)]


def test_both_wrappers_carry_the_short_to_the_venue(monkeypatch):
    for wrap in (lambda fb, b: BookAdapter(fb, b),
                 lambda fb, b: AuditAdapter(fb, b, transmit=True)):
        fb = Venue(positions={})
        b = _book.Book("cw-short")
        b.apply_audit({}, [])
        _run(fb, {"KROP": -4.0}, {"KROP": 10.0}, adapter=wrap(fb, b))
        assert _subs(fb) == [("sell", 4.0, True)]


def test_a_refused_opening_order_is_sent_three_times_a_day_not_every_sweep():
    fb = Venue(positions={}, fail_submit_msg=REFUSAL)
    refused = {}
    for _ in range(6):
        rep = _run(fb, {"KROP": -4.0}, {"KROP": 10.0}, refused=refused)
    attempts = [x for x in fb.log if x[0] == "submit_attempt"]
    assert len(attempts) == executor.REFUSED_MAX_PER_DAY == 3
    assert rep["errors"] == []                    # no alert from the skip
    (line,) = [a for a in rep["actions"] if "not sent" in a]
    assert "refused 3 times today" in line and "GENERATE_NEW_SHORT" in line


def test_the_audit_sees_no_drift_in_an_order_that_is_no_longer_sent():
    fb = Venue(positions={})
    refused = {("KROP", "sell", 4.0): [3, REFUSAL]}
    b = _book.Book("ca-short")
    audit = AuditAdapter(fb, b, transmit=False)
    _run(fb, {"KROP": -4.0}, {"KROP": 10.0}, adapter=audit, refused=refused)
    assert audit.recorded == []


def test_an_order_that_reduces_a_position_is_never_held_back():
    fb = Venue(positions={"SPY": 10.0}, fail_submit_msg="no")
    refused = {}
    for _ in range(6):
        _run(fb, {"SPY": 0.0}, {"SPY": 100.0}, refused=refused)
    assert len([x for x in fb.log if x[0] == "submit_attempt"]) == 6


def test_the_count_is_per_connection_and_starts_again_each_day(monkeypatch):
    executor._REFUSED_TODAY.clear()
    a = executor.refused_today("c1")
    a[("KROP", "sell", 4.0)] = [3, "x"]
    assert executor.refused_today("c1") is a
    assert executor.refused_today("c2") == {}
    executor._REFUSED_TODAY[("c1", "2026-01-01")] = executor._REFUSED_TODAY.pop(
        next(k for k in executor._REFUSED_TODAY if k[0] == "c1"))
    assert executor.refused_today("c1") == {}
    assert [k for k in executor._REFUSED_TODAY if k[0] == "c1"
            and k[1] == "2026-01-01"] == []


# ------------------------------------------------- found by review, 10/05

def test_a_buy_through_zero_buys_the_short_back_first():
    fb = Venue(positions={"TSN": -10.0})
    rep = _run(fb, {"TSN": 12.0}, {"TSN": 50.0})
    assert _subs(fb) == [("buy", 10.0, False)]
    assert any("buying the short back first" in a for a in rep["actions"])


def test_the_sell_to_flat_is_sent_even_when_the_short_was_refused_all_day():
    """The cap is about the order being sent. Selling a long to flat
    reduces exposure whatever the final target is."""
    fb = Venue(positions={"TSN": 10.0})
    refused = {("TSN", "sell", 10.0): [3, REFUSAL],
               ("TSN", "sell", 22.0): [3, REFUSAL]}
    _run(fb, {"TSN": -12.0}, {"TSN": 50.0}, refused=refused)
    assert _subs(fb) == [("sell", 10.0, False)]


def test_buying_a_short_back_is_sent_whatever_was_refused():
    fb = Venue(positions={"TSN": -10.0})
    refused = {("TSN", "buy", 10.0): [3, "no"]}
    _run(fb, {"TSN": 12.0}, {"TSN": 50.0}, refused=refused)
    assert _subs(fb) == [("buy", 10.0, False)]


def test_a_gateway_refusal_never_counts_for_any_order_in_the_batch():
    msg = "Webull refused: CAN_NOT_TRADING_FOR_FIXGW_NOT_READY_MARKET"
    assert executor._is_market_not_ready(msg)
    fb = Venue(positions={}, fail_submit_msg=msg)
    refused = {}
    for _ in range(4):
        _run(fb, {"A": 1.0, "B": 1.0, "C": 1.0},
             {"A": 10.0, "B": 10.0, "C": 10.0}, refused=refused)
    assert refused == {}


def test_a_holding_smaller_than_one_share_does_not_block_the_short():
    fb = Venue(positions={"TSN": 0.5})
    _run(fb, {"TSN": -3.0}, {"TSN": 50.0})
    (o,) = _subs(fb)
    assert o[0] == "sell" and o[2] is True


def test_a_different_size_is_a_different_order():
    fb = Venue(positions={})
    refused = {("KROP", "sell", 4.0): [3, REFUSAL]}
    _run(fb, {"KROP": -1.0}, {"KROP": 10.0}, refused=refused)
    assert _subs(fb) == [("sell", 1.0, True)]


# ------------------------------------------------------- on-close orders

def _moc(fb, qty, sym="SPY"):
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    rep = _report()
    reconcile(fb, {}, {}, [], {sym: 100.0}, Rails(), rep, lambda e: None,
              moc_wants=[("d1", sym, qty, "market_on_close", None)])
    return rep, [(o["side"], o["qty"], o["opens_short"], o["type"])
                 for tag, o in fb.log if tag == "submit"
                 and o["type"] == "market_on_close"]


def test_an_on_close_sell_from_flat_is_an_opening_short():
    _rep, subs = _moc(Venue(positions={}), -5.0)
    assert subs == [("sell", 5.0, True, "market_on_close")]


def test_an_on_close_sell_of_a_long_is_a_plain_sell():
    _rep, subs = _moc(Venue(positions={"SPY": 5.0}), -5.0)
    assert subs == [("sell", 5.0, False, "market_on_close")]


def test_an_on_close_sell_through_zero_is_cut_to_the_close():
    rep, subs = _moc(Venue(positions={"SPY": 5.0}), -8.0)
    assert subs == [("sell", 5.0, False, "market_on_close")]
    assert any("would cross zero" in a for a in rep["actions"])


def test_an_on_close_buy_through_zero_is_cut_to_the_cover():
    _rep, subs = _moc(Venue(positions={"SPY": -5.0}), 8.0)
    assert subs == [("buy", 5.0, False, "market_on_close")]


def test_an_on_close_buy_is_untouched():
    _rep, subs = _moc(Venue(positions={}), 5.0)
    assert subs == [("buy", 5.0, False, "market_on_close")]


def test_a_short_waits_while_the_venue_still_shows_the_long():
    """Found by review: long 10 with our own sell of 10 still in flight is
    flat on paper, but the venue sees a long. The short goes once it fills."""
    fb = Venue(positions={"TSN": 10.0}, orders=[
        {"id": "o9", "symbol": "TSN", "qty": 10.0, "side": "sell",
         "type": "market", "client_order_id": "sl-mkt-TSN-abc",
         "limit_price": None, "stop_price": None, "trail_percent": None}])
    rep = _run(fb, {"TSN": -4.0}, {"TSN": 50.0})
    assert _subs(fb) == []
    assert any("short of 4 waits" in a for a in rep["actions"])
