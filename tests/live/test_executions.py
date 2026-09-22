"""Executions ledger: one row per broker execution, unique per
(connection_id, broker_exec_id) so replayed/duplicated fill notifications
don't double-count. Also covers the execution_truth flag default on
BrokerConnection.

Execution.deployment_id is a real foreign key, so a fill needs a deployment
row behind it before it can be stored. Explicit ids like id="c1"/id="d1" are
the test's own handles; `owner_id` is whoever owns them -- nothing here, and
a real user on a host that has users.
"""
from datetime import date, datetime, timezone
from dqengine.live import persistence


_IR = {"version": "0.1", "universe": {"static": ["TQQQ"]},
       "params": {}, "rules": []}


def _seed(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="alpaca"))
        s.add(persistence.Deployment(id="d1", user_id=owner_id, name="copy", ir=_IR,
                               cash_initial=1000.0,
                               start_date=date(2026, 8, 18)))
        s.commit()


def test_execution_row_is_unique_per_broker_exec_id(pg, owner_id):
    from sqlalchemy.exc import IntegrityError

    def row(exec_id):
        return persistence.Execution(
            connection_id="c1", deployment_id="d1", broker_order_id="o1",
            broker_exec_id=exec_id, client_order_id="sl-mkt-TQQQ-ab-1234",
            symbol="TQQQ", signed_qty=100.0, price=50.0, fees=0.02,
            filled_at=datetime(2026, 1, 5, 15, 0, 0, tzinfo=timezone.utc),
            rule_tag="weekly-entry", source="broker")

    _seed(pg, owner_id)
    with pg() as s:
        s.add(row("x1"))
        s.commit()
    with pg() as s:
        s.add(row("x1"))
        try:
            s.commit()
        except IntegrityError:
            return
    raise AssertionError("duplicate (connection_id, broker_exec_id) accepted")


def test_connection_defaults_to_execution_truth_off(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c2", user_id=owner_id, broker="alpaca"))
        s.commit()
    with pg() as s:
        assert s.get(persistence.BrokerConnection, "c2").execution_truth == "off"
