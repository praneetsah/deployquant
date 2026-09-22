"""The broker-execution ledger the engine is handed, and the unknown set.

`build_ledger` is the gate that keeps execution truth dormant until a
connection is switched to `enforce`: in `observe` the executions are polled
and displayed but must never reach the engine's accounting.

`_unknown_symbols` is the other half of the safety property. A broker
outage (a recorded execution-poll error) or a same-day order still in
flight must read as UNKNOWN, never as a confirmed no-fill. Conflating the
two is how an outage becomes a duplicate live position: the strategy
concludes it never traded, the replay still wants the position, and the
executor buys it again.

Every figure here is made up.
"""
from datetime import date, datetime, timezone

from dqengine.live import deployment_store, persistence


def _seed(pg, owner_id, conn_id="c1", dep_id="d1", truth="enforce",
          universe=("TQQQ", "SPY"), ir=None, **dep_kw):
    with pg() as s:
        s.add(persistence.BrokerConnection(id=conn_id, user_id=owner_id,
                                           broker="alpaca",
                                           execution_truth=truth))
        s.add(persistence.Deployment(
            id=dep_id, user_id=owner_id, name="F", start_date=date(2026, 8, 1),
            ir=ir if ir is not None else {"universe": {"static": list(universe)}},
            status="running", broker_connection_id=conn_id,
            cash_initial=1000.0, reconciled_from=date(2026, 8, 1), **dep_kw))
        s.commit()


def _dep(s, dep_id="d1"):
    return s.get(persistence.Deployment, dep_id)


def test_build_ledger_is_none_unless_enforcing(pg, owner_id):
    _seed(pg, owner_id, truth="observe")
    with pg() as s:
        assert deployment_store.build_ledger(s, _dep(s)) is None


def test_build_ledger_converts_executions_to_et_bar_times(pg, owner_id):
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_exec_id="e1",
            symbol="TQQQ", signed_qty=12.0, price=50.25, fees=0.0,
            filled_at=datetime(2026, 8, 24, 13, 31, 41, tzinfo=timezone.utc),
            source="broker"))
        s.commit()
    with pg() as s:
        led = deployment_store.build_ledger(s, _dep(s))
    got = led.take(date(2026, 8, 24), "TQQQ", None)
    assert len(got) == 1
    # 13:31:41Z == 09:31:41 ET -> 34301000 ms since ET midnight
    assert got[0].time_ms == 34301000 and got[0].price == 50.25


def test_build_ledger_rounds_signed_qty_instead_of_truncating(pg, owner_id):
    """signed_qty is a Float sourced from broker API responses, so
    11.9999999997 must round to 12, not truncate to 11 and silently drop a
    share."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_exec_id="e1",
            symbol="TQQQ", signed_qty=11.9999999997, price=50.25, fees=0.0,
            filled_at=datetime(2026, 8, 24, 13, 31, 41, tzinfo=timezone.utc),
            source="broker"))
        s.commit()
    with pg() as s:
        led = deployment_store.build_ledger(s, _dep(s))
    assert led.take(date(2026, 8, 24), "TQQQ", None)[0].qty == 12


def test_build_ledger_carries_broker_order_id_so_a_partial_fill_applies(
        pg, owner_id):
    """Two executions of ONE order are one partial fill. `take()` groups on
    the order identity, which only works if build_ledger carries it --
    otherwise the second execution is stranded in leftovers() forever, the
    strategy permanently understates a real holding, and the executor sells
    a remainder it thinks it does not own."""
    _seed(pg, owner_id)
    with pg() as s:
        for eid, qty, px, sec in (("e1", 7.0, 50.25, 41), ("e2", 5.0, 50.26, 43)):
            s.add(persistence.Execution(
                connection_id="c1", deployment_id="d1", broker_exec_id=eid,
                broker_order_id="o1", symbol="TQQQ", signed_qty=qty, price=px,
                fees=0.0, filled_at=datetime(2026, 8, 24, 13, 31, sec,
                                             tzinfo=timezone.utc),
                source="broker"))
        s.commit()
    with pg() as s:
        led = deployment_store.build_ledger(s, _dep(s))
    got = led.take(date(2026, 8, 24), "TQQQ", None)
    assert [(g.qty, g.price) for g in got] == [(7, 50.25), (5, 50.26)]
    assert led.leftovers() == []


def test_unknown_symbols_includes_whole_universe_on_a_recorded_poll_error(
        pg, owner_id):
    """The shape the sweep records when its executions poll fails:
    position["execution"]["executions"]["error"]."""
    _seed(pg, owner_id,
          position={"execution": {"executions": {"error": "broker down"}}})
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == {"TQQQ", "SPY"}


def test_unknown_symbols_includes_todays_unresolved_broker_order(pg, owner_id):
    """A market delta is recorded with deployment_id=None -- the shape the
    executor actually produces. Keyed only on this deployment's id, a market
    order submitted at sweep N and not yet in the ledger when tick N+1 runs
    would be absent from the unknown set: take() returns [] on a reconciled
    day, the strategy stays flat, and sweep N+1 -- computing want=0 against a
    real long position -- SELLS it at market. A spurious real round trip on
    the very order type this exists to fix."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id=None, symbol="TQQQ", qty=10,
            side="buy", order_type="market", action="submit",
            status="pending_new"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == {"TQQQ"}


