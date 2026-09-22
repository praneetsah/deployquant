"""A daily deployment, pointed at a broker.

A daily strategy's orders fill at two clock moments, this session's close and
the next session's open, and the MODEL settles them a minute past the close
when the day's bar lands -- because that is where a backtest of the same day
settles them. The executor has no clock: it acts on whatever `holdings` say,
the moment it is called. The payload preview supplies the timing, and only
the timing.

The property the executor-level cases below are all about: ONE market order
at 15:59, NONE at 16:01, ONE at 09:31, and none on any sweep after either.
The preview and the holdings are computed from the SAME replay result, so at
16:01 -- when the ticket disappears and the model takes the quantity over --
the published want does not move by a share, and there is no moment where it
goes back and forth. (That shape, a payload that moved backwards and made the
executor re-place a filled order, is the 2026-09-21 morning incident.)
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from dqengine.live.driver import engine as live_python
from dqengine.live.executor import Rails, reconcile
from dqengine.live.persistence import Deployment
from rig import FakeBroker, report
# the same two shims the executor's own tests build the desired state with,
# so there is one spelling of "the state this sweep reconciles against"
from test_executor import compute_close_orders, compute_targets

ET = ZoneInfo("America/New_York")

D = date(2026, 9, 18)              # Friday, regular close
D1 = date(2026, 9, 21)             # the Monday after it
HALF = date(2026, 11, 27)          # the day after Thanksgiving: 13:00 close
HOLIDAY = date(2026, 11, 26)       # Thanksgiving
CLOSE_MS = 16 * 3600 * 1000
EARLY_CLOSE_MS = 13 * 3600 * 1000


def at(day, h, m, s=0):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)


def _dep(**kw):
    base = dict(id="daily1", name="d", kind="python", code="x",
                universe=["AAA"], resolution="daily",
                cash_initial=10000.0, start_date=date(2026, 9, 14))
    base.update(kw)
    return Deployment(**base)


def _moc(qty, order_id=1, sym="AAA", tag="c20", created=D):
    return {"symbol": sym, "qty": qty, "type": "market_on_close",
            "limit_price": None, "stop_price": None, "tag": tag,
            "order_id": order_id, "trail_pct": None,
            "created_day": created.isoformat()}


def _moo(qty, order_id=2, sym="AAA", tag="c10", created=D):
    return {**_moc(qty, order_id, sym, tag, created),
            "type": "market_on_open"}


def _hold(qty, sym="AAA", px=100.0):
    return {"symbol": sym, "qty": qty, "last_price": px,
            "market_value": round(qty * px, 2), "entry_price": px}


def qtys(p):
    """The published want: symbol -> shares. A previewed position the model
    does not hold yet carries no entry price, so only the quantity is
    comparable across the hand-over."""
    return {h["symbol"]: h["qty"] for h in p["position"]["holdings"]}


def _fill(day, ms, qty, sym="AAA", px=100.0, confirmed=True):
    return {"day": day.isoformat(), "ms": ms, "sym": sym, "qty": qty,
            "px": px, "tag": "c20", "fees": 0.0, "model_px": px,
            "confirmed": confirmed}


def _res(today=D, bar_applied=False, holdings=(), open_orders=(), fills=(),
         last_prices=None):
    return {
        "stats": {"fills": 0, "orders": 0},
        "equity_days": ["2026-09-16", "2026-09-17"],
        "equity": [10000.0, 10010.0],
        "flows": [],
        "fills": list(fills),
        "orders": [],
        "logs": [],
        "position": {"cash": 10000.0, "holdings": list(holdings),
                     "last_prices": (last_prices if last_prices is not None
                                     else {"AAA": 100.0}),
                     "open_orders": list(open_orders)},
        "leverage": 1.0,
        "subscriptions": ["AAA"],
        "resolution": "daily",
        "daily_live": {"today": today.isoformat(),
                       "bar_applied": bar_applied, "next_fire_ms": None},
    }


def payload(res, now, dep=None):
    """The payload a tick at `now` publishes, with the clock frozen."""
    from dqengine.live.driver import engine as lp
    real = lp._now_et
    lp._now_et = lambda: now
    try:
        return lp._payload_from_result(dep or _dep(), [], res)
    finally:
        lp._now_et = real


# ------------------------------------------------------- the at-close window

def test_the_window_opens_a_minute_before_the_close():
    res = _res(open_orders=[_moc(10)])
    for t, want in ((at(D, 15, 58, 59), 0), (at(D, 15, 59, 0), 1),
                    (at(D, 15, 59, 30), 1), (at(D, 16, 0, 30), 1)):
        p = payload(res, t)
        assert len(p["position"]["close_orders"]) == want, t
        assert qtys(p) == ({"AAA": 10} if want else {}), t


def test_the_window_closes_when_the_bar_lands_and_the_want_does_not_move():
    """The hand-over at 16:01. Before the bar the quantity is in the
    preview; after it the model holds it and the ticket is gone. Same
    number both sides, which is what makes the hand-over atomic."""
    before = payload(_res(open_orders=[_moc(10)]), at(D, 15, 59))
    after = payload(_res(bar_applied=True, holdings=[_hold(10)],
                         fills=[_fill(D, 15 * 3600_000 + 59 * 60_000 + 3000,
                                      10)]),
                    at(D, 16, 1))
    assert qtys(before) == qtys(after) == {"AAA": 10}
    assert after["position"]["close_orders"] == []


def test_a_ticket_resting_for_TOMORROWS_close_is_not_published_today():
    """on_data runs when the bar lands. A market-on-close order it places
    then is past this session's close, so it rests for the NEXT one --
    publishing it now would put tomorrow's order in tonight's account."""
    p = payload(_res(bar_applied=True, holdings=[_hold(10)],
                     open_orders=[_moc(4, order_id=9, tag="ondata")]),
                at(D, 16, 1))
    assert p["position"]["close_orders"] == []
    assert qtys(p) == {"AAA": 10}


def test_a_half_day_moves_the_window_with_the_close():
    res = _res(today=HALF, open_orders=[_moc(10, created=HALF)])
    assert payload(res, at(HALF, 12, 30))["position"]["close_orders"] == []
    assert len(payload(res, at(HALF, 12, 59))["position"]["close_orders"]) == 1
    # 15:59 on a half day is two hours after the close, and the window is
    # still open because the bar has not landed -- the want has not moved
    assert len(payload(res, at(HALF, 15, 59))["position"]["close_orders"]) == 1


def test_a_holiday_has_no_window_at_all():
    res = _res(today=HOLIDAY, open_orders=[_moc(10, created=HOLIDAY)])
    assert payload(res, at(HOLIDAY, 15, 59))["position"]["close_orders"] == []
    assert payload(res, at(HOLIDAY, 15, 59))["position"]["holdings"] == []


def test_the_entry_is_the_shape_compute_close_orders_reads():
    p = payload(_res(open_orders=[_moc(-7)]), at(D, 15, 59))
    got = compute_close_orders([("daily1", p["position"], ["AAA"])])
    assert got == [("daily1", "AAA", -7.0, "market_on_close", None,
                    live_python._intent_id("AAA", 1, "c20"))]


def test_two_tickets_on_one_symbol_net_into_one_entry_on_paper():
    """The executor's MOC channel is keyed per (deployment, symbol): a
    second entry would be counted as pending without ever being sent."""
    p = payload(_res(open_orders=[_moc(10, order_id=1),
                                  _moc(-4, order_id=3, tag="c15")]),
                at(D, 15, 59))
    assert p["position"]["close_orders"] == [
        {"type": "market_on_close", "symbol": "AAA", "qty": 6,
         "price": None, "rule": None}]
    assert qtys(p) == {"AAA": 6}


def test_two_tickets_on_one_symbol_freeze_a_broker_deployment():
    """One broker order comes back as one execution row, and the first
    ticket's take() groups every row of that order into itself -- the
    second reads as a no-fill and the model and the account part company.
    Refused at the PAYLOAD, so the model still fills both exactly as the
    backtest does and the row keeps its last good payload."""
    res = _res(open_orders=[_moc(10, order_id=1),
                            _moc(-4, order_id=3, tag="c15")])
    with pytest.raises(live_python.DailyEdgeCollision, match="at-close"):
        payload(res, at(D, 15, 59), dep=_dep(broker_connection_id="c1"))
    # the same tick on paper is fine
    assert payload(res, at(D, 15, 59))["position"]["close_orders"]


def test_a_ticket_on_a_symbol_the_model_does_not_hold_gets_a_price():
    p = payload(_res(open_orders=[_moc(3, sym="BBB")],
                     last_prices={"AAA": 100.0, "BBB": 55.0}),
                at(D, 15, 59))
    _, _, last_px = compute_targets([("daily1", p["position"], ["AAA", "BBB"])])
    assert last_px["BBB"] == 55.0


# -------------------------------------------------------- the at-open window

def test_the_at_open_window_covers_only_a_ticket_from_an_earlier_day():
    """A market-on-open order placed TODAY fills at TOMORROW's open. Asking
    the broker for it now would be a day early."""
    yesterday = _res(today=D1, open_orders=[_moo(5, created=D)])
    today = _res(today=D1, open_orders=[_moo(5, created=D1)])
    assert qtys(payload(yesterday, at(D1, 9, 31))) == {"AAA": 5}
    assert qtys(payload(today, at(D1, 9, 31))) == {}


def test_the_at_open_window_opens_at_0931_and_not_before():
    res = _res(today=D1, open_orders=[_moo(5, created=D)])
    assert qtys(payload(res, at(D1, 9, 30, 59))) == {}
    assert qtys(payload(res, at(D1, 9, 31, 0))) == {"AAA": 5}


def test_an_at_open_ticket_never_becomes_a_close_orders_entry():
    """It is an ordinary market delta at 09:31 -- no new order type, and
    journal.market_cid makes it idempotent."""
    p = payload(_res(today=D1, open_orders=[_moo(5, created=D)]),
                at(D1, 9, 31))
    assert p["position"]["close_orders"] == []


# ------------------------------------------- holding the last pre-close want

def _unconfirmed_moc_day():
    """16:01 on D after a MISSED 15:59 tick: the ledger cannot see a row,
    so the model books its own fill at the close and marks it provisional."""
    return _res(bar_applied=True, holdings=[_hold(10)],
                fills=[_fill(D, CLOSE_MS, 10, confirmed=False)])


def test_a_fill_the_broker_has_not_confirmed_is_held_after_the_close():
    p = payload(_unconfirmed_moc_day(), at(D, 16, 1))
    assert p["position"]["holdings"] == []
    assert any("held out of the target" in j["log"] for j in p["journal"])


def test_a_fill_the_broker_confirmed_is_not_held():
    """The executor's own 15:59 order. Its execution row is stamped before
    the close and the model took it, so the want stands and the delta is
    zero -- that is the whole distinction, and why this needs `enforce`."""
    p = payload(_res(bar_applied=True, holdings=[_hold(10)],
                     fills=[_fill(D, 15 * 3600_000 + 59 * 60_000 + 3000, 10)]),
                at(D, 16, 1))
    assert p["position"]["holdings"] == [_hold(10)]


def test_an_intraday_unconfirmed_fill_is_not_held():
    """The rule holds what the model settled at a session EDGE while the
    market was closed. A fill inside the session is the ordinary
    model-ahead-of-broker state and the executor's own rails cover it."""
    p = payload(_res(bar_applied=True, holdings=[_hold(10)],
                     fills=[_fill(D, 11 * 3600_000, 10, confirmed=False)]),
                at(D, 16, 1))
    assert p["position"]["holdings"] == [_hold(10)]


