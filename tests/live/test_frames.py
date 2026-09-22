"""The reconcile-frame recorder (frames.py).

Two obligations, and every test here is one of them. The frame must be a
faithful, replayable record of what reconcile() was handed -- ordered
`desired` included, credentials excluded. And the recorder must be unable to
touch the sweep: not its return value, not its rows, not its latency, however
badly the observer or the writer behaves.
"""
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from dqengine.live import executor, frames, persistence


@pytest.fixture(autouse=True)
def recorder(monkeypatch):
    """The recorder ON for this file only (the suite conftest turns it off),
    with per-test counters. The teardown flush is the guard that no writer
    thread outlives the test holding the patched session factory."""
    monkeypatch.setenv("DQENGINE_RECORD_RECONCILE", "1")
    frames._QUIET_SEEN.clear()
    for k in frames._STATS:
        frames._STATS[k] = 0
    frames.set_frame_observer(None)
    # retention is time-gated; its own test drives it deliberately
    monkeypatch.setattr(frames, "_LAST_RETENTION", time.time())
    yield
    assert frames.flush(10.0), "the frames writer never drained"
    frames.set_frame_observer(None)


def _rig(pg, owner_id, monkeypatch, conn, dep, universe=("SPY",),
         holdings=(("SPY", 5.0),), at_broker=None, position_extra=None):
    """One deployment on one connection, the book agreeing with the venue.
    `at_broker` decides whether the sweep has anything to do."""
    from rig import IdlessBroker, seed
    from dqengine.live import book as _book
    from dqengine.live.book import book_for
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id=conn, dep_id=dep)
    pos = {"holdings": [{"symbol": s, "qty": q, "last_price": 100.0}
                        for s, q in holdings]}
    pos.update(position_extra or {})
    with pg() as s:
        d = s.get(persistence.Deployment, dep)
        d.ir = None
        d.universe = list(universe)
        d.position = pos
        s.commit()
    at_broker = dict(at_broker or {})
    fb = IdlessBroker(positions=at_broker)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    book_for(conn).apply_audit(dict(at_broker), [])
    executor._GATHER_CACHE.pop(conn, None)
    return fb


def _frames(pg, conn=None):
    with pg() as s:
        q = s.query(persistence.ReconcileFrame)
        if conn:
            q = q.filter(persistence.ReconcileFrame.connection_id == conn)
        return q.all()


def _quiet_frame(conn="c", **over):
    f = {"v": 1, "conn_id": conn, "mode": "fast", "now": "t",
         "out": {"entries": [], "report": {"actions": [], "errors": []}}}
    f.update(over)
    return f


# ---------------------------------------------------------- the kill switch

def test_the_kill_switch_writes_no_row(pg, owner_id, monkeypatch):
    _rig(pg, owner_id, monkeypatch, "cf0", "df0")
    monkeypatch.setenv("DQENGINE_RECORD_RECONCILE", "0")
    assert executor.sync_broker_account("cf0", fast=True) == "submitted"
    assert frames.flush(5.0)
    assert _frames(pg) == []
    assert frames.stats()["emitted"] == 0


# ------------------------------------------------------------- what is kept

def test_a_sweep_that_did_or_refused_something_is_always_kept():
    acted = _quiet_frame(out={"entries": [{"action": "submit"}],
                              "report": {"errors": []}})
    refused = _quiet_frame(out={"entries": [],
                                "report": {"errors": ["rate limited"]}})
    for f in (acted, refused):
        assert frames._kept(f) == "active"
    # ...and an active frame never consumes the quiet counter
    assert frames._QUIET_SEEN == {}


def test_quiet_sweeps_keep_the_first_after_boot_then_every_fiftieth():
    kept = [frames._kept(_quiet_frame("cq")) for _ in range(120)]
    assert [i for i, k in enumerate(kept) if k == "sampled"] == [0, 50, 100]
    assert set(kept) == {"sampled", None}


def test_the_quiet_counter_is_per_connection():
    assert frames._kept(_quiet_frame("ca")) == "sampled"
    assert frames._kept(_quiet_frame("cb")) == "sampled"
    assert frames._kept(_quiet_frame("ca")) is None


# --------------------------------------------------------------- the frame