def test_unknown_symbols_ignores_null_dep_orders_outside_the_ir(pg, owner_id):
    """The NULL-deployment union is restricted to symbols this strategy can
    actually trade -- another deployment's (or the account holder's own)
    symbol must not freeze this one."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id=None, symbol="AAPL", qty=10,
            side="buy", order_type="market", action="submit",
            status="pending_new"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_unknown_symbols_ignores_null_dep_orders_on_another_connection(
        pg, owner_id):
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c2", user_id=owner_id,
                                           broker="alpaca",
                                           execution_truth="enforce"))
        s.flush()
        s.add(persistence.BrokerOrder(
            connection_id="c2", deployment_id=None, symbol="TQQQ", qty=10,
            side="buy", order_type="market", action="submit",
            status="pending_new"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_a_null_dep_market_order_resolves_once_its_fill_lands(pg, owner_id):
    """Same resolution rule as an attributed order: once a confirming
    Execution exists the symbol leaves the unknown set, so a same-day fill
    still displays at the broker's real price."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id=None, symbol="TQQQ", qty=10,
            side="buy", order_type="market", action="submit",
            status="pending_new", client_order_id="sl-mkt-TQQQ-abc"))
        s.add(persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_exec_id="e9",
            client_order_id="sl-mkt-TQQQ-abc", symbol="TQQQ",
            signed_qty=10.0, price=50.25, fees=0.0,
            filled_at=datetime(2026, 8, 24, 13, 31, 41, tzinfo=timezone.utc),
            source="broker"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_unknown_symbols_excludes_terminal_orders_and_clean_polls(pg, owner_id):
    _seed(pg, owner_id,
          position={"execution": {"executions": {"error": None}}})
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id="d1", symbol="TQQQ", qty=10,
            side="buy", order_type="market", action="submit",
            status="filled"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_unknown_symbols_on_poll_error_covers_weight_tree_assets_too(
        pg, owner_id):
    """A rotation strategy's held symbols can come from weight-tree `asset`
    nodes outside universe.static (the replay unions the two for exactly
    this reason, and collect_ir_symbols exists to compute that union). A
    poll error must mark ALL of them unknown, or a held
    weight-tree-selected symbol slips through the one safety net this
    function provides."""
    _seed(pg, owner_id,
          ir={"universe": {"static": ["TQQQ"]}, "params": {},
              "rules": [{"id": "rotate", "trigger": {"type": "session_open"},
                         "action": {"type": "set_weights",
                                    "weights": {"equal": [{"asset": "XLK"}]}}}]},
          position={"execution": {"executions": {"error": "broker down"}}})
    with pg() as s:
        unk = deployment_store._unknown_symbols(s, _dep(s))
    assert "XLK" in unk, \
        "weight-tree asset outside universe.static must be marked unknown too"
    assert "TQQQ" in unk


def test_unknown_symbols_degrades_on_unexpandable_ir_instead_of_raising(
        pg, owner_id):
    """`collect_ir_symbols` is tolerant, but not total -- a non-dict
    `universe` raises. The executions module already guards the equivalent
    call by degrading to `universe.static`; this path must do the same
    rather than take a whole fleet poll down with one malformed IR."""
    _seed(pg, owner_id, ir={"universe": "TQQQ"})
    with pg() as s:
        # no orders today, and no exception raised
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_unknown_symbols_resolves_a_todays_order_once_a_fill_lands(
        pg, owner_id):
    """BrokerOrder rows are insert-only -- `status` is only ever the
    synchronous submit-time acknowledgment and nothing ever advances it. If
    that stale status were the only resolution signal, a symbol traded today
    would stay unknown for the rest of the trading day even after a
    confirming Execution landed, defeating the feature on the one day
    (same-day fill display) it exists for."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id="d1", symbol="TQQQ", qty=10,
            side="buy", order_type="market", action="submit",
            status="pending_new", client_order_id="cid-1"))
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id="d1", symbol="SPY", qty=5,
            side="buy", order_type="market", action="submit",
            status="pending_new", client_order_id="cid-2"))
        # only TQQQ's order has a confirming execution
        s.add(persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_exec_id="e1",
            client_order_id="cid-1", symbol="TQQQ", signed_qty=10.0,
            price=50.25, fees=0.0,
            filled_at=datetime(2026, 8, 24, 13, 31, 41, tzinfo=timezone.utc),
            source="broker"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == {"SPY"}, \
            "TQQQ has a matching fill and must be resolved; SPY has none"


def test_cancel_row_resolves_pendency(pg, owner_id):
    """A canceled resting order leaves an unresolved submit row behind. Left
    in flight forever it keeps its symbol unknowable -- and the displayed
    basis at the model price -- for the rest of the day. A cancel row
    resolves its order."""
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id="d1", symbol="TQQQ", qty=12,
            side="sell", order_type="limit", action="submit",
            status="submitted", broker_order_id="B1",
            client_order_id="sl-tp-d1-x1"))
        s.add(persistence.BrokerOrder(
            connection_id="c1", deployment_id=None, symbol="TQQQ", qty=12,
            side="sell", order_type="limit", action="cancel",
            broker_order_id="B1"))
        s.commit()
    with pg() as s:
        assert deployment_store._unknown_symbols(s, _dep(s)) == set()


def test_resting_order_pendency_never_rewrites_settled_history(pg, owner_id):
    """The basis flap, pinned end to end through build_ledger: a resting
    order pending today makes TODAY unknowable for its symbol -- while
    yesterday's confirmed fill still reads at the broker price, so the cost
    basis (and every order level derived from it) cannot flap."""
    from zoneinfo import ZoneInfo
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_exec_id="e-hist",
            symbol="TQQQ", signed_qty=12.0, price=50.25,
            filled_at=datetime(2026, 8, 24, 13, 31, 41, tzinfo=timezone.utc),
            rule_tag="weekly-entry", source="broker"))
        s.add(persistence.BrokerOrder(   # resting sell, pending right now
            connection_id="c1", deployment_id="d1", symbol="TQQQ", qty=12,
            side="sell", order_type="limit", action="submit",
            status="submitted", client_order_id="sl-tp-d1-rest"))
        s.commit()
    with pg() as s:
        led = deployment_store.build_ledger(s, _dep(s))
    assert "TQQQ" in led.unknown, "the resting order IS pending"
    got = led.take(date(2026, 8, 24), "TQQQ", "weekly-entry")
    assert [(g.qty, g.price) for g in got] == [(12, 50.25)], \
        "settled history stays broker-priced despite the pendency"
    et_today = datetime.now(ZoneInfo("America/New_York")).date()
    assert led.take(et_today, "TQQQ", "weekly-entry") is None, \
        "today's absence is unknowable while the order rests"
