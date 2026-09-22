"""The rows port: one tick's transaction over one deployment row.

Two invariants, and both of them are about what happens when something
goes wrong.

A transaction that ends without a decision must leave the row alone. That
is how a runner outage ends: no error recorded, nothing rewritten, the last
good payload kept for the next tick. A store that committed on the way out
would mark every strategy errored during a deploy of the runner.

And a ledger that cannot be built must RAISE. `None` already means "this
connection is not in enforce", which makes the engine keep its own model
fills; a failure that borrowed that meaning would silently demote a real
account to modelled fills.
"""
from datetime import date

import pytest

from dqengine.live import deployment_store, persistence


def _dep(pg, owner_id, **kw):
    with pg() as s:
        d = persistence.Deployment(
            user_id=owner_id, name="p", kind="python", code="x",
            ir=None, universe=["TQQQ"], status="running",
            cash_initial=1000.0, start_date=date(2026, 8, 24),
            position={"qty": 9, "execution": {"error": "keep me"}},
            stats={"end_equity": 123.0}, **kw)
        s.add(d)
        s.commit()
        return d.id


def test_a_transaction_that_ends_without_a_decision_leaves_the_row_alone(pg, owner_id):
    dep_id = _dep(pg, owner_id)
    with deployment_store.SqlDeploymentStore().open(dep_id) as tx:
        assert tx.dep.id == dep_id
    with pg() as s:
        d = s.get(persistence.Deployment, dep_id)
    assert d.tick_error is None and d.last_tick is None
    assert d.position["qty"] == 9 and d.stats == {"end_equity": 123.0}


def test_a_committed_payload_lands_whole_and_clears_the_error(pg, owner_id):
    dep_id = _dep(pg, owner_id, tick_error="an older failure")
    out = {"stats": {"end_equity": 5.0}, "equity": {"days": [], "values": []},
           "position": {"qty": 1}, "fills": [], "journal": [{"log": "x"}]}
    with deployment_store.SqlDeploymentStore().open(dep_id) as tx:
        tx.commit_payload(out)
    with pg() as s:
        d = s.get(persistence.Deployment, dep_id)
    assert d.stats == out["stats"] and d.position == {"qty": 1}
    assert d.journal == [{"log": "x"}] and d.tick_error is None
    assert d.last_tick is not None


def test_a_committed_error_keeps_the_last_good_payload(pg, owner_id):
    dep_id = _dep(pg, owner_id)
    with deployment_store.SqlDeploymentStore().open(dep_id) as tx:
        tx.commit_error("it exploded")
    with pg() as s:
        d = s.get(persistence.Deployment, dep_id)
    assert d.tick_error == "it exploded" and d.last_tick is not None
    assert d.position["qty"] == 9 and d.stats == {"end_equity": 123.0}


def test_the_cash_events_arrive_in_effective_date_order(pg, owner_id):
    dep_id = _dep(pg, owner_id)
    with pg() as s:
        for day in (date(2026, 9, 3), date(2026, 8, 25)):
            s.add(persistence.SleeveEvent(deployment_id=dep_id, kind="deposit",
                                    amount=10.0, effective_date=day))
        s.commit()
    with deployment_store.SqlDeploymentStore().open(dep_id) as tx:
        assert [e.effective_date for e in tx.events] == [date(2026, 8, 25),
                                                         date(2026, 9, 3)]


def test_a_ledger_that_cannot_be_built_raises_rather_than_reading_as_no_ledger(
        pg, owner_id, monkeypatch):
    """None means "this connection is not in enforce". A failure must never
    borrow that meaning (hazard H6)."""
    dep_id = _dep(pg, owner_id)

    def boom(session, dep):
        raise RuntimeError("executions table unreachable")
    monkeypatch.setattr(deployment_store, "build_ledger", boom)
    with pg() as s:
        dep = s.get(persistence.Deployment, dep_id)
        with pytest.raises(RuntimeError, match="unreachable"):
            deployment_store.SqlDeploymentStore().ledger(dep)