def test_the_hold_lifts_at_0931_the_next_session():
    res = {**_unconfirmed_moc_day(),
           "daily_live": {"today": D1.isoformat(), "bar_applied": False,
                          "next_fire_ms": None}}
    assert qtys(payload(res, at(D1, 9, 0))) == {}
    assert qtys(payload(res, at(D1, 9, 31))) == {"AAA": 10}


def test_the_hold_does_not_lift_on_a_weekend_or_a_holiday():
    res = {**_unconfirmed_moc_day(),
           "daily_live": {"today": date(2026, 9, 19).isoformat(),
                          "bar_applied": False, "next_fire_ms": None}}
    assert payload(res, at(date(2026, 9, 19), 11, 0))["position"]["holdings"] == []


def test_an_early_close_measures_the_hold_against_its_own_close():
    """A fill at 13:00 on a half day is an at-close fill. At 16:00 it would
    be one on a regular day and nothing at all on this one."""
    res = _res(today=HALF, bar_applied=True, holdings=[_hold(10)],
               fills=[_fill(HALF, EARLY_CLOSE_MS, 10, confirmed=False)])
    assert payload(res, at(HALF, 13, 30))["position"]["holdings"] == []


# ------------------------------------------------- the preview holds no state

def test_the_preview_is_a_pure_function_of_the_result_and_the_clock():
    """A restart inside either window changes nothing: two ticks a second
    apart, and a process that forgot everything in between, publish the
    same numbers."""
    res = _res(open_orders=[_moc(10)])
    first = payload(res, at(D, 15, 59, 1))
    second = payload(res, at(D, 15, 59, 2))
    assert first["position"]["holdings"] == second["position"]["holdings"]
    assert qtys(first) == {"AAA": 10}
    assert first["position"]["close_orders"] == second["position"]["close_orders"]