def test_the_frame_round_trips_through_postgres_with_desired_order_intact(
        pg, owner_id, monkeypatch):
    """`desired`'s ORDER decides which deltas the max_orders_per_sync budget
    cuts and the launch order, so it is stored as a list of pairs: JSONB
    sorts an object's keys and would quietly re-order it."""
    _rig(pg, owner_id, monkeypatch, "cf1", "df1", universe=("ZZZ", "AAA"),
         holdings=(("MMM", 3.0),), at_broker={"MMM": 3.0})
    assert executor.sync_broker_account("cf1", fast=True) == "submitted"
    assert frames.flush(5.0)

    rows = _frames(pg, "cf1")
    assert len(rows) == 1 and rows[0].kept == "sampled"
    assert rows[0].mode == "fast"
    f = rows[0].frame
    assert [k for k, _ in f["desired"]] == ["ZZZ", "AAA", "MMM"]
    assert f["desired"] == [["ZZZ", 0.0], ["AAA", 0.0], ["MMM", 3.0]]
    assert f["v"] == 1 and f["last_px"] == {"MMM": 100.0}
    assert f["truth_mode"] == "off"
    assert f["rails"]["max_orders_per_sync"] == 20
    assert f["dep_states"][0][0] == "df1"
    assert f["dep_states"][0][2] == ["ZZZ", "AAA"]
    assert f["out"]["entries"] == [] and f["out"]["report"]["errors"] == []
    assert "submit_backoff_in" in f and "journal_gates" in f


def test_the_previous_report_is_not_nested_into_the_next_frame(pg, owner_id, monkeypatch):
    """position["execution"] is the LAST frame's report. Left in, every
    sweep's frame would carry the one before it and grow without bound."""
    _rig(pg, owner_id, monkeypatch, "cf2", "df2", at_broker={"SPY": 5.0},
         position_extra={"execution": {"actions": ["from the last sweep"]}})
    executor.sync_broker_account("cf2", fast=True)
    assert frames.flush(5.0)
    f = _frames(pg, "cf2")[0].frame
    assert "execution" not in f["dep_states"][0][1]
    assert "from the last sweep" not in repr(f)


