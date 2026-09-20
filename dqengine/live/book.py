"""The live book: per-connection broker-state cache that earns the fast
path (direct-submit spec, 2026-08-27).

One instance per connection, held in the API process (the sole broker
writer). Updated incrementally by everything the OMS does and sees;
replaced wholesale by every auditor pass; NEVER persisted -- broker state
is re-fetchable, so a restart yields an empty book, the fast path refuses
(fail-closed), and the first audit rebuilds it. The worst case of this
design is the pre-existing sweep, by construction.

Thread-safety: book mutations happen on the intent-consumer thread and
the sync/auditor path; a per-book lock keeps them coherent (operations
are tiny -- dict updates)."""
from __future__ import annotations

import threading
import time

# fast path is revoked when the last clean audit is older than this
RECONCILE_FRESH_S = 90
# our own submits within this window explain broker-side lag (an order
# gone from open_orders before the position reflects it)
RECENT_S = 5.0
# an inflight (submitted, never acked) entry older than this freezes the
# book -- we no longer know what the broker holds of ours
INFLIGHT_MAX_S = 20.0


# test seam, not a kill switch: the suite's conftest flips it off so the
# legacy sweep-as-transmitter tests keep their semantics
FAST_PATH = True


class Book:
    def __init__(self, conn_id: str):
        self.conn_id = conn_id
        self.lock = threading.RLock()
        self.positions: dict = {}          # SYM -> float qty (broker truth)
        self.open_orders: dict = {}        # broker_order_id -> order dict
        self.inflight: dict = {}           # cid -> {sym, side, qty, at}
        self.recent: list = []             # [{sym, side, qty, at}]
        self.seq: dict = {}                # dep_id -> last applied intent seq
        self.reconciled_at: float = 0.0    # unix ts of last clean audit
        self.frozen: str | None = "never audited"
        self.creds: dict | None = None     # decrypted creds cache
        self.creds_at: float = 0.0

    # ------------------------------------------------------------ status

    def fast_path_ok(self) -> tuple[bool, str]:
        if not FAST_PATH:
            return False, "fast path off (test seam)"
        with self.lock:
            if self.frozen:
                return False, f"frozen: {self.frozen}"
            age = time.time() - self.reconciled_at
            if age > RECONCILE_FRESH_S:
                return False, f"book stale ({age:.0f}s since audit)"
            for cid, e in self.inflight.items():
                if time.time() - e["at"] > INFLIGHT_MAX_S:
                    self.frozen = f"inflight {cid} unacked >" \
                                  f"{INFLIGHT_MAX_S:.0f}s"
                    return False, self.frozen
        return True, "ok"

    def freeze(self, reason: str) -> None:
        with self.lock:
            self.frozen = reason[:300]
        print(f"[book] FROZEN {self.conn_id}: {reason}", flush=True)

    # ------------------------------------------------------- book updates

    def note_submit(self, cid: str, sym: str, side: str, qty: float) -> None:
        with self.lock:
            now = time.time()
            self.inflight[cid] = {"sym": sym.upper(), "side": side,
                                  "qty": float(qty), "at": now}
            self.recent.append({"sym": sym.upper(), "side": side,
                                "qty": float(qty), "at": now})
            self._gc(now)

    def note_ack(self, cid: str, order: dict | None) -> None:
        """Submission returned: move inflight -> open_orders. Keyed by the
        broker-native id when the venue returns one, else by our own
        client_order_id -- Webull's place response carries NO id, and
        dropping the ack on that made the book blind to its own submit:
        the next pass recomputed the same delta and duplicated the order
        (the 2026-08-31 live double-buy). An instant reject/cancel never
        rested and is dropped; a `filled` ack is KEPT as pending until
        affirmative evidence (a polled fill via note_fills, or a
        post-settle audit) clears it -- positions lag fills at every
        broker, and the pending entry is what bridges that gap."""
        with self.lock:
            self.inflight.pop(cid, None)
            if not order:
                return
            key = order.get("id") or order.get("client_order_id") or cid
            status = (order.get("status") or "").lower()
            if not key or status in ("canceled", "cancelled", "rejected",
                                     "expired"):
                return
            self.open_orders[key] = dict(order)

    def note_reject(self, cid: str) -> None:
        with self.lock:
            self.inflight.pop(cid, None)

    def note_cancel(self, broker_order_id: str) -> None:
        with self.lock:
            self.open_orders.pop(broker_order_id, None)

    def note_fills(self, rows) -> None:
        """Executions-poll ingest: apply fills between audits. `rows`
        iterable of (broker_order_id, client_order_id, symbol, signed_qty).
        The client id fallback is what clears an idless ack's entry (see
        note_ack) -- the fill IS the affirmative evidence."""
        with self.lock:
            for boid, cid, sym, qty in rows:
                sym = (sym or "").upper()
                self.positions[sym] = self.positions.get(sym, 0.0) \
                    + float(qty)
                key = boid if boid and boid in self.open_orders else None
                if key is None and cid:
                    for k, o in self.open_orders.items():
                        if k == cid or o.get("client_order_id") == cid:
                            key = k
                            break
                if key is not None:
                    o = self.open_orders[key]
                    o["qty"] = float(o.get("qty") or 0) - abs(float(qty))
                    if o["qty"] <= 1e-9:
                        del self.open_orders[key]

    def apply_audit(self, positions: dict, open_orders: list) -> None:
        """A clean auditor pass: the broker wins wholesale -- EXCEPT for
        symbols mid-settle (our own submit within RECENT_S, or still
        inflight). There OUR writes are fresher than this fetch: a
        just-filled order is already absent from open_orders before the
        position read reflects it, and overwriting the book with that
        stale pair re-opens the duplicate-order window note_ack's entry
        closes. For settle symbols the book keeps its own coherent
        (position, pending) pair; a fetched order carrying the same
        client id supersedes its book entry (the fetch has the
        broker-native id)."""
        with self.lock:
            now = time.time()
            self._gc(now)
            settle = {r["sym"] for r in self.recent}
            settle |= {e["sym"] for e in self.inflight.values()}
            new_pos = {(s or "").upper(): float(q)
                       for s, q in positions.items()}
            new_oo = {o["id"]: dict(o) for o in open_orders
                      if o.get("id")}
            for sym in settle:
                if sym in self.positions:
                    new_pos[sym] = self.positions[sym]
                else:
                    new_pos.pop(sym, None)
            fetched_cids = {(o.get("client_order_id") or "")
                            for o in new_oo.values()}
            fetched_cids.discard("")
            for k, o in self.open_orders.items():
                if (o.get("symbol") or "").upper() not in settle:
                    continue
                if k in new_oo \
                        or (o.get("client_order_id") or "") in fetched_cids:
                    continue
                new_oo[k] = dict(o)
            self.positions = new_pos
            self.open_orders = new_oo
            # an audit supersedes anything older than itself
            self.inflight = {c: e for c, e in self.inflight.items()
                             if now - e["at"] < 2.0}
            self.reconciled_at = now
            if self.frozen:
                print(f"[book] unfrozen {self.conn_id} (clean audit; was: "
                      f"{self.frozen})", flush=True)
            self.frozen = None

    def _gc(self, now: float) -> None:
        self.recent = [r for r in self.recent if now - r["at"] < RECENT_S]

    # ---------------------------------------------------------- read side

    def positions_view(self) -> dict:
        with self.lock:
            return dict(self.positions)

    def open_orders_view(self) -> list:
        with self.lock:
            return [dict(o) for o in self.open_orders.values()]

    def recent_symbols(self) -> set:
        """Symbols with our own activity inside the settle window -- the
        auditor treats drift there as pending settle, not book failure."""
        with self.lock:
            now = time.time()
            self._gc(now)
            out = {r["sym"] for r in self.recent}
            out |= {e["sym"] for e in self.inflight.values()}
            return out

    def intent_is_new(self, dep_id: str, seq: int) -> bool:
        with self.lock:
            if seq <= self.seq.get(dep_id, 0):
                return False
            self.seq[dep_id] = seq
            return True

    def health(self) -> dict:
        with self.lock:
            return {"reconciled_age_s": (round(time.time()
                                               - self.reconciled_at)
                                         if self.reconciled_at else None),
                    "frozen": self.frozen,
                    "inflight": len(self.inflight),
                    "open_orders": len(self.open_orders)}


_BOOKS: dict[str, Book] = {}
_BOOKS_LOCK = threading.Lock()


def book_for(conn_id: str) -> Book:
    with _BOOKS_LOCK:
        b = _BOOKS.get(conn_id)
        if b is None:
            b = _BOOKS[conn_id] = Book(conn_id)
        return b


def books_health() -> dict:
    with _BOOKS_LOCK:
        return {cid: b.health() for cid, b in _BOOKS.items()}