def test_the_first_day_of_a_deployment_has_nothing_to_hold():
    p = payload(_res(), at(D, 16, 1))
    assert p["position"]["holdings"] == []
    assert p["position"]["close_orders"] == []


def test_a_paused_deployment_replaying_an_older_day_previews_nothing():
    """Its run ends on the day it was paused on. That day is over; there is
    no window on it and nothing to publish."""
    p = payload(_res(today=D, open_orders=[_moc(10)]), at(D1, 15, 59))
    assert p["position"]["close_orders"] == []
    assert p["position"]["holdings"] == []


def test_a_minute_deployment_gets_no_preview_and_an_empty_channel():
    res = {k: v for k, v in _res(open_orders=[_moc(10)]).items()
           if k not in ("daily_live", "resolution")}
    p = payload(res, at(D, 15, 59), dep=_dep(resolution="minute"))
    assert p["position"]["close_orders"] == []
    assert p["position"]["holdings"] == []


# ----------------------------------------------------- through the executor

def _sweep(p, broker, rails=None, recs=None):
    """One reconcile() pass over one deployment's payload."""
    state = [("daily1", p["position"], ["AAA"])]
    desired, exit_wants, last_px = compute_targets(state)
    moc_wants = compute_close_orders(state)
    recs = [] if recs is None else recs
    reconcile(broker, {}, desired, exit_wants, last_px, rails or Rails(),
              report(), recs.append, moc_wants=moc_wants)
    return [e for a, e in broker.log if a == "submit"], recs


