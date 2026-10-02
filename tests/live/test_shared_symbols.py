"""The open half of several deployments trading one symbol in one account.

The engine runs one deployment per connection and never splits a fill
itself. What it owns is everything a host needs to do so safely: a fill of
a netted order on a shared symbol is stored as nobody's (`source="shared"`),
a deployment's ledger includes its `fill_allocations` rows, a shared symbol
is never read as "the broker did not fill" on the day, the fold rails are
told about transfers and released shares, and the sweep calls the host's
allocator right after it polls.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dqengine.live import deployment_store, executions, executor, persistence
from dqengine.runtime.core.ledger import ExecutionLedger

ET = ZoneInfo("America/New_York")
WHEN = datetime(2026, 9, 30, 19, 59, 1, tzinfo=timezone.utc)   # 15:59:01 ET


@pytest.fixture(autouse=True)
def no_hooks():
    executions.set_extra_holders(None)
    executor.set_fill_allocator(None)
    yield
    executions.set_extra_holders(None)
    executor.set_fill_allocator(None)


def _row(exec_id, cid="", sym="SPY", side="buy", qty=30.0, px=500.0):
    return {"broker_order_id": "", "broker_exec_id": exec_id,
            "client_order_id": cid, "symbol": sym, "side": side,
            "qty": qty, "price": px, "fees": 0.0, "filled_at": WHEN,
            "order_level_avg": False}


def _seed(pg, owner_id, deps=(("da", ("SPY",)), ("db", ("SPY", "QQQ"))),
          truth="enforce", conn="cs"):
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id=conn, user_id=owner_id, broker="alpaca",
            execution_truth=truth))
        for dep_id, universe in deps:
            s.add(persistence.Deployment(
                id=dep_id, user_id=owner_id, name=dep_id, kind="python",
                ir=None, code="pass", universe=list(universe),
                status="running", broker_connection_id=conn,
                cash_initial=1000.0, start_date=date(2026, 9, 1),
                reconciled_from=date(2026, 9, 1)))
        s.commit()


# ------------------------------------------------------------ attribution

def test_our_own_netted_fill_on_a_shared_symbol_is_nobodys(pg, owner_id):
    _seed(pg, owner_id)
    with pg() as s:
        executions.store(s, "cs", [_row("e1", cid="sl-mkt-SPY-ab12cd34")])
        s.commit()
        e = s.query(persistence.Execution).one()
        assert e.deployment_id is None and e.source == "shared"


def test_a_manual_trade_on_a_shared_symbol_stays_manual(pg, owner_id):
    """Not our order: the account holder traded their own account."""
    _seed(pg, owner_id)
    with pg() as s:
        executions.store(s, "cs", [_row("e1", cid="")])
        s.commit()
        e = s.query(persistence.Execution).one()
        assert e.deployment_id is None and e.source == "manual"


def test_a_symbol_one_deployment_trades_is_attributed_as_before(pg, owner_id):
    _seed(pg, owner_id)
    with pg() as s:
        executions.store(s, "cs", [_row("e1", cid="sl-mkt-QQQ-ab12cd34",
                                        sym="QQQ")])
        s.commit()
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "db" and e.source == "broker"


def test_an_extra_holder_keeps_a_fill_from_the_one_managed_holder(
        pg, owner_id):
    """A stopped deployment being released still holds QQQ. Its liquidation
    fill must not land in the ledger of the one managed deployment that
    happens to trade QQQ too."""
    _seed(pg, owner_id)
    executions.set_extra_holders(lambda s, conn: {"gone": {"QQQ"}})
    with pg() as s:
        executions.store(s, "cs", [_row("e1", cid="sl-mkt-QQQ-ab12cd34",
                                        sym="QQQ", side="sell")])
        s.commit()
        e = s.query(persistence.Execution).one()
        assert e.deployment_id is None and e.source == "shared"
        assert executions.shared_symbols(s, "cs")["QQQ"] == ["gone", "db"]


def test_shared_symbols_names_the_deployments(pg, owner_id):
    _seed(pg, owner_id)
    with pg() as s:
        assert executions.shared_symbols(s, "cs") == {"SPY": ["da", "db"]}


# ------------------------------------------------------------- the ledger

def _alloc(pg, dep, qty, px, when=WHEN, exec_id=None, group="ag:1",
           conn="cs", sym="SPY", cross=None):
    with pg() as s:
        s.add(persistence.FillAllocation(
            connection_id=conn, deployment_id=dep, execution_id=exec_id,
            symbol=sym, signed_qty=qty, price=px, fees=0.0, filled_at=when,
            group_id=group, cross_id=cross))
        s.commit()


def test_a_deployments_ledger_includes_its_allocation_rows(pg, owner_id):
    _seed(pg, owner_id)
    _alloc(pg, "db", 15, 500.10, group="ag:b")
    _alloc(pg, "db", 10, 500.00, when=WHEN + timedelta(seconds=4),
           group="ag:b", cross="x:1")
    _alloc(pg, "da", -10, 500.00, when=WHEN + timedelta(seconds=4),
           group="ag:a", cross="x:1")
    with pg() as s:
        led = deployment_store.build_ledger(
            s, s.get(persistence.Deployment, "db"))
    assert isinstance(led, ExecutionLedger)
    rows = led.take(date(2026, 9, 30), "SPY", "py:SPY:7", side=1)
    # ONE ticket takes the venue share and the transfer together
    assert sorted((f.qty, f.price) for f in rows) == [(10, 500.0),
                                                      (15, 500.10)]
    assert {f.broker_order_id for f in rows} == {"ag:b"}
    assert rows[0].time_ms == (15 * 3600 + 59 * 60 + 1) * 1000


def test_two_groups_on_one_day_go_to_two_tickets(pg, owner_id):
    _seed(pg, owner_id)
    _alloc(pg, "db", 5, 500.0, group="ag:1")
    _alloc(pg, "db", 7, 501.0, when=WHEN + timedelta(minutes=30),
           group="ag:2")
    with pg() as s:
        led = deployment_store.build_ledger(
            s, s.get(persistence.Deployment, "db"))
    day = date(2026, 9, 30)
    assert [f.qty for f in led.take(day, "SPY", "py:SPY:1", side=1)] == [5]
    assert [f.qty for f in led.take(day, "SPY", "py:SPY:2", side=1)] == [7]


def test_a_shared_symbol_is_unknown_today_never_a_no_fill(pg, owner_id):
    """Between the venue fill and the host's split there is no row for this
    deployment. On the replay path a confirmed no-fill would drop the
    position and the next sweep would sell real shares."""
    _seed(pg, owner_id)
    with pg() as s:
        dep = s.get(persistence.Deployment, "db")
        assert "SPY" in deployment_store._unknown_symbols(s, dep)
        assert "QQQ" not in deployment_store._unknown_symbols(s, dep)
        led = deployment_store.build_ledger(s, dep)
    today = datetime.now(ET).date()
    assert led.take(today, "SPY", "py:SPY:1", side=1) is None
    assert led.take(today, "QQQ", "py:QQQ:1", side=1) == []
    # a settled day still settles
    assert led.take(today - timedelta(days=3), "SPY", "r", side=1) == []


# --------------------------------------------------------------- the rails

def test_the_rails_are_told_about_transfers_and_released_shares(
        pg, owner_id):
    now = datetime.now(timezone.utc)
    _seed(pg, owner_id)
    with pg() as s:
        s.add(persistence.Deployment(
            id="gone", user_id=owner_id, name="gone", kind="python",
            ir=None, code="pass", universe=["SPY"], status="stopped",
            broker_connection_id="cs", cash_initial=1000.0,
            start_date=date(2026, 9, 1)))
        s.add(persistence.Execution(
            id="ex1", connection_id="cs", broker_exec_id="e1",
            client_order_id="sl-mkt-SPY-1", symbol="SPY", signed_qty=-12,
            price=500.0, filled_at=now, source="shared"))
        s.commit()
    _alloc(pg, "da", -10, 500.0, when=now, group="g1", cross="x")   # transfer
    _alloc(pg, "db", 10, 500.0, when=now, group="g2", cross="x")
    _alloc(pg, "gone", -12, 500.0, when=now, group="g3", exec_id="ex1")
    _alloc(pg, "db", 4, 500.0, when=now, group="g4", exec_id="ex1")
    with pg() as s:
        adj = executor._allocation_rail_adjust(s, "cs", {"da", "db"})
    # managed transfers come OUT of what the sleeves folded; the stopped
    # deployment's venue share is counted as folded; a managed deployment's
    # venue share is its own to fold and changes nothing here
    assert adj == {"SPY": {"buy": -10.0, "sell": 2.0, "net": -12.0}}


# ---------------------------------------------------- the allocator's hook

def _sweep_rig(pg, owner_id, monkeypatch, truth):
    from rig import FakeBroker, seed
    from dqengine.live import book as _book
    seed(pg, owner_id, conn_id="ch", dep_id="dh", truth=truth)
    with pg() as s:
        d = s.get(persistence.Deployment, "dh")
        d.ir, d.universe, d.live_confirmed = None, ["SPY"], True
        s.commit()
    fb = FakeBroker(positions={"SPY": 5.0})
    monkeypatch.setattr(_book, "FAST_PATH", False)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    executor._GATHER_CACHE.pop("ch", None)
    executor._SYNC_GATE.pop("ch", None)


def test_an_enforce_audit_sweep_calls_the_allocator_and_reports_it(
        pg, owner_id, monkeypatch):
    calls = []

    def allocator(conn_id, positions, desired):
        calls.append((conn_id, positions, dict(desired)))
        return {"actions": ["SPY split"], "errors": []}
    executor.set_fill_allocator(allocator)
    _sweep_rig(pg, owner_id, monkeypatch, "enforce")
    executor.sync_broker_account("ch", fast=False)
    # told what the sweep saw at the venue and what it reconciled to
    assert calls == [("ch", {"SPY": 5.0}, {"SPY": 5.0})]
    with pg() as s:
        rep = s.get(persistence.Deployment, "dh").position["execution"]
    assert "SPY split" in rep["actions"]


def test_the_allocator_is_not_called_outside_enforce(pg, owner_id,
                                                     monkeypatch):
    calls = []
    executor.set_fill_allocator(lambda c, p, d: calls.append(c))
    _sweep_rig(pg, owner_id, monkeypatch, "observe")
    executor.sync_broker_account("ch", fast=False)
    assert calls == []


def test_an_allocator_that_raises_is_an_error_line_not_a_dead_sweep(
        pg, owner_id, monkeypatch):
    def boom(conn_id, positions, desired):
        raise RuntimeError("db down")
    executor.set_fill_allocator(boom)
    _sweep_rig(pg, owner_id, monkeypatch, "enforce")
    assert executor.sync_broker_account("ch", fast=False) == "audited"
    with pg() as s:
        rep = s.get(persistence.Deployment, "dh").position["execution"]
    assert any("fill allocation failed" in e for e in rep["errors"])


# ------------------------------------------------- what the payload says

def test_a_state_says_whose_it_is():
    st = executor.desired_from_payload("dx", {"holdings": []}, ["SPY"])
    assert st.owner == "dx"


def test_the_daily_preview_reports_the_tickets_it_added():
    from dqengine.live.driver import engine
    now = datetime(2026, 9, 30, 15, 59, 5, tzinfo=ET)
    res = {"daily_live": {"today": "2026-09-30", "bar_applied": False},
           "fills": [], "position": {"last_prices": {"SPY": 500.0}}}
    orders = [{"type": "market_on_close", "symbol": "SPY", "qty": 25,
               "order_id": 3, "tag": ""}]
    detail: dict = {}
    holdings, close_orders, _ = engine.daily_preview(
        res, [], orders, now, broker=True, detail=detail)
    assert detail["ticket_add"] == {"SPY": 25}
    assert detail["held_out"] == {}
    assert [(h["symbol"], h["qty"]) for h in holdings] == [("SPY", 25)]


def test_the_daily_preview_reports_what_it_holds_out():
    from dqengine.live.driver import engine
    now = datetime(2026, 9, 30, 17, 0, 0, tzinfo=ET)
    res = {"daily_live": {"today": "2026-09-30", "bar_applied": True},
           "fills": [{"day": "2026-09-30", "ms": 16 * 3600 * 1000,
                      "sym": "SPY", "qty": 25, "confirmed": False}],
           "position": {"last_prices": {"SPY": 500.0}}}
    detail: dict = {}
    holdings, _, _ = engine.daily_preview(
        res, [{"symbol": "SPY", "qty": 25, "last_price": 500.0}], [], now,
        broker=True, detail=detail)
    assert detail == {"ticket_add": {}, "held_out": {"SPY": 25}}
    assert holdings == []
