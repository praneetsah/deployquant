"""Fills of orders placed on an earlier day (executions.recover_earlier_fills).

A venue whose executions() lists only today's orders never reports the fill
of a GTC take-profit placed days earlier. Under `enforce` the day it filled
then settles as a confirmed no-fill and the next sweep buys the shares back
(2026-09-21). The executor asks the venue about such an order once it has
left the open-order list, and stores what it is told.
"""
from datetime import datetime, timedelta, timezone

import pytest

from dqengine.adapters.base import Caps, normalize_execution
from dqengine.live import executions, executor, persistence

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)   # 14:00 ET
FILLED_AT = datetime(2026, 9, 30, 13, 30, 1, tzinfo=timezone.utc)


class Venue:
    caps = Caps(fills_of_earlier_orders=False)

    def __init__(self, status="FILLED", rows=None):
        self.status = status
        self.rows = rows if rows is not None else [normalize_execution(
            broker_order_id="WB1", broker_exec_id="WB1:0",
            client_order_id="sl-tp-old", symbol="TQQQ", side="SELL",
            qty=40, price=61.27, filled_at=FILLED_AT, fees=0.15,
            order_level_avg=True)]
        self.asked = []

    def order_executions(self, creds, cid):
        self.asked.append(cid)
        return (self.status, self.rows)


@pytest.fixture(autouse=True)
def fresh_throttle():
    executions._LAST_RECOVER.clear()
    yield
    executions._LAST_RECOVER.clear()


def _rig(pg, owner_id, conn="cr", dep="dr", placed=NOW - timedelta(days=1),
         cid="sl-tp-old", status="SUBMITTED"):
    from rig import seed
    seed(pg, owner_id, conn_id=conn, dep_id=dep, truth="enforce")
    with pg() as s:
        s.add(persistence.BrokerOrder(
            connection_id=conn, deployment_id=dep, broker_order_id=None,
            client_order_id=cid, symbol="TQQQ", qty=40, side="sell",
            order_type="limit", limit_price=60.50, status=status,
            action="submit", created_at=placed, rule_tag="tp-1"))
        s.commit()


def _execs(pg, conn="cr"):
    with pg() as s:
        return (s.query(persistence.Execution)
                .filter(persistence.Execution.connection_id == conn).all())


def _order(pg, cid="sl-tp-old"):
    with pg() as s:
        return (s.query(persistence.BrokerOrder)
                .filter(persistence.BrokerOrder.client_order_id == cid)
                .one())


def test_a_vanished_earlier_order_s_fill_is_stored_against_its_deployment(
        pg, owner_id):
    _rig(pg, owner_id)
    v = Venue()
    got = executions.recover_earlier_fills(v, {}, "cr", [], now=NOW)
    assert v.asked == ["sl-tp-old"]
    assert got == {"cid": "sl-tp-old", "status": "FILLED", "new": 1}
    (e,) = _execs(pg)
    assert (e.deployment_id, e.rule_tag) == ("dr", "tp-1")
    assert (float(e.signed_qty), float(e.price)) == (-40.0, 61.27)
    assert e.filled_at == FILLED_AT
    o = _order(pg)
    assert o.status == "FILLED" and o.broker_order_id == "WB1"
    # final: never asked again
    executions._LAST_RECOVER.clear()
    assert executions.recover_earlier_fills(v, {}, "cr", [], now=NOW) is None
    assert v.asked == ["sl-tp-old"]


def test_an_order_still_open_at_the_venue_is_not_asked_about(pg, owner_id):
    _rig(pg, owner_id)
    v = Venue()
    still = [{"id": "", "client_order_id": "sl-tp-old"}]
    assert executions.recover_earlier_fills(v, {}, "cr", still,
                                            now=NOW) is None
    assert v.asked == []


def test_an_order_placed_today_is_left_to_the_today_poll(pg, owner_id):
    _rig(pg, owner_id, placed=NOW - timedelta(hours=2))
    v = Venue()
    assert executions.recover_earlier_fills(v, {}, "cr", [], now=NOW) is None
    assert v.asked == []


def test_an_order_older_than_the_window_is_history(pg, owner_id):
    _rig(pg, owner_id, placed=NOW - timedelta(days=20))
    v = Venue()
    assert executions.recover_earlier_fills(v, {}, "cr", [], now=NOW) is None


