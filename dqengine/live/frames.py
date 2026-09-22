"""The reconcile-frame recorder (Phase 3 spec §5a, plan §4).

`broker_orders`, `order_journal` and `executions` record what the executor
DID. Nothing records what it was told: the positions and open orders it
fetched, the gates, `fold_pending`, `qb_cooldown`, `desired`. Output without
input is not a golden pair, so a refactor of the order path has nothing to
replay against. One row per sweep closes that.

Nothing here runs on the sweep thread except `emit`, which is one
`put_nowait` on a bounded queue. A single daemon thread calls the observer,
decides whether this frame is worth a row, builds the JSON and inserts it in
its own short-lived session. The sweep has already committed and released
both locks by then; a slow or broken recorder can delay nothing and change
nothing that was sent.

`DQENGINE_RECORD_RECONCILE=0` turns the whole thing off at `emit`.
"""
import dataclasses
import math
import os
import queue
import threading
import time
from datetime import date, datetime

# Bounded on purpose: if the writer falls behind (a wedged database, a slow
# observer) the sweep drops frames rather than growing a queue that is
# holding references to every payload it ever recorded.
_QUEUE: queue.Queue = queue.Queue(maxsize=200)
_WRITER = None
_WRITER_GUARD = threading.Lock()
_OBSERVER = None

# quiet frames kept: the first one after boot, then every 50th, per connection
SAMPLE_EVERY = 50
_QUIET_SEEN: dict = {}

RETENTION_DAYS = 30
RETENTION_EVERY_S = 3600
RETENTION_LIMIT = 5000
_LAST_RETENTION = 0.0

_STATS = {"emitted": 0, "dropped": 0, "written": 0, "observer_errors": 0,
          "write_errors": 0}

# A frame is a debugging artefact that anyone with database access can read.
# These keys never belong in one, at any depth, whatever a payload carries.
_NEVER = frozenset({"creds", "creds_encrypted", "user_id", "balance",
                    "password_hash", "access_token", "refresh_token"})


def enabled() -> bool:
    return os.environ.get("DQENGINE_RECORD_RECONCILE", "1") != "0"


def set_frame_observer(fn) -> None:
    """Install a callable that sees EVERY frame (sampling only decides which
    ones get a row). It runs on the writer thread, after the sweep has
    committed; raising only costs its own frame. `None` uninstalls."""
    global _OBSERVER
    _OBSERVER = fn


def stats() -> dict:
    return dict(_STATS)


# ------------------------------------------------------------------ emit

def emit(frame: dict) -> None:
    """Called on the sweep thread as its last statement. Never blocks."""
    if not enabled():
        return
    _start_writer()
    _STATS["emitted"] += 1
    try:
        _QUEUE.put_nowait(frame)
    except queue.Full:
        _STATS["dropped"] += 1


