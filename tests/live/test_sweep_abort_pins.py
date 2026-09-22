"""What is saved when a sync cycle fails partway through.

`sync_broker_account` wraps the whole cycle in a try, and after it -- whatever
happened -- runs a tail that (1) saves a `broker_orders` row for every order
already sent, (2) writes the cycle's report onto each deployment, (3) keeps an
`executions` block present, and on the fast path carries the PREVIOUS poll
verdict forward instead of overwriting it.

If that tail is lost or moved inside the try, an order that reached the broker
has no row. The ledger then has no "unknown" coverage for it, the strategy
concludes it never traded, and it buys again: a duplicate position.
"""
import pytest

from dqengine.live import executor
from dqengine.adapters.base import BrokerAuthExpired, BrokerUnavailable

from rig import blind_rig
from dqengine.live import persistence


def _raise_after_sending(monkeypatch, exc):
    """Let the real reconcile run (it submits and records), then fail."""
    real = executor.reconcile

    def wrapped(*a, **k):
        real(*a, **k)
        raise exc
    monkeypatch.setattr(executor, "reconcile", wrapped)


def _state(pg, conn, dep):
    with pg() as s:
        rows = [(r.symbol, r.side, r.qty, r.client_order_id)
                for r in s.query(persistence.BrokerOrder).filter_by(connection_id=conn).all()]
        c = s.get(persistence.BrokerConnection, conn)
        d = s.get(persistence.Deployment, dep)
        return rows, c.status, (d.position or {}).get("execution") or {}


@pytest.mark.parametrize("exc", [BrokerUnavailable("gateway 502"), RuntimeError("anything else")])
def test_an_order_already_sent_is_saved_when_the_cycle_then_fails(pg, owner_id, monkeypatch, exc):
    fb = blind_rig(pg, owner_id, monkeypatch, "ch26", "dh26")
    _raise_after_sending(monkeypatch, exc)

    assert executor.sync_broker_account("ch26", fast=True) == "error"

    sent = [o for tag, o in fb.log if tag == "submit"]
    assert len(sent) == 1                                   # it did reach the broker
    rows, status, report = _state(pg, "ch26", "dh26")
    assert [(r[0], r[1], r[2]) for r in rows] == [("SPY", "buy", 5.0)]
    assert rows[0][3] == sent[0]["client_order_id"]         # the row names that order
    assert status == "ok"                                   # not an auth problem
    assert any(str(exc)[:20] in e for e in report["errors"])
    assert "executions" in report                           # the block is always present


def test_an_expired_login_marks_the_connection_and_still_saves_the_order(pg, owner_id, monkeypatch):
    fb = blind_rig(pg, owner_id, monkeypatch, "ch26b", "dh26b")
    _raise_after_sending(monkeypatch, BrokerAuthExpired("refresh token expired"))

    assert executor.sync_broker_account("ch26b", fast=True) == "error"

    rows, status, report = _state(pg, "ch26b", "dh26b")
    assert len([o for tag, o in fb.log if tag == "submit"]) == 1
    assert len(rows) == 1
    assert status == "reconnect_needed"
    with pg() as s:
        assert "refresh token expired" in s.get(persistence.BrokerConnection, "ch26b").balance_error
    assert any("refresh token expired" in e for e in report["errors"])


def test_a_cycle_that_fails_before_polling_says_so(pg, owner_id, monkeypatch):
    """A cycle that dies before the executions poll (here: the session refresh
    fails) must leave an `executions` block whose error is set, so the ledger
    reads the state as UNKNOWN rather than as "polled, nothing filled"."""
    fb = blind_rig(pg, owner_id, monkeypatch, "ch26c", "dh26c")

    def boom(creds):
        raise BrokerUnavailable("down before anything")
    monkeypatch.setattr(fb, "ensure_session", boom, raising=False)

    assert executor.sync_broker_account("ch26c", fast=False) == "audited"
    rows, _status, report = _state(pg, "ch26c", "dh26c")
    assert rows == [] and not [o for tag, o in fb.log if tag == "submit"]
    assert report["executions"]["error"] == "sweep aborted before the executions poll"
    assert any("down before anything" in e for e in report["errors"])


def test_a_failed_poll_is_recorded_and_does_not_stop_order_management(pg, owner_id, monkeypatch):
    """An unreachable executions endpoint means UNKNOWN, and unknown must not
    abort the cycle: the error is recorded on the block and the cycle goes on."""
    fb = blind_rig(pg, owner_id, monkeypatch, "ch26e", "dh26e")

    def boom(*a, **k):
        raise BrokerUnavailable("executions endpoint 503")
    monkeypatch.setattr(executor, "_poll_executions", boom)
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: False)

    assert executor.sync_broker_account("ch26e", fast=False) == "audited"
    _rows, status, report = _state(pg, "ch26e", "dh26e")
    assert report["executions"]["error"] == "executions endpoint 503"
    assert status == "ok"
    assert not any("executions endpoint 503" in e for e in report["errors"])   # recorded, not fatal


def test_a_payload_that_cannot_be_read_is_a_report_error_not_a_lost_tail(
        pg, owner_id, monkeypatch):
    """The desired state is built INSIDE the try, where the three compute_*
    calls were. Built at the top of the function instead, a malformed
    payload would take the whole sweep with it -- no report on the
    deployment, no `executions` block, and no row for anything already
    sent."""
    blind_rig(pg, owner_id, monkeypatch, "ch26f", "dh26f")
    monkeypatch.setattr(executor, "desired_from_payload",
                        lambda *a: (_ for _ in ()).throw(
                            TypeError("float() argument must be a string")))

    assert executor.sync_broker_account("ch26f", fast=True) == "error"

    rows, status, report = _state(pg, "ch26f", "dh26f")
    assert rows == []                       # nothing was sent, nothing to save
    assert status == "ok"
    assert any("float() argument" in e for e in report["errors"])
    assert report["executions"]["error"] is None    # the poll block is coherent


def test_the_fast_path_keeps_the_last_poll_verdict(pg, owner_id, monkeypatch):
    """The fast path never polls. If the last full cycle recorded a poll ERROR,
    a fast cycle must carry that error forward, not replace it with a clean
    block -- the error is what keeps a missing fill from reading as no fill."""
    blind_rig(pg, owner_id, monkeypatch, "ch26d", "dh26d")
    with pg() as s:
        d = s.get(persistence.Deployment, "dh26d")
        d.position = {**(d.position or {}),
                      "execution": {"executions": {"new": 0, "skipped": 0,
                                                   "error": "poll failed: 503"}}}
        s.commit()
    executor._GATHER_CACHE.pop("ch26d", None)

    assert executor.sync_broker_account("ch26d", fast=True) == "submitted"
    _rows, _status, report = _state(pg, "ch26d", "dh26d")
    assert report["executions"]["error"] == "poll failed: 503"