def test_a_fill_already_on_the_ledger_is_not_counted_twice(pg, owner_id):
    """A take-profit fill had once been entered by hand under its client
    order id and a made-up exec id. The venue's row has a different exec id,
    so the dedupe is by order, not by exec id."""
    _rig(pg, owner_id)
    with pg() as s:
        s.add(persistence.Execution(
            connection_id="cr", deployment_id="dr", broker_order_id=None,
            broker_exec_id="manual:20260930:tp-fill",
            client_order_id="sl-tp-old", symbol="TQQQ", signed_qty=-40,
            price=61.27, fees=0.15, filled_at=FILLED_AT, rule_tag="tp-1",
            source="manual", order_level_avg=True))
        s.commit()
    got = executions.recover_earlier_fills(Venue(), {}, "cr", [], now=NOW)
    assert got["new"] == 0
    assert len(_execs(pg)) == 1
    assert _order(pg).status == "FILLED"


def test_a_cancelled_order_is_settled_with_no_rows(pg, owner_id):
    _rig(pg, owner_id)
    got = executions.recover_earlier_fills(Venue("CANCELLED", rows=[]), {},
                                           "cr", [], now=NOW)
    assert got == {"cid": "sl-tp-old", "status": "CANCELLED", "new": 0}
    assert _execs(pg) == [] and _order(pg).status == "CANCELLED"


def test_nothing_is_asked_without_an_open_order_fetch_or_on_a_full_venue(
        pg, owner_id):
    _rig(pg, owner_id)
    v = Venue()
    assert executions.recover_earlier_fills(v, {}, "cr", None,
                                            now=NOW) is None
    v.caps = Caps()                       # reports earlier orders itself
    assert executions.recover_earlier_fills(v, {}, "cr", [], now=NOW) is None
    assert v.asked == []


def test_one_lookup_per_connection_per_minute(pg, owner_id):
    """Webull rate-limited a probe after three quick lookups."""
    _rig(pg, owner_id, status="SUBMITTED")
    v = Venue("SUBMITTED", rows=[])       # not final: stays a candidate
    executions.recover_earlier_fills(v, {}, "cr", [], now=NOW)
    executions.recover_earlier_fills(v, {}, "cr", [], now=NOW)
    assert v.asked == ["sl-tp-old"]


def test_a_failed_lookup_is_an_error_line_and_nothing_else():
    class Down(Venue):
        def order_executions(self, creds, cid):
            raise RuntimeError("Webull rate-limited us: to many requests")
    report = {"actions": [], "errors": []}

    def boom(*a, **k):
        raise RuntimeError("Webull rate-limited us: to many requests")
    orig = executions.recover_earlier_fills
    executions.recover_earlier_fills = boom
    try:
        executor._recover_into(report, Down(), {}, "cx", [])
    finally:
        executions.recover_earlier_fills = orig
    assert report["actions"] == []
    assert "rate-limited" in report["errors"][0]


def test_an_audit_sweep_recovers_the_fill(pg, owner_id, monkeypatch):
    """End to end: the executor's audit pass passes its own open-order fetch
    in, and the venue's fill lands on the ledger."""
    from rig import FakeBroker
    from dqengine.live import book as _book
    _rig(pg, owner_id, conn="ca", dep="da",
         placed=datetime.now(timezone.utc) - timedelta(days=2))

    class WB(FakeBroker):
        caps = Caps(fills_of_earlier_orders=False)
        asked = []

        def order_executions(self, creds, cid):
            self.asked.append(cid)
            return ("FILLED", Venue().rows)
    fb = WB(positions={"SPY": 5.0})
    fb.caps = Caps(fills_of_earlier_orders=False)  # FakeBroker sets its own
    monkeypatch.setattr(_book, "FAST_PATH", False)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    executor._GATHER_CACHE.pop("ca", None)
    executor._SYNC_GATE.pop("ca", None)
    executor.sync_broker_account("ca", fast=False)
    with pg() as s:
        rep = s.get(persistence.Deployment, "da").position.get("execution")
    assert fb.asked == ["sl-tp-old"], rep
    (e,) = _execs(pg, "ca")
    assert e.deployment_id == "da" and float(e.price) == 61.27


# ------------------------------------------------ the 2026-10-05 double buy
#
# A take-profit placed on an earlier day filled (sell 79). One audit sweep
# read 0 shares at the venue, wrote 0 into the book, froze it on the stale
# want, and THEN recovered the fill -- which subtracted the shares from the
# book a second time (-79). A fast pass that had passed the frozen check
# before the audit took the connection lock ran next and bought
# 79 - (-79) = 158.