def flush(timeout: float = 10.0) -> bool:
    """Tests only: wait until every queued frame has been handled. Returns
    False if the writer is still busy when the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _QUEUE.all_tasks_done:
            if _QUEUE.unfinished_tasks == 0:
                return True
        time.sleep(0.005)
    return False


def _start_writer() -> None:
    global _WRITER
    if _WRITER is not None and _WRITER.is_alive():
        return
    with _WRITER_GUARD:
        if _WRITER is not None and _WRITER.is_alive():
            return
        _WRITER = threading.Thread(target=_writer_loop, name="frames",
                                   daemon=True)
        _WRITER.start()


def _writer_loop() -> None:
    while True:
        frame = _QUEUE.get()
        try:
            _handle(frame)
        except Exception as e:                      # never kill the thread
            print(f"[frames] writer failed: {e!r}", flush=True)
        finally:
            _QUEUE.task_done()


# --------------------------------------------------------------- handling

def _handle(frame: dict) -> None:
    obs = _OBSERVER
    if obs is not None:
        try:
            obs(frame)
        except Exception as e:
            _STATS["observer_errors"] += 1
            print(f"[frames] observer failed: {e!r}", flush=True)
    kept = _kept(frame)
    if kept is None:
        return
    try:
        _insert(frame, kept)
    except Exception as e:
        _STATS["write_errors"] += 1
        print(f"[frames] insert failed: {e!r}", flush=True)


def _kept(frame: dict):
    """'active' when the sweep did or refused something, 'sampled' for the
    quiet ones we keep anyway, None to drop. The counter is per connection
    and in-process, so it is deterministic in a test and keeps the first
    quiet frame after a boot (the one that shows a fresh process's state)."""
    out = frame.get("out") or {}
    report = out.get("report") or {}
    adapter = frame.get("adapter")
    if (out.get("entries") or report.get("errors")
            or getattr(adapter, "recorded", None)):
        return "active"
    conn_id = frame.get("conn_id")
    n = _QUIET_SEEN.get(conn_id, 0)
    _QUIET_SEEN[conn_id] = n + 1
    return "sampled" if n % SAMPLE_EVERY == 0 else None


def _insert(frame: dict, kept: str) -> None:
    from sqlalchemy import text
    from dqengine.live import persistence
    built = build(frame)
    with persistence.SessionLocal() as s:
        # the recorder is bookkeeping: it waits two seconds for a lock and
        # then gives up, rather than sitting behind a migration
        s.execute(text("SET LOCAL lock_timeout='2s'"))
        s.add(persistence.ReconcileFrame(
            connection_id=frame.get("conn_id"), mode=built["mode"],
            kept=kept, frame=built))
        s.commit()
    _STATS["written"] += 1
    _retention()


def _retention() -> None:
    global _LAST_RETENTION
    now = time.time()
    if now - _LAST_RETENTION < RETENTION_EVERY_S:
        return
    _LAST_RETENTION = now
    from sqlalchemy import text
    from dqengine.live import persistence
    try:
        with persistence.SessionLocal() as s:
            s.execute(text("SET LOCAL lock_timeout='2s'"))
            s.execute(text(                        # both bounds are ints above
                f"DELETE FROM reconcile_frames WHERE id IN ("
                f"SELECT id FROM reconcile_frames WHERE created_at < "
                f"now() - interval '{RETENTION_DAYS} days' "
                f"LIMIT {RETENTION_LIMIT})"))
            s.commit()
    except Exception as e:
        print(f"[frames] retention failed: {e!r}", flush=True)


# ------------------------------------------------------------- the frame

FRAME_VERSION = 1


def build(frame: dict) -> dict:
    """The stored shape (v1). `desired` is a LIST OF PAIRS, not an object:
    reconcile() iterates it and its ORDER decides which deltas the
    max_orders_per_sync budget cuts, and JSONB sorts an object's keys."""
    adapter = frame.get("adapter")
    out = frame.get("out") or {}
    mode = frame.get("mode") or "audit"
    # a full pass that was not allowed to transmit (the fast path owns the
    # connection) read the venue for real and sent nothing: that is a third
    # kind of pass, and a replay must not mistake it for one that traded
    if mode == "audit" and getattr(adapter, "transmit", True) is False:
        mode = "shadow"
    built = {
        "v": FRAME_VERSION,
        "mode": mode,
        "now": frame.get("now"),
        "caps_id": getattr(adapter, "id", None),
        "env": {"SUBMIT_POOL": os.environ.get("SUBMIT_POOL"),
                "SUBMIT_STAGGER_MS": os.environ.get("SUBMIT_STAGGER_MS")},
        # the previous report is nested inside each position payload and
        # would recurse in size every sweep
        "dep_states": [[d, _strip_execution(pos), _json(uni)]
                       for d, pos, uni in (frame.get("dep_states") or [])],
        "desired": [[k, v] for k, v in (frame.get("desired") or {}).items()],
        "venue": None if mode == "fast" else {
            "positions": _json(getattr(adapter, "fetched_positions", None)),
            "open_orders": _json(getattr(adapter, "fetched_open_orders",
                                         None))},
        "out": {"entries": _json(out.get("entries")),
                "report": _json(out.get("report")),
                "recorded": _json(getattr(adapter, "recorded", None))},
    }
    for key in ("exit_wants", "entry_wants", "close_wants", "last_px",
                "held_px", "rails", "truth_mode", "moc_done",
                "recent_refusals", "qb_cooldown", "fold_pending",
                "journal_gates", "submit_backoff_in", "buying_power",
                "known_symbols"):
        if key in frame:
            built[key] = _json(frame[key])
    return built


def _strip_execution(pos):
    if isinstance(pos, dict) and "execution" in pos:
        pos = {k: v for k, v in pos.items() if k != "execution"}
    return _json(pos)


def _json(v):
    """JSON-safe, sets sorted so two frames of the same state compare equal,
    and _NEVER keys dropped wherever they appear."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else repr(v)   # JSONB has no NaN/Inf
    if isinstance(v, dict):
        return {str(k): _json(x) for k, x in v.items() if k not in _NEVER}
    if isinstance(v, (set, frozenset)):
        return sorted((_json(x) for x in v), key=repr)
    if isinstance(v, (list, tuple)):
        return [_json(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _json(dataclasses.asdict(v))
    return repr(v)
