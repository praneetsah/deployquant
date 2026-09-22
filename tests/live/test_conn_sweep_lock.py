"""One sweeping PROCESS per broker connection.

`_conn_lock` binds the threads of one interpreter. Nothing bound the
processes: a rolling deploy runs the old and the new api container together
for a minute, and a self-hoster can start the live command twice. The
journal's UNIQUE(conn, cid) collapses duplicate market deltas, but exits and
entries carry a random cid suffix, so two processes sweeping one account both
send them. `conn_sweep_lock` takes a Postgres session advisory lock on a
dedicated connection for the length of the sweep; a process that cannot get
it sends nothing and says so.

The other process is a real second Postgres session on its own engine -- a
pooled connection of the sweep's own engine would satisfy the lock
re-entrantly and prove nothing.
"""
import pytest
from sqlalchemy import create_engine

from dqengine.live import executor, persistence

from rig import FakeBroker, OtherProcess, blind_rig, seed, submitted


@pytest.fixture()
def other(pg):
    """Factory for second sessions, all closed when the test ends."""
    made = []

    def _make(conn_id):
        o = OtherProcess(pg.kw["bind"], conn_id)
        made.append(o)
        return o
    try:
        yield _make
    finally:
        for o in made:
            o.close()


def _audit_rig(pg, owner_id, monkeypatch, conn, dep):
    """A plain full sweep that transmits: no book, so the auditor owns the
    wire. Wants SPY 5, holds nothing -> one BUY 5."""
    seed(pg, owner_id, conn_id=conn, dep_id=dep)
    fb = FakeBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    return fb


# --------------------------------------------------------------- the key

def test_the_key_is_the_same_in_every_build():
    """Two builds overlapping in a rolling deploy must compute one key, so
    the derivation is pinned to a literal (and is not Python's salted
    hash())."""
    assert executor.conn_sweep_key("conn-A") == 2138761333228636656
    assert executor.conn_sweep_key("conn-B") != \
        executor.conn_sweep_key("conn-A")
    assert -2 ** 63 <= executor.conn_sweep_key("conn-A") < 2 ** 63


# ------------------------------------------------- another process sweeps

def test_a_sweep_sends_nothing_while_another_process_holds_the_lock(
        pg, owner_id, monkeypatch, capsys, other):
    fb = _audit_rig(pg, owner_id, monkeypatch, "cl1", "dl1")
    held = other("cl1")
    assert held.take()

    assert executor.sync_broker_account("cl1", fast=False) == "skipped"

    assert submitted(fb) == []
    assert "another process holds the sweep lock" in capsys.readouterr().out
    with pg() as s:
        assert s.query(persistence.BrokerOrder).count() == 0


def test_the_fast_lane_sends_nothing_while_another_process_holds_the_lock(
        pg, owner_id, monkeypatch, capsys, other):
    fb = blind_rig(pg, owner_id, monkeypatch, "cl2", "dl2")
    held = other("cl2")
    assert held.take()

    assert executor.sync_broker_account("cl2", fast=True) == "fallback"

    assert submitted(fb) == []
    assert "another process holds the sweep lock" in capsys.readouterr().out


def test_another_connection_is_not_affected(pg, owner_id, monkeypatch, other):
    """The key is per connection: a lock held on one account is silence for
    that account only. One shared fake broker -- the refused sweep sends
    nothing, so everything in the log came from the other connection."""
    seed(pg, owner_id, conn_id="cl4a", dep_id="dl4a")
    seed(pg, owner_id, conn_id="cl4b", dep_id="dl4b")
    fb = FakeBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    held = other("cl4a")
    assert held.take()

    assert executor.sync_broker_account("cl4a", fast=False) == "skipped"
    assert submitted(fb) == []
    assert executor.sync_broker_account("cl4b", fast=False) == "audited"
    assert [(o["symbol"], o["side"], o["qty"]) for o in submitted(fb)] == \
        [("SPY", "buy", 5.0)]


def test_the_sweep_runs_once_the_other_process_releases(
        pg, owner_id, monkeypatch, other):
    fb = _audit_rig(pg, owner_id, monkeypatch, "cl5", "dl5")
    held = other("cl5")
    assert held.take()
    assert executor.sync_broker_account("cl5", fast=False) == "skipped"
    assert submitted(fb) == []

    held.release()

    assert executor.sync_broker_account("cl5", fast=False) == "audited"
    assert [(o["symbol"], o["side"], o["qty"]) for o in submitted(fb)] == \
        [("SPY", "buy", 5.0)]


# ------------------------------------------------------- the lock is given up

def test_the_lock_is_released_after_a_normal_sweep(pg, owner_id, monkeypatch, other):
    fb = _audit_rig(pg, owner_id, monkeypatch, "cl6", "dl6")
    assert executor.sync_broker_account("cl6", fast=False) == "audited"
    assert len(submitted(fb)) == 1

    assert other("cl6").take(), "the sweep left the advisory lock held"


def test_the_lock_is_released_after_a_sweep_that_raises(
        pg, owner_id, monkeypatch, other):
    blind_rig(pg, owner_id, monkeypatch, "cl7", "dl7")

    def boom(*a, **k):
        raise RuntimeError("mid-sweep failure")
    monkeypatch.setattr(executor, "reconcile", boom)
    assert executor.sync_broker_account("cl7", fast=True) == "error"

    assert other("cl7").take(), "a failed sweep left the advisory lock held"


# ------------------------------------------------------------- edge cases

def test_the_quote_breach_exit_is_not_blocked_by_the_lock(
        pg, owner_id, monkeypatch, other):
    """Decision Q8: the emergency exit goes around the book, the journal and
    both locks, and moves as it is. A held sweep lock must not swallow a
    stop that a live quote has already breached."""
    seed(pg, owner_id, conn_id="cl8", dep_id="dl8-0123456789")
    fb = FakeBroker(positions={"SPY": 5.0})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    held = other("cl8")
    assert held.take()

    entry = executor.execute_quote_breach_exit(
        "cl8", "dl8-0123456789", "SPY", -5.0)

    assert entry is not None
    assert [(o["symbol"], o["side"], o["qty"]) for o in submitted(fb)] == \
        [("SPY", "sell", 5.0)]


def test_a_lock_that_cannot_be_taken_sends_nothing(
        pg, owner_id, monkeypatch, capsys, other):
    """Fail closed: with the database unreachable the lock cannot be proved,
    and a sweep that cannot prove it is the only writer transmits nothing."""
    fb = _audit_rig(pg, owner_id, monkeypatch, "cl9", "dl9")

    def down():
        raise OSError("could not connect to server")
    monkeypatch.setattr(executor, "_sweep_lock_bind", down)

    assert executor.sync_broker_account("cl9", fast=False) == "skipped"
    assert submitted(fb) == []
    out = capsys.readouterr().out
    assert "could not take the sweep lock" in out
    assert "another process holds the sweep lock" in out


def test_a_database_without_advisory_locks_is_a_no_op(monkeypatch):
    """Requirement of the self-hosted path: on a dialect with no advisory
    locks the helper reports acquired rather than refusing every sweep."""
    sqlite = create_engine("sqlite://")
    monkeypatch.setattr(executor, "_sweep_lock_bind", lambda: sqlite)
    try:
        with executor.conn_sweep_lock("cl10") as held:
            assert held is True
    finally:
        sqlite.dispose()
