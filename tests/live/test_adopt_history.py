"""Pulling a broker account's execution history into the ledger. plan()
must write nothing — it reads the broker's history and reports what would
change, so the user can review it against their live position before
apply() ever restates an open entry_price (which moves every entry-derived
stop/take-profit at the venue on the next sync)."""
from datetime import date, datetime, timezone

from dqengine.adapters.base import ExecutionBatch
from dqengine.live import adopt as bf
from dqengine.live import persistence, vault

_IR = {"universe": {"static": ["TQQQ"]}}


def _seed(pg, owner_id, *, start_date=date(2026, 1, 2),
          position=None, execution_truth="observe"):
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c1", user_id=owner_id, broker="alpaca",
            execution_truth=execution_truth,
            creds_encrypted=vault.encrypt_creds({"key_id": "k",
                                                 "secret_key": "s"})))
        s.add(persistence.Deployment(
            id="d1", user_id=owner_id, name="F", start_date=start_date,
            ir=_IR, status="running",
            broker_connection_id="c1", cash_initial=10000.0,
            position=position if position is not None else
            {"symbol": "TQQQ", "qty": 100, "entry_price": 49.5}))
        s.commit()
        return owner_id


_ONE_FILL = [{
    "broker_order_id": "o1", "broker_exec_id": "e1",
    "client_order_id": "", "symbol": "TQQQ", "side": "buy", "qty": 100.0,
    "price": 50.0, "fees": 0.0, "order_level_avg": False,
    "filled_at": datetime(2026, 1, 5, 15, 0, 0, tzinfo=timezone.utc)}]


def test_plan_reports_moved_entry_prices_without_writing(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    out = bf.plan("c1")
    assert out["executions_found"] == 1
    moved = out["entry_price_changes"]
    assert moved and moved[0]["symbol"] == "TQQQ"
    assert moved[0]["from"] == 49.5 and moved[0]["to"] == 50.0
    with pg() as s:
        assert s.query(persistence.Execution).count() == 0    # dry run wrote nothing
        assert s.query(persistence.Deployment).get("d1").reconciled_from is None
        assert s.query(persistence.Deployment).get("d1").position["entry_price"] \
            == 49.5    # unchanged


def test_plan_reports_no_change_when_price_matches(pg, owner_id, monkeypatch):
    _seed(pg, owner_id, position={"symbol": "TQQQ", "qty": 100, "entry_price": 50.0})
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    out = bf.plan("c1")
    assert out["entry_price_changes"] == []


def test_plan_reports_earliest_execution_and_reconciled_from_boundary(
        pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    out = bf.plan("c1")
    assert out["earliest"] == datetime(2026, 1, 5, 15, 0, 0,
                                       tzinfo=timezone.utc)
    assert out["reconciled_from"] == [
        {"deployment_id": "d1", "date": "2026-01-05"}]


def test_plan_flags_deployment_the_history_does_not_reach(pg, owner_id, monkeypatch):
    # deployment started well before the broker's earliest returned fill --
    # everything before that fill stays model-priced. plan() must say so.
    _seed(pg, owner_id, start_date=date(2025, 6, 1))
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    out = bf.plan("c1")
    assert out["coverage_gaps"] == [{
        "deployment_id": "d1", "start_date": "2025-06-01",
        "earliest_execution": "2026-01-05"}]


def test_plan_with_no_executions_is_honest_and_writes_nothing(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: [])

    out = bf.plan("c1")
    assert out == {"connection_id": "c1", "executions_found": 0,
                    "earliest": None, "entry_price_changes": [],
                    "reconciled_from": [], "coverage_gaps": [], "skipped": 0}
    with pg() as s:
        assert s.query(persistence.Execution).count() == 0


def test_plan_reports_nonzero_skip_count(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    batch = ExecutionBatch(_ONE_FILL)
    batch.skipped = 2
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: batch)

    out = bf.plan("c1")
    assert out["skipped"] == 2


def test_apply_refuses_when_fetch_skipped_rows(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    batch = ExecutionBatch(_ONE_FILL)
    batch.skipped = 1
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: batch)

    try:
        bf.apply("c1")
        assert False, "apply() should have refused"
    except bf.SkippedRowsError:
        pass
    with pg() as s:
        assert s.query(persistence.Execution).count() == 0
        assert s.query(persistence.Deployment).get("d1").reconciled_from is None


def test_apply_proceeds_when_no_rows_skipped(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    batch = ExecutionBatch(_ONE_FILL)
    batch.skipped = 0
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: batch)

    out = bf.apply("c1")
    assert out["stored"] == 1
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1


def test_apply_stores_executions_and_sets_reconciled_from(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    out = bf.apply("c1")
    assert out["stored"] == 1
    with pg() as s:
        rows = s.query(persistence.Execution).all()
        assert len(rows) == 1
        assert rows[0].broker_exec_id == "e1"
        assert rows[0].symbol == "TQQQ"
        dep = s.query(persistence.Deployment).get("d1")
        assert dep.reconciled_from == date(2026, 1, 5)


def test_apply_is_idempotent(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    bf.apply("c1")
    out2 = bf.apply("c1")
    assert out2["stored"] == 0
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1


def test_cli_apply_without_yes_refuses_and_writes_nothing(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    rc = bf._cli(["backfill_executions.py", "apply", "c1"])
    assert rc != 0
    with pg() as s:
        assert s.query(persistence.Execution).count() == 0
        assert s.query(persistence.Deployment).get("d1").reconciled_from is None


def test_cli_apply_with_yes_writes(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    rc = bf._cli(["backfill_executions.py", "apply", "c1", "--yes"])
    assert rc == 0
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1
        assert s.query(persistence.Deployment).get("d1").reconciled_from \
            == date(2026, 1, 5)


def test_cli_plan_needs_no_flag_and_writes_nothing(pg, owner_id, monkeypatch):
    _seed(pg, owner_id)
    monkeypatch.setattr(bf, "_fetch_history", lambda *a, **k: _ONE_FILL)

    rc = bf._cli(["backfill_executions.py", "plan", "c1"])
    assert rc == 0
    with pg() as s:
        assert s.query(persistence.Execution).count() == 0
