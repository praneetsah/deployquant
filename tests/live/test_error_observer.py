"""The sweep's error observer (executor.set_error_observer).

A sweep that ends with errors in its report tells the observer once, after it
has committed. A quiet sweep tells it nothing, and an observer that raises
cannot change what the sweep did or returned.
"""
import pytest

from dqengine.live import executor, persistence


@pytest.fixture(autouse=True)
def no_observer():
    executor.set_error_observer(None)
    yield
    executor.set_error_observer(None)


def _rig(pg, owner_id, monkeypatch, conn, dep, fail=None):
    """One deployment that holds 5 SPY on a connection whose account holds
    none, so the sweep submits a buy. `fail` makes the venue refuse it."""
    from rig import IdlessBroker, seed
    from dqengine.live import book as _book
    from dqengine.live.book import book_for
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id=conn, dep_id=dep)
    with pg() as s:
        d = s.get(persistence.Deployment, dep)
        d.ir = None
        d.universe = ["SPY"]
        d.live_confirmed = True
        s.commit()
    fb = IdlessBroker(positions={}, fail_submit_msg=fail)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    book_for(conn).apply_audit({}, [])
    executor._GATHER_CACHE.pop(conn, None)
    return fb


def test_a_rejected_order_reaches_the_observer(pg, owner_id, monkeypatch):
    seen = []
    executor.set_error_observer(lambda *a: seen.append(a))
    _rig(pg, owner_id, monkeypatch, "ce1", "de1",
         fail="Webull refused the request: SHORT_NOT_ALLOWED")
    executor.sync_broker_account("ce1", fast=True)
    assert len(seen) == 1
    conn_id, broker, errors, fast = seen[0]
    assert (conn_id, broker, fast) == ("ce1", "fake", True)
    assert any("SHORT_NOT_ALLOWED" in e for e in errors)


def test_a_sweep_with_no_errors_tells_the_observer_nothing(pg, owner_id,
                                                           monkeypatch):
    seen = []
    executor.set_error_observer(lambda *a: seen.append(a))
    fb = _rig(pg, owner_id, monkeypatch, "ce2", "de2")
    assert executor.sync_broker_account("ce2", fast=True) == "submitted"
    assert [e for a, e in fb.log if a == "submit"]
    assert seen == []


def test_an_observer_that_raises_does_not_change_the_sweep(pg, owner_id,
                                                           monkeypatch):
    def boom(*a):
        raise RuntimeError("mail server down")
    _rig(pg, owner_id, monkeypatch, "ce3", "de3", fail="REJECTED")
    quiet = executor.sync_broker_account("ce3", fast=True)
    executor._GATHER_CACHE.pop("ce3", None)
    executor.set_error_observer(boom)
    _rig_again = executor.sync_broker_account("ce3", fast=True)
    assert _rig_again == quiet