def _keys(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from _keys(x)
    elif isinstance(v, list):
        for x in v:
            yield from _keys(x)


def test_no_credential_or_owner_key_survives_into_a_frame(pg, owner_id, monkeypatch):
    """A frame is a debugging artefact anyone with database access reads.
    The payload here carries exactly the keys that must never reach one."""
    _rig(pg, owner_id, monkeypatch, "cf3", "df3", at_broker={"SPY": 5.0},
         position_extra={"creds": {"app_key": "k-123"},
                         "user_id": "u-1",
                         "nested": [{"creds_encrypted": "gAAAA..."}]})
    executor.sync_broker_account("cf3", fast=True)
    assert frames.flush(5.0)
    f = _frames(pg, "cf3")[0].frame
    seen = set(_keys(f))
    assert not seen & {"creds", "creds_encrypted", "user_id", "balance",
                       "access_token", "refresh_token"}
    assert "k-123" not in repr(f) and "gAAAA..." not in repr(f)
    assert "nested" in seen                      # the scrub is per key, not all


def test_the_venue_book_is_recorded_on_a_full_pass_and_null_on_a_fast_one(
        pg, owner_id, monkeypatch):
    _rig(pg, owner_id, monkeypatch, "cf4", "df4", at_broker={"SPY": 5.0})
    executor.sync_broker_account("cf4", fast=True)
    assert frames.flush(5.0)
    frames._QUIET_SEEN.clear()          # keep the next quiet one as well
    executor._SYNC_GATE.pop("cf4", None)
    executor._GATHER_CACHE.pop("cf4", None)
    executor.sync_broker_account("cf4", fast=False)
    assert frames.flush(5.0)

    by_mode = {r.mode: r.frame for r in _frames(pg, "cf4")}
    assert set(by_mode) == {"fast", "shadow"}    # the fast path owns it
    assert by_mode["fast"]["venue"] is None
    assert by_mode["shadow"]["venue"]["positions"] == {"SPY": 5.0}
    assert by_mode["shadow"]["venue"]["open_orders"] == []


# -------------------------------------------------- it cannot touch a sweep

def test_the_observer_sees_every_frame_even_the_ones_no_row_keeps(
        pg, monkeypatch):
    seen = []
    frames.set_frame_observer(seen.append)
    frames._QUIET_SEEN["cobs"] = 1               # the next quiet one is dropped
    frames.emit(_quiet_frame("cobs"))
    assert frames.flush(5.0)
    assert len(seen) == 1
    assert _frames(pg, "cobs") == []             # seen, not stored


def test_an_observer_that_raises_changes_nothing_the_sweep_did(
        pg, owner_id, monkeypatch):
    def boom(frame):
        raise RuntimeError("observer is broken")
    frames.set_frame_observer(boom)
    fb = _rig(pg, owner_id, monkeypatch, "cf5", "df5")
    assert executor.sync_broker_account("cf5", fast=True) == "submitted"
    assert frames.flush(5.0)
    assert len([1 for tag, _ in fb.log if tag == "submit"]) == 1
    with pg() as s:
        assert s.query(persistence.BrokerOrder).count() == 1
    assert frames.stats()["observer_errors"] == 1
    assert len(_frames(pg, "cf5")) == 1          # the row is still written


def test_a_writer_that_raises_changes_nothing_the_sweep_did(pg, owner_id, monkeypatch):
    monkeypatch.setattr(frames, "build",
                        lambda f: (_ for _ in ()).throw(ValueError("nope")))
    fb = _rig(pg, owner_id, monkeypatch, "cf6", "df6")
    assert executor.sync_broker_account("cf6", fast=True) == "submitted"
    assert frames.flush(5.0)
    assert len([1 for tag, _ in fb.log if tag == "submit"]) == 1
    with pg() as s:
        assert s.query(persistence.BrokerOrder).count() == 1
    assert frames.stats()["write_errors"] == 1
    assert _frames(pg, "cf6") == []


def test_an_observer_that_blocks_cannot_delay_the_sweep_or_its_rows(
        pg, owner_id, monkeypatch):
    """The observer runs on the writer thread, off both locks and after the
    last commit. Held for five seconds it must cost the sweep nothing."""
    released = threading.Event()
    frames.set_frame_observer(lambda f: released.wait(5.0))
    fb = _rig(pg, owner_id, monkeypatch, "cf7", "df7")

    t0 = time.monotonic()
    assert executor.sync_broker_account("cf7", fast=True) == "submitted"
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"the sweep waited {elapsed:.2f}s on the recorder"
    assert len([1 for tag, _ in fb.log if tag == "submit"]) == 1
    with pg() as s:                              # rows are final already
        assert s.query(persistence.BrokerOrder).count() == 1
    released.set()


def test_a_full_queue_drops_and_counts(monkeypatch):
    small = queue.Queue(maxsize=2)
    monkeypatch.setattr(frames, "_QUEUE", small)
    monkeypatch.setattr(frames, "_start_writer", lambda: None)
    for _ in range(3):
        frames.emit(_quiet_frame("cfull"))
    assert frames.stats()["emitted"] == 3
    assert frames.stats()["dropped"] == 1
    while not small.empty():                     # leave the queue drained
        small.get()
        small.task_done()


def test_a_pacing_skip_and_a_lock_refusal_record_no_frame(pg, owner_id, monkeypatch):
    """Both return before anything was sent, so there is nothing to record.
    A frame for them would make 'no frame' stop meaning 'nothing happened'."""
    _rig(pg, owner_id, monkeypatch, "cf8", "df8", at_broker={"SPY": 5.0})
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: False)
    executor._SYNC_GATE["cf8"] = {"cooldown_until": time.time() + 30}
    assert executor.sync_broker_account("cf8", fast=False) == "skipped"

    executor._SYNC_GATE.pop("cf8", None)

    @contextmanager
    def held_elsewhere(conn_id):
        yield False
    monkeypatch.setattr(executor, "conn_sweep_lock", held_elsewhere)
    assert executor.sync_broker_account("cf8", fast=False) == "skipped"

    assert frames.flush(5.0)
    assert _frames(pg, "cf8") == []


# ---------------------------------------------------------------- retention

def test_retention_drops_frames_older_than_thirty_days(pg, owner_id, monkeypatch):
    from rig import seed
    seed(pg, owner_id, conn_id="cf9", dep_id="df9")
    assert frames.RETENTION_DAYS < 40
    old = datetime.now(timezone.utc) - timedelta(days=40)
    with pg() as s:
        s.add(persistence.ReconcileFrame(connection_id="cf9", created_at=old,
                                   mode="fast", kept="sampled", frame={}))
        s.add(persistence.ReconcileFrame(connection_id="cf9",
                                   created_at=datetime.now(timezone.utc),
                                   mode="fast", kept="sampled", frame={}))
        s.commit()
    monkeypatch.setattr(frames, "_LAST_RETENTION", 0.0)
    frames._retention()
    rows = _frames(pg, "cf9")
    assert len(rows) == 1 and rows[0].created_at > old


def test_retention_runs_at_most_hourly(monkeypatch):
    stamp = time.time() - 10
    monkeypatch.setattr(frames, "_LAST_RETENTION", stamp)
    frames._retention()
    assert frames._LAST_RETENTION == stamp       # it did not even start