def test_one_market_order_at_1559_and_none_at_1601():
    fb = FakeBroker(positions={})
    subs, _ = _sweep(payload(_res(open_orders=[_moc(10)]), at(D, 15, 59)), fb)
    assert len(subs) == 1
    assert subs[0]["qty"] == 10.0 and subs[0]["side"] == "buy"
    assert subs[0]["type"] == "market"          # the near-close emulation

    # the order filled; the model takes the quantity over when the bar lands
    fb2 = FakeBroker(positions={"AAA": 10.0})
    subs2, _ = _sweep(
        payload(_res(bar_applied=True, holdings=[_hold(10)],
                     fills=[_fill(D, 15 * 3600_000 + 59 * 60_000 + 3000, 10)]),
                at(D, 16, 1)), fb2)
    assert subs2 == []


def test_the_same_sweep_twice_sends_one_order():
    """The in-flight guard nets the pending order out; nothing about the
    preview makes it re-place one."""
    fb = FakeBroker(positions={})
    p = payload(_res(open_orders=[_moc(10)]), at(D, 15, 59))
    _sweep(p, fb)
    _sweep(p, fb)
    assert len([e for a, e in fb.log if a == "submit"]) == 1


def test_a_restart_between_the_two_sweeps_sends_one_order():
    """The payload is rebuilt from the replay result, not remembered."""
    fb = FakeBroker(positions={})
    res = _res(open_orders=[_moc(10)])
    _sweep(payload(res, at(D, 15, 59, 1)), fb)
    _sweep(payload(res, at(D, 15, 59, 40)), fb)
    assert len([e for a, e in fb.log if a == "submit"]) == 1


def test_one_market_order_at_0931_and_none_on_the_next_sweep():
    fb = FakeBroker(positions={"AAA": 10.0})
    p = payload(_res(today=D1, holdings=[_hold(10)],
                     open_orders=[_moo(5, created=D)]), at(D1, 9, 31))
    subs, _ = _sweep(p, fb)
    assert len(subs) == 1 and subs[0]["qty"] == 5.0 and subs[0]["side"] == "buy"

    fb2 = FakeBroker(positions={"AAA": 15.0})
    assert _sweep(p, fb2)[0] == []


def test_a_missed_1559_tick_sends_nothing_after_hours():
    """Without the hold the 16:01 fill reads as a delta and a market order
    goes out after the close -- Alpaca queues it to the next open, Webull
    refuses it and the backoff retries at the 04:00 pre-market window."""
    fb = FakeBroker(positions={})
    subs, _ = _sweep(payload(_unconfirmed_moc_day(), at(D, 16, 1)), fb)
    assert subs == []
    subs, _ = _sweep(payload(_unconfirmed_moc_day(), at(D, 19, 0)), fb)
    assert subs == []


