"""Write-ahead order ledger (spec: docs/superpowers/specs/
2026-08-31-order-journal.md).

Sessions are the caller's; every function is a plain query/update so the
transmit path controls transaction boundaries. record_sending's savepoint
keeps a unique-violation from poisoning the caller's transaction -- the
violation IS the dedupe verdict, not an error: two passes computing the
same intent produce the same deterministic client id, and the second
INSERT stands down instead of double-sending (the 2026-08-31 double-buy
class, closed at the database)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from dqengine.live.persistence import OrderJournal

ACK_DEADLINE_S = 10          # a `sending` row older than this is suspect
ABANDON_S = 60               # ... and this old, provably unlisted: abandoned
_OPEN = ("sending", "submitted", "open")


def record_sending(session, conn_id: str, entry: dict):
    """Insert the write-ahead row for a reconcile submit `entry` (the same
    dict shape broker_order_row takes). Returns the row, or None when the
    (connection, client id) pair already exists -- the caller must stand
    down that one submit."""
    row = OrderJournal(
        connection_id=conn_id, deployment_id=entry.get("deployment_id"),
        client_order_id=entry["client_order_id"],
        symbol=(entry["symbol"] or "").upper(),
        side=entry["side"], qty=abs(float(entry["qty"])),
        kind=entry.get("order_type") or "market",
        limit_price=entry.get("limit_price"),
        stop_price=entry.get("stop_price"),
        rule_tag=entry.get("rule_tag"), state="sending")
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return None
    return row


def _row(session, conn_id, cid):
    if not cid:
        return None
    return (session.query(OrderJournal)
            .filter_by(connection_id=conn_id, client_order_id=cid)
            .first())


def mark_submitted(session, conn_id, cid, broker_order_id, status) -> None:
    row = _row(session, conn_id, cid)
    if row is None:
        return
    status = (status or "").lower()
    if broker_order_id:
        row.broker_order_id = broker_order_id
    if status == "filled":
        row.state = "filled"
    elif status in ("canceled", "cancelled", "expired"):
        row.state = "canceled"
    elif status == "rejected":
        row.state = "rejected"
    else:
        row.state = "submitted"


def mark_rejected(session, conn_id, cid, note) -> None:
    row = _row(session, conn_id, cid)
    if row is not None:
        row.state = "rejected"
        row.note = (note or "")[:300]


def fold_execution(session, conn_id, execution) -> None:
    """A broker execution landed: accumulate it on its journal row (cid
    first, broker order id fallback). filled_qty >= qty closes the row."""
    cid = getattr(execution, "client_order_id", None)
    boid = getattr(execution, "broker_order_id", None)
    row = _row(session, conn_id, cid)
    if row is None and boid:
        row = (session.query(OrderJournal)
               .filter_by(connection_id=conn_id, broker_order_id=boid)
               .first())
    if row is None:
        return
    row.filled_qty = (row.filled_qty or 0.0) + abs(
        float(execution.signed_qty))
    if row.filled_qty >= row.qty - 1e-9:
        row.state = "filled"


def mark_canceled(session, conn_id, cid=None, broker_order_id=None) -> None:
    """A cancel we sent succeeded: the row leaves the outstanding set. An
    open-but-canceled row would otherwise false-freeze its symbol under
    the visibility rule for the rest of the day."""
    row = _row(session, conn_id, cid) if cid else None
    if row is None and broker_order_id:
        row = (session.query(OrderJournal)
               .filter_by(connection_id=conn_id,
                          broker_order_id=broker_order_id).first())
    if row is not None and row.state in _OPEN:
        row.state = "canceled"


def mark_folded(session, conn_id, symbol, upto) -> None:
    """The sleeve folded this symbol's broker fills: lift the
    filled-unfolded freeze. `upto` reserved for partial folds."""
    (session.query(OrderJournal)
     .filter(OrderJournal.connection_id == conn_id,
             OrderJournal.symbol == (symbol or "").upper(),
             OrderJournal.state == "filled",
             OrderJournal.note.is_(None))
     .update({OrderJournal.note: "folded"}, synchronize_session=False))


def gates(session, conn_id: str, today_start_et) -> dict:
    """One query -> everything the delta pass needs, per symbol:
    open_qty (signed remaining of sending/submitted/open rows),
    open_rows ({SYM: [(cid, signed_remaining)]} so the caller can dedupe
    against orders it already sees at the venue), unfolded (ABSOLUTE filled
    qty the sleeve has not folded -- offsetting fills must still freeze),
    filled_today (signed filled total, folded included), filled_side
    ({SYM: {buy, sell}} totals -- the per-side fold-lift input), unresolved
    (symbols with an aged `sending` row), epoch (today's row count, the
    deterministic-cid input)."""
    rows = (session.query(OrderJournal)
            .filter(OrderJournal.connection_id == conn_id,
                    OrderJournal.created_at >= today_start_et).all())
    out = {"open_qty": {}, "open_rows": {}, "unfolded": {},
           "filled_today": {}, "filled_side": {}, "unresolved": set(),
           "epoch": {}}
    now = datetime.now(timezone.utc)
    for r in rows:
        sym = r.symbol.upper()
        out["epoch"][sym] = out["epoch"].get(sym, 0) + 1
        sign = 1.0 if r.side == "buy" else -1.0
        if r.state in _OPEN:
            rem = max(0.0, (r.qty or 0.0) - (r.filled_qty or 0.0))
            if rem:
                out["open_qty"][sym] = out["open_qty"].get(sym, 0.0) \
                    + sign * rem
                out["open_rows"].setdefault(sym, []).append(
                    (r.client_order_id, sign * rem))
        if r.state == "filled":
            fq = abs(r.filled_qty or 0.0)
            out["filled_today"][sym] = out["filled_today"].get(sym, 0.0) \
                + sign * fq
            # per-side totals (a sell and an equal buy sum to zero and
            # reads as "nothing happened" -- sides must be compared apart)
            fs = out["filled_side"].setdefault(sym, {"buy": 0.0,
                                                     "sell": 0.0})
            fs[r.side] = fs.get(r.side, 0.0) + fq
            if r.note != "folded":
                # ABSOLUTE unfolded qty: any unfolded fill freezes, whatever
                # its sign or whatever it nets against
                out["unfolded"][sym] = out["unfolded"].get(sym, 0.0) + fq
        if r.state == "sending" and r.created_at is not None:
            created = r.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() > ACK_DEADLINE_S:
                out["unresolved"].add(sym)
    return out


def resolve_stale(session, conn_id: str, open_orders, poll_ok) -> list:
    """Recovery for `sending` rows past the ack deadline (spec: state-based
    gates): listed at the venue -> `open` (+ broker id); a fill already
    advanced the row (fold_execution) -> untouched; provably absent (the
    executions poll succeeded, the open-orders fetch happened, ABANDON_S
    old) -> `abandoned`, loudly. `open_orders` None means the fetch never
    happened -- absence is not evidence, nothing abandons. Returns
    human-readable action lines for the sweep report."""
    now = datetime.now(timezone.utc)
    by_cid = {}
    for o in (open_orders or []):
        c = o.get("client_order_id")
        if c:
            by_cid[c] = o
    out = []
    rows = (session.query(OrderJournal)
            .filter(OrderJournal.connection_id == conn_id,
                    OrderJournal.state == "sending").all())
    for r in rows:
        created = r.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds()
        if age <= ACK_DEADLINE_S:
            continue
        o = by_cid.get(r.client_order_id)
        if o is not None:
            r.state = "open"
            if o.get("id"):
                r.broker_order_id = o["id"]
            out.append(f"{r.symbol} order {r.client_order_id} found at "
                       f"the venue — reopened")
        elif poll_ok and open_orders is not None and age > ABANDON_S:
            r.state = "abandoned"
            r.note = "never observed at venue"
            out.append(f"{r.symbol} order {r.client_order_id} ABANDONED "
                       f"— never observed at the venue")
    return out


def market_cid(conn_id, day_iso, sym, side, qty, epoch) -> str:
    h = hashlib.sha1(f"{conn_id}|{day_iso}|{sym}|{side}|{qty}|{epoch}"
                     .encode()).hexdigest()[:8]
    return f"sl-mkt-{sym}-{h}"
