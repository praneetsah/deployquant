"""What a running install looks like from outside it: status, orders, fills.

Three read-only readers over the tables the executor already writes. They
open a session, read, and return plain dictionaries; nothing here changes a
row, talks to a broker or touches the bus beyond one key.

`problems()` is the list `dqengine status` exits non-zero on. Each entry is
a fact a table holds, not an inference: a tick error, a journal row that was
never resolved, a connection the broker rejected, a sweep that recorded an
error, a feed that has gone quiet during the session, a sweep lock another
process is still holding. A healthcheck can read the exit code and a person
can read the lines.

The book (`dqengine.live.book`) is deliberately absent. It is in-process
state in whichever process transmits, it is never persisted, and reporting
an empty one from here would say "no orders in flight" about a process this
one cannot see.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# how long a held sweep lock has to survive to count as stuck: two samples,
# same holder. One sweep is well under a second, so a single sample catching
# the lock says nothing.
LOCK_SAMPLE_S = 1.0


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _feed_state(bus) -> dict:
    """The feed's own state off the bus, plus the age of the quote board.
    `{}` when there is no bus to ask or nothing has been published."""
    from dqengine.live.driver.loop import FEED_STATE_KEY
    from dqengine.live.feed_runner import QUOTE_SNAPSHOT_KEY
    if bus is None:
        return {}
    out: dict = {}
    try:
        raw = bus.get(FEED_STATE_KEY)
        if raw:
            out = dict(json.loads(raw))
    except Exception as e:                                   # noqa: BLE001
        return {"error": f"could not read the feed state: {e!r}"}
    try:
        out["quotes_published"] = bus.get(QUOTE_SNAPSHOT_KEY) is not None
    except Exception:                                        # noqa: BLE001
        out["quotes_published"] = None
    return out


def _lock_holder(conn_id: str) -> int | None:
    """The pid of a process holding this connection's sweep advisory lock,
    or None. Reads `pg_locks`, which is the only place that answer lives."""
    from sqlalchemy import text

    from dqengine.live import persistence
    from dqengine.live.executor import conn_sweep_key
    key = conn_sweep_key(conn_id)
    hi, lo = (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF
    sql = text("SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
               "AND objsubid = 1 AND classid = :hi AND objid = :lo "
               "AND granted")
    try:
        with persistence.SessionLocal() as s:
            row = s.execute(sql, {"hi": hi, "lo": lo}).first()
            return int(row[0]) if row else None
    except Exception:                                        # noqa: BLE001
        return None


def _stuck_lock(conn_id: str, sleep=time.sleep) -> int | None:
    first = _lock_holder(conn_id)
    if first is None:
        return None
    sleep(LOCK_SAMPLE_S)
    return first if _lock_holder(conn_id) == first else None


def report(bus=None, check_lock: bool = True) -> dict:
    """Every fact `dqengine status` prints, in one dictionary."""
    from dqengine.live import persistence
    from dqengine.live.executor import _journal_health
    out: dict = {"at": datetime.now(timezone.utc).isoformat(),
                 "connections": [], "deployments": [],
                 "journal": _journal_health(), "feed": _feed_state(bus)}
    with persistence.SessionLocal() as s:
        conns = s.query(persistence.BrokerConnection).all()
        for c in conns:
            settings = dict(c.settings or {})
            out["connections"].append({
                "id": c.id, "broker": c.broker, "mode": c.mode,
                "status": c.status, "execution_truth": c.execution_truth,
                "paused": bool(settings.get("paused")),
                "dry_run": bool(settings.get("dry_run")),
                "max_order_notional": settings.get("max_order_notional"),
                "max_position_notional": settings.get("max_position_notional"),
                "sweep_lock_pid": (_stuck_lock(c.id) if check_lock else None)})
        for d in s.query(persistence.Deployment).all():
            pos = dict(d.position or {})
            exe = dict(pos.get("execution") or {})
            holdings = [{"symbol": h.get("symbol"), "qty": h.get("qty")}
                        for h in (pos.get("holdings") or [])]
            out["deployments"].append({
                "id": d.id, "name": d.name, "status": d.status,
                "mode": d.mode, "kind": d.kind,
                "resolution": d.resolution, "universe": list(d.universe or []),
                "connection_id": d.broker_connection_id,
                "start_date": _iso(d.start_date),
                "last_tick": _iso(d.last_tick), "tick_error": d.tick_error,
                "holdings": holdings,
                "last_sweep_at": exe.get("synced_at"),
                "sweep_errors": list(exe.get("errors") or []),
                "sweep_actions": list(exe.get("actions") or []),
                "executions_poll": exe.get("executions")})
    return out


def problems(rep: dict, now_et: datetime | None = None) -> list:
    """The facts in `rep` that mean something is wrong. Empty means the exit
    code is 0."""
    from dqengine.feeds import FeedState, check_feed, market_is_open
    now_et = now_et or datetime.now(ET)
    out = []
    for d in rep["deployments"]:
        if d["tick_error"]:
            first = str(d["tick_error"]).strip().splitlines()[-1][:200]
            if "did not reproduce itself" in str(d["tick_error"]):
                out.append(f"{d['name']}: frozen by the determinism check — "
                           f"{first}")
            else:
                out.append(f"{d['name']}: tick error — {first}")
        for e in d["sweep_errors"]:
            out.append(f"{d['name']}: last sweep reported {e}")
        poll = d["executions_poll"] or {}
        if poll.get("error"):
            out.append(f"{d['name']}: executions poll — {poll['error']}")
    for c in rep["connections"]:
        if c["status"] in ("error", "reconnect_needed"):
            out.append(f"connection {c['broker']}: {c['status']}")
        if c["paused"]:
            out.append(f"connection {c['broker']}: paused, nothing will be sent")
        if c["sweep_lock_pid"] is not None:
            out.append(f"connection {c['broker']}: sweep lock still held by "
                       f"pid {c['sweep_lock_pid']}")
    j = rep["journal"]
    if j.get("unresolved"):
        out.append(f"order journal: {j['unresolved']} row(s) sent and never "
                   f"resolved")
    if j.get("abandoned_today"):
        out.append(f"order journal: {j['abandoned_today']} row(s) abandoned "
                   f"today")
    if market_is_open(now_et):
        feed = rep["feed"]
        syms = sorted({s for d in rep["deployments"]
                       if d["status"] == "running" for s in d["universe"]})
        if not feed:
            out.append("no feed state has been published: nothing is "
                       "streaming bars")
        elif feed.get("error") and not feed.get("connected"):
            out.append(f"feed: {feed['error']}")
        else:
            health = check_feed(FeedState.from_status(feed), syms, now_et)
            if health.silent:
                out.append(f"feed: {health.reason}")
    return out


def orders(limit: int = 20) -> list:
    """The most recent journal rows: what was sent, and where each one
    ended. The journal is the write-ahead record, so a row exists before the
    wire call and leaves the outstanding set only on evidence."""
    from dqengine.live import persistence
    with persistence.SessionLocal() as s:
        rows = (s.query(persistence.OrderJournal)
                .order_by(persistence.OrderJournal.created_at.desc())
                .limit(limit).all())
        return [{"at": _iso(r.created_at), "symbol": r.symbol, "side": r.side,
                 "qty": r.qty, "filled_qty": r.filled_qty, "kind": r.kind,
                 "limit_price": r.limit_price, "stop_price": r.stop_price,
                 "state": r.state, "rule_tag": r.rule_tag, "note": r.note,
                 "client_order_id": r.client_order_id,
                 "broker_order_id": r.broker_order_id,
                 "deployment_id": r.deployment_id} for r in rows]


def fills(limit: int = 20) -> list:
    """The most recent executions: what the broker actually did. One row per
    execution, so a partial fill is two rows rather than an average."""
    from dqengine.live import persistence
    with persistence.SessionLocal() as s:
        rows = (s.query(persistence.Execution)
                .order_by(persistence.Execution.filled_at.desc())
                .limit(limit).all())
        return [{"at": _iso(r.filled_at), "symbol": r.symbol,
                 "qty": r.signed_qty, "price": r.price, "fees": r.fees,
                 "source": r.source, "rule_tag": r.rule_tag,
                 "order_level_avg": bool(r.order_level_avg),
                 "broker_order_id": r.broker_order_id,
                 "deployment_id": r.deployment_id} for r in rows]