def test_a_native_venue_would_place_the_ticket_as_written():
    """Not reachable yet -- daily_broker_refusal keeps those destinations
    out until the ticket is published at decision time -- but the entry the
    preview emits is the one reconcile()'s step 0 submits."""
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit",
                                          "market_on_close"}))
    subs, _ = _sweep(payload(_res(open_orders=[_moc(10)]), at(D, 15, 59)), fb)
    assert len(subs) == 1 and subs[0]["type"] == "market_on_close"


def test_dry_run_records_the_intent_and_sends_nothing():
    fb = FakeBroker(positions={})
    subs, recs = _sweep(payload(_res(open_orders=[_moc(10)]), at(D, 15, 59)),
                        fb, rails=Rails(dry_run=True))
    assert subs == []
    assert [r["status"] for r in recs] == ["dry_run"]
    assert recs[0]["symbol"] == "AAA" and recs[0]["qty"] == 10.0


def test_an_unfolded_fill_of_our_own_order_still_freezes_the_symbol():
    """The rail is not weakened anywhere. Between 15:59 and 16:01 it is
    harmless because the delta is zero; a same-symbol at-close order after
    an unfolded 09:31 fill is frozen, loudly, and that is the known limit
    in the daily docs."""
    fb = FakeBroker(positions={"AAA": 5.0})
    p = payload(_res(holdings=[_hold(5)], open_orders=[_moc(10)]),
                at(D, 15, 59))
    state = [("daily1", p["position"], ["AAA"])]
    desired, exit_wants, last_px = compute_targets(state)
    rep = report()
    reconcile(fb, {}, desired, exit_wants, last_px, Rails(), rep, lambda r: None,
              moc_wants=compute_close_orders(state),
              journal_gates={"unfolded": {"AAA": 5.0}})
    assert [e for a, e in fb.log if a == "submit"] == []
    assert any("not yet folded" in a for a in rep["actions"])


# ---------------------------------------------------- what the dialog says

CODE = ("from AlgorithmImports import *\n\n"
        "class A(QCAlgorithm):\n"
        "    def on_data(self, data):\n"
        "        self.set_holdings('AAA', 1.0)\n")


def test_a_daily_strategy_declares_the_at_close_order_it_will_place():
    """On daily data a market order placed while the session is open
    BECOMES an at-close order. The source scan sees set_holdings and never
    the conversion, so without this the venue's emulation note would not
    reach the deploy dialog for the strategies that use it."""
    from dqengine.live import capabilities

    assert capabilities.python_order_types(CODE) == {"market"}
    assert capabilities.python_order_types(CODE, daily=True) == {
        "market", "market_on_close"}


def test_a_market_on_close_order_is_transmittable_on_daily_data():
    """PYTHON_UNTRANSMITTED says the platform cannot carry an at-close order
    from a python strategy to a broker. On daily data it does -- the payload
    publishes it -- so the lift is exactly that, and the venue's own answer
    is unchanged."""
    from dqengine.adapters import catalog as registry
    from dqengine.live import capabilities

    caps = registry.get_adapter("alpaca").caps
    bad = capabilities.unsupported_for_deploy(
        caps, ["market", "market_on_close"], kind="python")
    assert [r.order_type for r in bad] == ["market_on_close"]
    assert capabilities.unsupported_for_deploy(
        caps, ["market", "market_on_close"], kind="python", daily=True) == []


def test_the_lift_does_not_approve_what_the_venue_cannot_do():
    """It lifts the platform's own limit and nothing else."""
    from dqengine.adapters.base import Caps
    from dqengine.live import capabilities

    caps = Caps(order_types=frozenset({"market"}))       # no limit orders
    bad = capabilities.unsupported_for_deploy(
        caps, ["limit"], kind="python", daily=True)
    assert [r.order_type for r in bad] == ["limit"]


def test_a_minute_strategy_still_cannot_send_one():
    from dqengine.adapters import catalog as registry
    from dqengine.live import capabilities

    caps = registry.get_adapter("alpaca").caps
    bad = capabilities.unsupported_for_deploy(
        caps, ["market_on_close"], kind="python")
    assert len(bad) == 1 and "nothing transmits" in bad[0].note


