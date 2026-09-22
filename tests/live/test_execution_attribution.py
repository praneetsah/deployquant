from datetime import date, datetime, timedelta, timezone
from dqengine.live import persistence


def _row(exec_id, order_id="", cid="", sym="TQQQ", side="buy", qty=100.0,
         px=50.0, when=None):
    return {"broker_order_id": order_id, "broker_exec_id": exec_id,
            "client_order_id": cid, "symbol": sym, "side": side,
            "qty": qty, "price": px, "fees": 0.0,
            "filled_at": when or datetime(2026, 1, 5, 15, 0, 0,
                                          tzinfo=timezone.utc),
            "order_level_avg": False}


def _seed(pg, owner_id, universe=("TQQQ",)):
    """One connection with one running deployment holding `universe`.
    `owner_id` is whoever owns the rows -- nothing here, a real user on a
    host that has users."""
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="alpaca"))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="F",
            ir={"universe": {"static": list(universe)}}, status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1)))
        s.commit()


def test_attributes_by_broker_order_id(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(connection_id="c1", deployment_id="d1",
                                broker_order_id="o1", symbol="TQQQ",
                                qty=100.0, side="buy", order_type="market",
                                action="submit"))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", order_id="o1")]) == 1
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1" and e.source == "broker"
        assert e.signed_qty == 100.0


def test_order_id_match_beats_symbol_match(pg, owner_id):
    """Adversarial precedence check: deployment A has the broker_order_id
    match, deployment B is the sole universe-holder of the same symbol.
    Step 1 must win over step 3 — nothing else would catch a reordering
    of those branches."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id, universe=("SPY",))       # d1 ("A") does NOT hold TQQQ
    with pg() as s:
        uid = s.query(persistence.Deployment).filter_by(id="d1").one().user_id
        s.add(persistence.Deployment(                       # d2 ("B") holds TQQQ
            id="d2", user_id=uid, name="G",
            ir={"universe": {"static": ["TQQQ"]}}, status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1)))
        s.add(persistence.BrokerOrder(connection_id="c1", deployment_id="d1",
                                broker_order_id="o1", symbol="TQQQ",
                                qty=100.0, side="buy", order_type="market",
                                action="submit"))
        s.commit()
    with pg() as s:
        ex.store(s, "c1", [_row("e1", order_id="o1", sym="TQQQ")])
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1"          # order-id match, not B


def test_attributes_by_client_order_id_alone(pg, owner_id):
    """Step 2 in isolation: the fill carries no broker_order_id, only a
    client_order_id that matches a broker_orders row."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.BrokerOrder(connection_id="c1", deployment_id="d1",
                                client_order_id="cid1", symbol="TQQQ",
                                qty=100.0, side="buy", order_type="market",
                                action="submit"))
        s.commit()
    with pg() as s:
        ex.store(s, "c1", [_row("e1", cid="cid1")])
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1" and e.source == "broker"