def _tp_filled_rig(pg, owner_id, monkeypatch, conn, dep, filled_at=None):
    """The venue after the take-profit filled: 0 TQQQ, no open order. The
    sleeve has not heard: it still holds 40. The book is one audit behind."""
    from rig import FakeBroker, book_for
    from dqengine.live import book as _book
    _rig(pg, owner_id, conn=conn, dep=dep,
         placed=datetime.now(timezone.utc) - timedelta(days=2))
    with pg() as s:
        d = s.get(persistence.Deployment, dep)
        d.ir = {"universe": {"static": ["TQQQ"]}}
        d.position = {"holdings": [{"symbol": "TQQQ", "qty": 40,
                                    "last_price": 61.0}]}
        s.commit()

    rows = [normalize_execution(
        broker_order_id="WB1", broker_exec_id="WB1:0",
        client_order_id="sl-tp-old", symbol="TQQQ", side="SELL", qty=40,
        price=61.27, filled_at=filled_at or datetime.now(timezone.utc),
        fees=0.15, order_level_avg=True)]

    class WB(FakeBroker):
        def order_executions(self, creds, cid):
            return ("FILLED", rows)
    fb = WB(positions={})
    fb.caps = Caps(fills_of_earlier_orders=False)
    monkeypatch.setattr(_book, "FAST_PATH", True)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: False)
    executor._GATHER_CACHE.pop(conn, None)
    executor._SYNC_GATE.pop(conn, None)
    book = book_for(conn)
    book.apply_audit({"TQQQ": 40.0}, [
        {"id": "sl-tp-old", "client_order_id": "sl-tp-old", "symbol": "TQQQ",
         "qty": 40.0, "side": "sell", "type": "limit", "limit_price": 60.5}])
    return fb, book


def test_a_recovered_fill_is_not_taken_off_the_book_twice(
        pg, owner_id, monkeypatch):
    fb, book = _tp_filled_rig(pg, owner_id, monkeypatch, "cw", "dw")
    executor.sync_broker_account("cw", fast=False)
    assert len(_execs(pg, "cw")) == 1                 # the fill was recovered
    assert book.positions_view().get("TQQQ", 0.0) == 0.0
    assert book.open_orders_view() == []


def test_a_fast_pass_that_waited_behind_the_audit_sends_nothing(
        pg, owner_id, monkeypatch):
    """The fast pass checks the book BEFORE it waits for the connection
    lock. The audit that held the lock froze the book and recovered the
    fill; the fast pass must look again once it is inside."""
    from rig import submitted
    fb, book = _tp_filled_rig(pg, owner_id, monkeypatch, "cv", "dv")
    real_lock = executor._conn_lock("cv")

    class AuditGoesFirst:
        """The lock, as the fast pass met it: held by an audit sweep."""
        def __enter__(self):
            if not getattr(self, "ran", False):
                self.ran = True
                executor.sync_broker_account("cv", fast=False)
            return real_lock.__enter__()

        def __exit__(self, *a):
            return real_lock.__exit__(*a)
    gate = AuditGoesFirst()
    orig = executor._conn_lock
    monkeypatch.setattr(
        executor, "_conn_lock",
        lambda c: gate if (c == "cv" and not getattr(gate, "ran", False))
        else orig(c))
    res = executor.sync_broker_account("cv", fast=True)
    assert submitted(fb) == [], submitted(fb)
    assert res == "fallback"


def test_the_audit_after_a_recovery_waits_for_the_sleeve(
        pg, owner_id, monkeypatch):
    """The book is frozen after a recovery, so the next audit transmits
    for itself. The sleeve still wants its 40 shares until it re-runs with
    the fill; the ledger explains that difference, and nothing is bought."""
    from rig import submitted
    fb, book = _tp_filled_rig(pg, owner_id, monkeypatch, "cu", "du")
    executor.sync_broker_account("cu", fast=False)
    assert not book.fast_path_ok()[0]
    executor._SYNC_GATE.pop("cu", None)
    executor.sync_broker_account("cu", fast=False)
    assert submitted(fb) == [], submitted(fb)
    with pg() as s:
        rep = s.get(persistence.Deployment, "du").position["execution"]
    assert any("TQQQ market-delta frozen" in a for a in rep["actions"]), rep