def test_the_emulation_note_is_what_the_journal_records_at_the_sweep():
    from dqengine.adapters import catalog as registry
    from dqengine.live import capabilities

    note = capabilities.journal_note(registry.get_adapter("alpaca").caps,
                                     "market_on_close", "AAA")
    assert "market order near the close" in note


def test_the_preview_moves_no_fill_and_no_equity():
    """It publishes a want earlier than the model reaches it. The model is
    untouched, which is why a paper deployment's fills still equal its
    backtest to the cent (test_daily_live_paper.py's end-to-end case)."""
    res = _res(open_orders=[_moc(10)],
               fills=[_fill(D, 11 * 3600_000, 2)])
    plain = payload(res, at(D, 12, 0))
    inside = payload(res, at(D, 15, 59))
    assert plain["fills"] == inside["fills"]
    assert plain["equity"] == inside["equity"]
    assert plain["position"]["cash"] == inside["position"]["cash"]


# --------------------------------------------- a venue with its own on-close

def native(res, now, dep=None):
    """The payload for a destination whose caps take market-on-close."""
    from dqengine.live.driver import engine as lp
    real_now, real_venue = lp._now_et, lp.venue_takes_moc
    lp._now_et = lambda: now
    lp.venue_takes_moc = lambda conn_id: True
    try:
        return lp._payload_from_result(
            dep or _dep(broker_connection_id="c1"), [], res)
    finally:
        lp._now_et, lp.venue_takes_moc = real_now, real_venue


def test_a_native_venue_gets_the_ticket_when_it_rests():
    """The exchange stops accepting on-close orders ten minutes before the
    close. Publishing at 15:59 there would be a rejection, not an order, so
    the window opens at the decision instead."""
    res = _res(open_orders=[_moc(10)])
    p = native(res, at(D, 15, 40))
    assert p["position"]["close_orders"] == [
        {"type": "market_on_close", "symbol": "AAA", "qty": 10,
         "price": None, "rule": live_python._intent_id("AAA", 1, "c20")}]
    assert qtys(p) == {"AAA": 10}


def test_the_same_ticket_is_not_published_to_an_emulation_venue_yet():
    """A minute before the close is the emulation timing, and 15:40 is not
    it: an emulation venue would send a market order three hours early."""
    p = payload(_res(open_orders=[_moc(10)]), at(D, 15, 40))
    assert p["position"]["close_orders"] == [] and qtys(p) == {}


def test_past_the_venue_cutoff_it_falls_back_to_the_emulation_timing():
    res = _res(open_orders=[_moc(10)])
    assert native(res, at(D, 15, 49))["position"]["close_orders"] == []
    assert qtys(native(res, at(D, 15, 49))) == {}       # nothing yet
    late = native(res, at(D, 15, 59))
    assert late["position"]["close_orders"] == []       # not to a shut window
    assert qtys(late) == {"AAA": 10}                    # a market order instead
    assert any("on-close cutoff" in j["log"] for j in late["journal"])


def test_the_cutoff_moves_with_an_early_close():
    res = _res(today=HALF, open_orders=[_moc(10, created=HALF)])
    assert len(native(res, at(HALF, 12, 47))["position"]["close_orders"]) == 1
    assert native(res, at(HALF, 12, 49))["position"]["close_orders"] == []


def test_the_native_entry_reaches_the_venue_as_an_on_close_order():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit",
                                          "market_on_close"}),
                   supports_client_order_id=False)
    subs, _ = _sweep(native(_res(open_orders=[_moc(10)]), at(D, 15, 40)), fb)
    assert len(subs) == 1
    assert subs[0]["type"] == "market_on_close" and subs[0]["qty"] == 10.0
    # and the market-delta pass nets it out rather than sending it twice
    assert len([e for a, e in fb.log if a == "submit"]) == 1


def test_a_resting_native_ticket_is_not_resubmitted_on_the_next_sweep():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit",
                                          "market_on_close"}),
                   supports_client_order_id=False)
    p = native(_res(open_orders=[_moc(10)]), at(D, 15, 40))
    _sweep(p, fb)
    _sweep(native(_res(open_orders=[_moc(10)]), at(D, 15, 44)), fb)
    assert len([e for a, e in fb.log if a == "submit"]) == 1


def test_a_venue_lookup_that_fails_uses_the_emulation_timing():
    """The conservative direction: a native venue treated as an emulation
    venue trades a slightly worse price; the other way round would publish
    an order type the broker cannot take."""
    assert live_python.venue_takes_moc(None) is False
    assert live_python.venue_takes_moc("no-such-connection") is False