def test_sell_is_stored_signed_negative(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        ex.store(s, "c1", [_row("e1", side="sell", qty=100.0)])
        s.commit()
    with pg() as s:
        assert s.query(persistence.Execution).one().signed_qty == -100.0


def test_sole_holder_of_symbol_gets_the_fill(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        ex.store(s, "c1", [_row("e1")])
        s.commit()
    with pg() as s:
        assert s.query(persistence.Execution).one().deployment_id == "d1"


def test_paused_deployment_still_attributes_by_symbol(pg, owner_id):
    """A paused deployment still owns its broker position (the executor
    keeps maintaining its resting exits — executor.py:1479 manages
    running AND paused deployments), so a fill on its sole symbol must
    attribute to it, not fall through to manual."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        s.query(persistence.Deployment).filter_by(id="d1").update(
            {"status": "paused"})
        s.commit()
    with pg() as s:
        ex.store(s, "c1", [_row("e1")])
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1" and e.source == "broker"


def test_unknown_symbol_is_manual_not_guessed(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        ex.store(s, "c1", [_row("e1", sym="NVDA")])
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id is None and e.source == "manual"


def test_ambiguous_symbol_is_manual_not_split(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:                       # a SECOND deployment holding TQQQ
        uid = s.query(persistence.Deployment).filter_by(id="d1").one().user_id
        s.add(persistence.Deployment(
            id="d2", user_id=uid, name="G",
            ir={"universe": {"static": ["TQQQ"]}}, status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1)))
        s.commit()
    with pg() as s:
        ex.store(s, "c1", [_row("e1")])
        s.commit()
    with pg() as s:
        assert s.query(persistence.Execution).one().deployment_id is None


def test_store_is_idempotent(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    rows = [_row("e1")]
    with pg() as s:
        assert ex.store(s, "c1", rows) == 1
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", rows) == 0
        s.commit()
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1


def test_within_batch_duplicate_broker_exec_id_inserts_once(pg, owner_id):
    """A single call to store() with two rows sharing a broker_exec_id must
    not double-insert — the query-based `seen` set is not enough on its
    own since both rows are new at query time; store() must also track
    what it has already added within the same batch."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    with pg() as s:
        n = ex.store(s, "c1", [_row("e1"), _row("e1")])
        s.commit()
    assert n == 1
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1


def test_poll_asks_from_last_fill_minus_overlap(pg, owner_id):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    last = datetime(2026, 1, 5, 15, 0, 0, tzinfo=timezone.utc)
    with pg() as s:
        ex.store(s, "c1", [_row("e1", when=last)])
        s.commit()
    seen = {}

    class A:
        def executions(self, creds, since=None):
            seen["since"] = since
            return []

    ex.poll(A(), {}, "c1")
    assert seen["since"] == last - timedelta(seconds=ex.OVERLAP_S)


# --- I6: a row the adapter could not parse is UNKNOWN, not "no fill" ------


def test_poll_reports_the_adapters_skip_count(pg, owner_id):
    """Each adapter skips a row it cannot parse, logs it, and returns the
    rest -- correct at the ADAPTER layer. But under `enforce` the absence of
    that row on a reconciled day is read by take() as an affirmative no-fill,
    which inverts this branch's own rule. poll() surfaces the count so the
    caller can widen the unknown set."""
    from dqengine.live import executions as ex
    from dqengine.adapters.base import ExecutionBatch
    _seed(pg, owner_id)

    class A:
        def executions(self, creds, since=None):
            return ExecutionBatch([_row("e1")], skipped=2)

    assert ex.poll(A(), {}, "c1") == (1, 2)


def test_poll_reports_skips_even_when_every_row_was_skipped(pg, owner_id):
    """The worst case, and the one an `or []` fallback silently swallows:
    the adapter parsed NOTHING. An empty batch is falsy, so the skip count
    has to be read off the adapter's own return before any `or []`
    substitutes a plain list for it -- otherwise the one poll that most
    needs to say UNKNOWN reports a clean, empty, confirmed no-fill."""
    from dqengine.live import executions as ex
    from dqengine.adapters.base import ExecutionBatch
    _seed(pg, owner_id)

    class A:
        def executions(self, creds, since=None):
            return ExecutionBatch([], skipped=3)

    assert ex.poll(A(), {}, "c1") == (0, 3)


def test_poll_reports_zero_skips_for_a_plain_list(pg, owner_id):
    """An adapter that returns a bare list (the base-class default, or a
    third-party one) reports no skips rather than blowing up."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id)

    class A:
        def executions(self, creds, since=None):
            return [_row("e1")]

    assert ex.poll(A(), {}, "c1") == (1, 0)


def test_execution_batch_is_a_list():
    """Every existing caller does `rows or []`, iterates, or compares to
    [] -- the batch must stay a plain list to all of them."""
    from dqengine.adapters.base import ExecutionBatch
    b = ExecutionBatch([1, 2], skipped=3)
    assert isinstance(b, list) and b == [1, 2] and b.skipped == 3
    assert ExecutionBatch([]) == [] and ExecutionBatch([]).skipped == 0


def test_universe_match_covers_weight_tree_assets(pg, owner_id):
    """I4: `_deployment_universes` read `ir.universe.static` while
    `_unknown_symbols` reads `collect_ir_symbols`. A fill on a
    weight-tree-selected symbol therefore landed as source="manual" with
    deployment_id NULL -- and under `enforce` a manual-bucketed fill is
    INDISTINGUISHABLE from no fill, because build_ledger filters on
    `Execution.deployment_id == dep.id`. Same flattening as C1, on any
    rotation/switcher strategy. The two must use the same symbol set."""
    from dqengine.live import executions as ex
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="alpaca"))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="F", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["TQQQ"]}, "params": {},
                "rules": [{"id": "rotate",
                           "trigger": {"type": "session_open"},
                           "action": {"type": "set_weights",
                                      "weights": {"equal": [
                                          {"asset": "XLK"}]}}}]}))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", sym="XLK")]) == 1
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1" and e.source == "broker", \
            "a weight-tree asset's fill must reach its sleeve, not `manual`"


def test_one_unexpandable_ir_does_not_abort_the_connections_poll(pg, owner_id):
    """`collect_ir_symbols` is tolerant, but not total -- a non-dict
    `universe` raises. One broken deployment must not take the whole
    connection's attribution down with it: every OTHER deployment's fills
    would then go unattributed, which under `enforce` reads as "the broker
    did not fill them". d2's fill must still reach d2."""
    from dqengine.live import executions as ex
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="alpaca"))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="broken", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1), ir={"universe": "TQQQ"}))
        s.add(persistence.Deployment(
            id="d2", user_id=owner_id, name="fine", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["SPY"]}}))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", sym="SPY")]) == 1
        s.commit()
    with pg() as s:
        assert s.query(persistence.Execution).one().deployment_id == "d2"


