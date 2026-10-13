"""Every resting python order either reaches the executor or is journaled.

Silently dropping one is the worst failure this layer has: the engine rests
a protective order, the model believes the position is protected, and
nothing is at the broker. Under ENFORCE it is self-sustaining — the model
fills its own stop on a breach, the ledger returns [] (no such broker
order), the ticket stays resting, and the executor sees zero delta. The
position rides the whole drawdown while the strategy thinks it exited.

project_orders dropped `trailing_stop`, `market_on_close`,
`market_on_open` and `limit_if_touched` under a comment that said "market
orders never rest" — which is untrue of all four.
"""


def _o(sym, qty, kind, oid, **kw):
    d = {"symbol": sym, "qty": qty, "type": kind, "limit_price": None,
         "stop_price": None, "tag": "", "order_id": oid}
    d.update(kw)
    return d


def _h(sym, qty):
    return {"symbol": sym, "qty": qty}


def test_a_trailing_stop_reaches_the_executor():
    """The executor has handled trailing_stop exits since the IR path — the
    payload just never carried python's."""
    from dqengine.live.driver import engine
    exits, entries, deferred = engine.project_orders(
        [_o("TQQQ", -10, "trailing_stop", 4, trail_pct=0.05)], [_h("TQQQ", 10)])
    assert deferred == []
    assert entries == []
    assert exits[0]["type"] == "trailing_stop"
    assert exits[0]["trail_pct"] == 0.05
    assert exits[0]["qty"] == 10


def test_a_trailing_stop_without_a_trail_is_deferred_not_sent():
    """No trail = no level. Sending it would rest an order at a price
    nobody computed."""
    from dqengine.live.driver import engine
    exits, _, deferred = engine.project_orders(
        [_o("TQQQ", -10, "trailing_stop", 4)], [_h("TQQQ", 10)])
    assert exits == []
    assert deferred and "trail" in deferred[0]


def test_kinds_the_executor_cannot_carry_are_journaled_not_dropped():
    from dqengine.live.driver import engine
    for kind in ("market_on_close", "market_on_open", "limit_if_touched"):
        exits, entries, deferred = engine.project_orders(
            [_o("TQQQ", -10, kind, 9)], [_h("TQQQ", 10)])
        assert (exits, entries) == ([], []), kind
        assert deferred and kind in deferred[0], (kind, deferred)


def test_a_market_order_still_produces_no_noise():
    """Market orders genuinely never rest — they must not fill the journal
    with a line every tick."""
    from dqengine.live.driver import engine
    exits, entries, deferred = engine.project_orders(
        [_o("SPY", 5, "market", 1)], [])
    assert (exits, entries, deferred) == ([], [], [])


def test_the_engine_exports_the_trail_on_a_resting_ticket():
    """project_orders can only carry what the payload holds."""
    from dqengine.runtime.orders import OrderBook
    from dqengine.runtime.core.portfolio import Sleeve
    from datetime import date

    sleeve = Sleeve(cash=100000.0)
    book = OrderBook(sleeve, clock=lambda: (date(2026, 9, 4), 0),
                     events_out=lambda e: None, prices={"TQQQ": 50.0})
    sleeve.qty["TQQQ"] = 10
    book.trailing_stop("TQQQ", -10, 0.05, tag="t")
    t = book._open[0]
    assert t.trailing_amount == 0.05 and t.trailing_as_percentage is True


def test_a_daily_edge_ticket_says_when_the_broker_order_goes_out():
    """The journal line for a daily market-on-open/close ticket used to end
    'a brokerage account cannot take this order yet', written before the
    broker legs existed. A user read it as 'my orders never reach my
    broker'. The line now says where it fills and when it is sent."""
    from dqengine.live.driver import engine
    _, _, deferred = engine.project_orders(
        [_o("SGOV", 2, "market_on_open", 3)], [], daily=True)
    assert len(deferred) == 1
    line = deferred[0]
    assert line.startswith("SGOV +2 fills at the next session's open")
    assert "cannot take" not in line
    assert "sent to the broker just after that open" in line
    _, _, deferred = engine.project_orders(
        [_o("SGOV", -2, "market_on_close", 3)], [_h("SGOV", 2)], daily=True)
    assert "sent to the broker just before that close" in deferred[0]