def test_a_fill_recovered_on_a_later_day_holds_its_symbol_until_the_sleeve_ticks(
        pg, owner_id, monkeypatch):
    """The take-profit filled yesterday and is only recovered today. The
    rail that reads today's fills cannot see it; the sleeve still wants its
    40 shares. Nothing is bought until the deployment has ticked cleanly
    after the row was stored."""
    from rig import submitted
    fb, book = _tp_filled_rig(pg, owner_id, monkeypatch, "ct", "dt",
                              filled_at=datetime.now(timezone.utc)
                              - timedelta(days=1, hours=2))
    executor.sync_broker_account("ct", fast=False)       # recovers
    assert len(_execs(pg, "ct")) == 1
    for _ in range(2):
        executor._SYNC_GATE.pop("ct", None)
        executor.sync_broker_account("ct", fast=False)
    assert submitted(fb) == [], submitted(fb)
    with pg() as s:
        d = s.get(persistence.Deployment, "dt")
        assert any("TQQQ market-delta frozen" in a
                   for a in d.position["execution"]["actions"])
        # a tick that FAILED moves last_tick and folds nothing: the last
        # clean payload is still the one from before the row was stored
        pos = dict(d.position)
        pos["clean_tick_at"] = (datetime.now(timezone.utc)
                                - timedelta(minutes=5)).isoformat()
        d.position = pos
        d.last_tick = datetime.now(timezone.utc) + timedelta(seconds=60)
        d.tick_error = "boom"
        s.commit()
    executor._SYNC_GATE.pop("ct", None)
    executor._GATHER_CACHE.pop("ct", None)
    executor.sync_broker_account("ct", fast=False)
    assert submitted(fb) == []
    with pg() as s:
        d = s.get(persistence.Deployment, "dt")
        pos = dict(d.position)
        pos["clean_tick_at"] = (datetime.now(timezone.utc)
                                + timedelta(seconds=60)).isoformat()
        d.position = pos
        d.tick_error = None
        s.commit()
    for _ in range(2):      # an audit that sees the want, then one that sends
        executor._SYNC_GATE.pop("ct", None)
        executor._GATHER_CACHE.pop("ct", None)
        executor.sync_broker_account("ct", fast=False)
    # released: the (test's unchanged) sleeve wants 40 and gets them
    assert [(o["side"], o["qty"]) for o in submitted(fb)] == [("buy", 40.0)]


def test_an_old_fill_does_not_hold_its_symbol_while_a_deployment_is_in_error(
        pg, owner_id):
    """Found by review: reading `tick_error` made every fill of the last
    three days look unfolded for as long as the error stood, which froze
    sells that reduce the position too."""
    from rig import seed
    seed(pg, owner_id, conn_id="ce", dep_id="de", truth="enforce")
    now = datetime.now(timezone.utc)
    with pg() as s:
        s.add(persistence.Execution(
            connection_id="ce", deployment_id="de", broker_order_id="b1",
            broker_exec_id="b1:0", client_order_id="sl-mkt-x", symbol="TQQQ",
            signed_qty=40, price=60.0, fees=0.0,
            filled_at=now - timedelta(days=2),
            created_at=now - timedelta(days=2)))
        d = s.get(persistence.Deployment, "de")
        d.position = {**d.position, "clean_tick_at":
                      (now - timedelta(hours=3)).isoformat()}
        d.last_tick, d.tick_error = now, "boom"
        s.commit()
        today0 = now - timedelta(hours=1)
        assert executor._late_fills_read(s, "ce", today0) == set()
        # stored AFTER the last clean payload: held
        s.add(persistence.Execution(
            connection_id="ce", deployment_id="de", broker_order_id="b2",
            broker_exec_id="b2:0", client_order_id="sl-tp-y", symbol="SPY",
            signed_qty=-5, price=400.0, fees=0.0,
            filled_at=now - timedelta(days=1), created_at=now))
        s.commit()
        assert executor._late_fills_read(s, "ce", today0) == {"SPY"}


def test_a_clean_tick_stamps_when_its_payload_was_computed(pg, owner_id):
    from rig import seed
    from dqengine.live import deployment_store
    seed(pg, owner_id, conn_id="cs", dep_id="ds")
    with pg() as s:
        store = deployment_store._DeploymentTx(s, "ds")
        d = store.dep
        store.commit_payload({"stats": {}, "equity": [], "fills": [],
                              "journal": [], "position": {"qty": 0}})
        stamp = d.position["clean_tick_at"]
        assert datetime.fromisoformat(stamp) == d.last_tick
        store.commit_error("boom")
        assert d.position["clean_tick_at"] == stamp and d.last_tick > \
            datetime.fromisoformat(stamp)
