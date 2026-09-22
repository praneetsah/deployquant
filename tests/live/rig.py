"""Shared fixtures for the executor's tests: a fake venue and the rows a
sweep needs in the database before it can run.

Two suites import this module. The engine's own suite runs it against a
Postgres holding the engine's tables only, where `user_id` is a plain
nullable tag. The platform's suite runs the same helpers against its own
database, where that column is a NOT NULL foreign key to `users`. The
difference is the `owner_id` argument every seeder takes: each suite's
conftest supplies it from an `owner_id` fixture -- `None` on this side, a
real user row on the platform's.

Nothing here reads a live broker or a live database: `FakeBroker` keeps its
positions and orders in a dict, and every seeder writes through the session
factory the test's `pg` fixture installed.
"""
from datetime import date

from sqlalchemy import create_engine, text

from dqengine.adapters.base import BrokerAdapter, BrokerRejected, Caps
from dqengine.live import book as _book
from dqengine.live import executor, persistence, vault
from dqengine.live.book import book_for


# ------------------------------------------------------------- the venue

class FakeBroker(BrokerAdapter):
    """In-memory broker: positions dict + open orders list."""
    id = "fake"
    caps = Caps()

    def __init__(self, positions=None, orders=None, supports_replace=True,
                 fail_submit_msg=None, fail_replace_msg=None):
        self.pos = dict(positions or {})
        self.orders = list(orders or [])
        self.caps = Caps(supports_replace=supports_replace)
        self.log = []
        self._seq = 0
        # I7 tests: when set, submit()/replace() raise BrokerRejected(msg)
        # instead of succeeding, logging the attempt first so tests can
        # count how many broker calls were actually made.
        self.fail_submit_msg = fail_submit_msg
        self.fail_replace_msg = fail_replace_msg

    def positions(self, creds):
        return dict(self.pos)

    def open_orders(self, creds):
        return list(self.orders)

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        self._seq += 1
        if self.fail_submit_msg is not None:
            self.log.append(("submit_attempt", symbol))
            raise BrokerRejected(self.fail_submit_msg)
        o = {"id": f"o{self._seq}", "symbol": symbol, "qty": float(qty),
             "side": side, "type": order_type, "limit_price": limit_price,
             "stop_price": stop_price, "trail_percent": trail_percent,
             "status": "new", "client_order_id": client_order_id or ""}
        self.log.append(("submit", o))
        self.orders.append(o)
        return o

    def replace(self, creds, order_id, qty=None, limit_price=None):
        if self.fail_replace_msg is not None:
            self.log.append(("replace_attempt", order_id))
            raise BrokerRejected(self.fail_replace_msg)
        self.log.append(("replace", order_id, qty, limit_price))
        for o in self.orders:
            if o["id"] == order_id:
                if qty is not None:
                    o["qty"] = float(qty)
                if limit_price is not None:
                    o["limit_price"] = limit_price
                return dict(o)
        raise AssertionError("replace of unknown order")

    def cancel(self, creds, order_id):
        self.log.append(("cancel", order_id))
        self.orders = [o for o in self.orders if o["id"] != order_id]


class IdlessBroker(FakeBroker):
    """Webull-shaped venue: the place response carries NO broker-native id,
    and a market order fills instantly -- it never shows up in
    open_orders() and the positions() read lags the fill (self.pos is
    deliberately not updated). This is the shape that duplicated orders:
    a market order acknowledged with no id, so the book dropped the ack
    and the next pass recomputed and re-sent the same order."""

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        self._seq += 1
        if self.fail_submit_msg is not None:
            self.log.append(("submit_attempt", symbol))
            raise BrokerRejected(self.fail_submit_msg)
        o = {"id": "", "symbol": symbol, "qty": float(qty), "side": side,
             "type": order_type, "limit_price": limit_price,
             "status": "submitted", "client_order_id": client_order_id or ""}
        self.log.append(("submit", o))
        return o


def report():
    """A fresh sweep report, the shape `reconcile` appends into."""
    return {"synced_at": "t", "actions": [], "errors": []}


# ------------------------------------------------------------- the rows

def seed_conns(pg, owner_id, *conn_ids):
    """One usable broker connection per id. connection_id is a real foreign
    key wherever these tests run, so the rows have to exist before a sweep
    can write anything against them."""
    with pg() as s:
        for cid in conn_ids:
            s.add(persistence.BrokerConnection(
                id=cid, user_id=owner_id, broker="fake", status="ok",
                mode="paper",
                creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.commit()
    return owner_id


def seed(pg, owner_id, conn_id="cb", dep_id="db1", qty_held=0, truth="off"):
    """One connection and one running deployment that wants 5 SPY."""
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id=conn_id, user_id=owner_id, broker="fake", status="ok",
            mode="paper", execution_truth=truth,
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id=dep_id, user_id=owner_id, name="D",
            start_date=date(2026, 8, 1),
            ir={"universe": {"static": ["SPY"]}}, status="running",
            broker_connection_id=conn_id, cash_initial=1000.0,
            position={"holdings": [{"symbol": "SPY", "qty": 5,
                                    "last_price": 400.0}]}))
        s.commit()


def blind_rig(pg, owner_id, monkeypatch, conn, dep, truth="enforce"):
    """IdlessBroker + empty book: the in-memory rails see nothing.
    truth defaults to `enforce`: the journal's net/freeze gates are scoped
    to poll-backed connections (rows close on executions evidence)."""
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id=conn, dep_id=dep, truth=truth)
    fb = IdlessBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    book_for(conn).apply_audit({}, [])
    executor._GATHER_CACHE.pop(conn, None)
    return fb


class OtherProcess:
    """A second Postgres session holding a connection's sweep lock. Its own
    engine on purpose: a pooled connection of the sweep's own engine would
    satisfy the advisory lock re-entrantly and prove nothing."""

    def __init__(self, bind, conn_id):
        self.key = executor.conn_sweep_key(conn_id)
        self.engine = create_engine(bind.url)
        self.conn = self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT")

    def take(self) -> bool:
        return bool(self.conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar())

    def release(self) -> None:
        self.conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                          {"k": self.key})

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.engine.dispose()


def submitted(fb):
    """Every order the fake venue was actually asked to place."""
    return [o for tag, o in fb.log if tag == "submit"]