def test_rsi_gate_only_referencer_is_not_a_holder(pg, owner_id):
    """The shape that produced the bug: a single-symbol TQQQ strategy
    genuinely trades TQQQ; a many-ETF switcher on the same connection
    only reads TQQQ inside an RSI gate, never trades it. Both
    used to look like `collect_ir_symbols` holders of TQQQ, so a real
    TQQQ fill was ambiguous between two deployments and landed as
    `manual`. `collect_tradeable_symbols` must see only the real holder."""
    from dqengine.live import executions as ex
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="webull"))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="tqqq-real", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["TQQQ"]}, "params": {}, "rules": []}))
        s.add(persistence.Deployment(
            id="d2", user_id=owner_id, name="switcher-29etf", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["SPY", "QQQ"]}, "params": {},
                "rules": [{
                    "id": "rotate", "trigger": {"type": "session_open"},
                    "when": {"gt": [{"ind": "rsi", "symbol": "TQQQ"}, 50]},
                    "action": {"type": "set_weights", "weights": {
                        "equal": [{"asset": "SPY"}]}}}]}))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", sym="TQQQ", qty=100.0,
                                       px=50.0)]) == 1
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "d1" and e.source == "broker", \
            "an RSI-gate-only referencer must not make TQQQ ambiguous"


def test_expression_only_symbol_does_not_make_a_deployment_a_holder(pg, owner_id):
    """Unit-level companion to the production-shape test: a deployment
    whose IR references a symbol only inside a `when` gate must not be a
    holder of that symbol for attribution purposes."""
    from dqengine.live import executions as ex
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="alpaca"))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="gate-only", status="running",
            broker_connection_id="c1", cash_initial=1000.0,
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["SPY"]}, "params": {},
                "rules": [{
                    "id": "r", "trigger": {"type": "session_open"},
                    "when": {"gt": [{"ind": "rsi", "symbol": "TQQQ"}, 50]},
                    "action": {"type": "set_weights", "weights": {
                        "equal": [{"asset": "SPY"}]}}}]}))
        s.commit()
    with pg() as s:
        universes = ex._deployment_universes(s, "c1")
    assert "TQQQ" not in universes["d1"]
    assert "SPY" in universes["d1"]


# -------------------------------------------- warm+enforce stale signaling

class _FakeBus:
    def __init__(self):
        self.kv = {}

    def set_ex(self, key, value, ex_s):
        self.kv[key] = value

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)


def _with_bus(monkeypatch):
    from dqengine.live import bus as bus_mod
    b = _FakeBus()
    monkeypatch.setattr(bus_mod, "BUS", b)
    return b


def _set_truth(pg, mode):
    with pg() as s:
        s.get(persistence.BrokerConnection, "c1").execution_truth = mode
        s.commit()


def test_store_marks_warm_stale_for_enforce_attributed_rows(pg, owner_id, monkeypatch):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    _set_truth(pg, "enforce")
    bus = _with_bus(monkeypatch)
    with pg() as s:
        s.add(persistence.BrokerOrder(connection_id="c1", deployment_id="d1",
                                broker_order_id="o1", symbol="TQQQ",
                                qty=100.0, side="buy", order_type="market",
                                action="submit"))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", order_id="o1")]) == 1
        s.commit()
    assert bus.get("warm:stale:d1") == "broker executions ingested"


def test_store_does_not_mark_stale_under_observe(pg, owner_id, monkeypatch):
    from dqengine.live import executions as ex
    _seed(pg, owner_id)
    _set_truth(pg, "observe")
    bus = _with_bus(monkeypatch)
    with pg() as s:
        s.add(persistence.BrokerOrder(connection_id="c1", deployment_id="d1",
                                broker_order_id="o1", symbol="TQQQ",
                                qty=100.0, side="buy", order_type="market",
                                action="submit"))
        s.commit()
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1", order_id="o1")]) == 1
        s.commit()
    assert bus.kv == {}, "observe never touches accounting -> no signal"


def test_store_does_not_mark_stale_for_unattributed_rows(pg, owner_id, monkeypatch):
    """A NULL-deployment row (the user's own manual trade) belongs to no
    sleeve; no warm engine's state depends on it."""
    from dqengine.live import executions as ex
    _seed(pg, owner_id, universe=("SPY",))       # TQQQ matches no deployment
    _set_truth(pg, "enforce")
    bus = _with_bus(monkeypatch)
    with pg() as s:
        assert ex.store(s, "c1", [_row("e9")]) == 1
        s.commit()
    assert bus.kv == {}


# The same-process sibling of the test above -- a stale marker poked
# straight into the warm registry, so a process holding the engine
# converged with no bus round trip -- went with the IR warm registry.
# The python warm engine never had such a poke: in-process and in a worker
# alike it converges by CONSUMING the marker
# (driver.engine.warm_tick_python reads it), which
# test_store_marks_warm_stale_for_enforce_attributed_rows above pins from
# the producing end.
