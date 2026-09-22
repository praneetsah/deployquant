"""Order journal: write-ahead ledger rows, cid-uniqueness dedupe, lifecycle
transitions, and the per-symbol gates the delta pass consumes."""
import pytest
from sqlalchemy.exc import IntegrityError

from dqengine.live import persistence
from rig import blind_rig, seed_conns


def test_journal_row_and_cid_uniqueness(pg, owner_id):
    seed_conns(pg, owner_id, "c1", "c2")
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="c1", client_order_id="sl-mkt-SPY-abc",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="sending"))
        s.commit()
    with pg() as s:
        row = s.query(persistence.OrderJournal).one()
        assert row.state == "sending" and row.filled_qty == 0.0
        assert row.created_at is not None
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="c1", client_order_id="sl-mkt-SPY-abc",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="sending"))
        with pytest.raises(IntegrityError):
            s.commit()
    # same cid on ANOTHER connection is fine
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="c2", client_order_id="sl-mkt-SPY-abc",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="sending"))
        s.commit()


# ------------------------------------------------------------- ledger API

def test_record_sending_dedupes_by_cid(pg, owner_id):
    from dqengine.live import journal
    seed_conns(pg, owner_id, "c1")
    entry = {"symbol": "SPY", "qty": 5.0, "side": "buy",
             "order_type": "market", "limit_price": None,
             "client_order_id": "sl-mkt-SPY-e0", "deployment_id": None,
             "rule_tag": None}
    with pg() as s:
        assert journal.record_sending(s, "c1", entry) is not None
        s.commit()
    with pg() as s:
        assert journal.record_sending(s, "c1", entry) is None, \
            "same intent twice -> second insert refused, no exception"
        s.commit()  # session still usable after the savepoint rollback


def test_lifecycle_and_gates(pg, owner_id):
    from dqengine.live import journal
    from datetime import datetime, timedelta, timezone
    seed_conns(pg, owner_id, "c1")
    day0 = datetime.now(timezone.utc) - timedelta(hours=6)
    e = {"symbol": "SPY", "qty": 5.0, "side": "buy", "order_type": "market",
         "limit_price": None, "client_order_id": "sl-mkt-SPY-e1",
         "deployment_id": None, "rule_tag": None}
    with pg() as s:
        journal.record_sending(s, "c1", e)
        g = journal.gates(s, "c1", day0)
        assert g["open_qty"] == {"SPY": 5.0} and g["epoch"] == {"SPY": 1}
        assert g["open_rows"] == {"SPY": [("sl-mkt-SPY-e1", 5.0)]}
        journal.mark_submitted(s, "c1", "sl-mkt-SPY-e1", "bo9", "submitted")
        g = journal.gates(s, "c1", day0)
        assert g["open_qty"] == {"SPY": 5.0} and g["unresolved"] == set()

        class Ex:  # duck-typed Execution
            client_order_id = "sl-mkt-SPY-e1"
            broker_order_id = "bo9"
            symbol = "SPY"
            signed_qty = 5.0
        journal.fold_execution(s, "c1", Ex)
        g = journal.gates(s, "c1", day0)
        assert g["open_qty"] == {} and g["unfolded"] == {"SPY": 5.0}
        journal.mark_folded(s, "c1", "SPY", None)
        g = journal.gates(s, "c1", day0)
        assert g["unfolded"] == {}


def test_unresolved_sending_gate(pg, owner_id):
    from dqengine.live import journal
    from datetime import datetime, timedelta, timezone
    seed_conns(pg, owner_id, "c1")
    with pg() as s:
        row = journal.record_sending(
            s, "c1", {"symbol": "QQQ", "qty": 2.0, "side": "sell",
                      "order_type": "market", "limit_price": None,
                      "client_order_id": "sl-mkt-QQQ-e0",
                      "deployment_id": None, "rule_tag": None})
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        s.commit()
    with pg() as s:
        g = journal.gates(s, "c1",
                          datetime.now(timezone.utc) - timedelta(hours=6))
        assert g["unresolved"] == {"QQQ"}
        assert g["open_qty"] == {"QQQ": -2.0}, "sell rows net negative"


def test_market_cid_deterministic():
    from dqengine.live import journal
    a = journal.market_cid("c1", "2026-01-05", "TQQQ", "buy", 100.0, 0)
    b = journal.market_cid("c1", "2026-01-05", "TQQQ", "buy", 100.0, 0)
    c = journal.market_cid("c1", "2026-01-05", "TQQQ", "buy", 100.0, 1)
    assert a == b != c and a.startswith("sl-mkt-TQQQ-")


# ------------------------------------------- write-ahead at submit time

def test_act_writes_journal_row_before_wire(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    fb = blind_rig(pg, owner_id, monkeypatch, "cj1", "dj1")
    assert executor.sync_broker_account("cj1", fast=True) == "submitted"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert len(subs) == 1
    with pg() as s:
        rows = s.query(persistence.OrderJournal).all()
        assert len(rows) == 1
        assert rows[0].state == "submitted"
        assert rows[0].client_order_id == subs[0]["client_order_id"]
        assert rows[0].symbol == "SPY" and rows[0].qty == 5.0


def test_journal_unique_gate_blocks_true_race(pg, owner_id, monkeypatch):
    """The RACE case: both passes computed their cid before either row
    landed (here: pass 2 reads the 2s gather cache, so it sees the same
    epoch pass 1 saw). Same intent -> same cid -> the second insert hits
    the unique gate and the wire call is skipped."""
    from dqengine.live import executor
    from dqengine.live.book import book_for
    fb = blind_rig(pg, owner_id, monkeypatch, "cj7", "dj7")
    assert executor.sync_broker_account("cj7", fast=True) == "submitted"
    # blind the book, KEEP the gather cache: epoch still reads 0
    b = book_for("cj7")
    b.recent.clear()
    b.inflight.clear()
    b.apply_audit({}, [])
    assert executor.sync_broker_account("cj7", fast=True) == "submitted"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert len(subs) == 1, "same epoch -> same cid -> unique gate blocks"
    with pg() as s:
        assert s.query(persistence.OrderJournal).count() == 1


def test_journal_dedupes_across_blinded_passes(pg, owner_id, monkeypatch):
    """Two full passes against a blinded broker must reach the venue ONCE:
    the second pass sees the first's open journal row and nets it."""
    from dqengine.live import executor
    from dqengine.live.book import book_for
    fb = blind_rig(pg, owner_id, monkeypatch, "cj2", "dj2")
    assert executor.sync_broker_account("cj2", fast=True) == "submitted"
    # blind the in-memory layer COMPLETELY (restart / cross-process shape):
    # no settle window to preserve, then a stale audit wipes the book
    b = book_for("cj2")
    b.recent.clear()
    b.inflight.clear()
    b.apply_audit({}, [])
    executor._GATHER_CACHE.pop("cj2", None)
    assert executor.sync_broker_account("cj2", fast=True) == "submitted"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert len(subs) == 1, "journal unique gate must stop the second send"


def test_market_delta_cid_is_deterministic_per_intent(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    fb = blind_rig(pg, owner_id, monkeypatch, "cj6", "dj6")
    executor.sync_broker_account("cj6", fast=True)
    subs = [o for tag, o in fb.log if tag == "submit"]
    cid = subs[0]["client_order_id"]
    assert cid.startswith("sl-mkt-SPY-") and len(cid.split("-")[-1]) == 8


# ------------------------------------------------ gates in the delta pass

def test_open_journal_row_nets_the_delta(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    fb = blind_rig(pg, owner_id, monkeypatch, "cj3", "dj3")
    with pg() as s:   # want is 5 (from _seed); an open journal buy of 5
        s.add(persistence.OrderJournal(
            connection_id="cj3", client_order_id="sl-mkt-SPY-prior",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="open"))
        s.commit()
    assert executor.sync_broker_account("cj3", fast=True) == "submitted"
    assert [o for t, o in fb.log if t == "submit"] == []


def test_unfolded_fill_freezes_symbol_every_mode(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    fb = blind_rig(pg, owner_id, monkeypatch, "cj4", "dj4")   # truth_mode "off"
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="cj4", client_order_id="sl-mkt-SPY-done",
            symbol="SPY", side="buy", qty=5.0, filled_qty=5.0,
            kind="market", state="filled"))
        s.commit()
    executor.sync_broker_account("cj4", fast=True)
    assert [o for t, o in fb.log if t == "submit"] == []


def test_unresolved_sending_freezes_symbol(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    from datetime import datetime, timedelta, timezone
    fb = blind_rig(pg, owner_id, monkeypatch, "cj5", "dj5")
    with pg() as s:
        r = persistence.OrderJournal(
            connection_id="cj5", client_order_id="sl-mkt-SPY-lost",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="sending")
        s.add(r)
        s.flush()
        r.created_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        s.commit()
    executor.sync_broker_account("cj5", fast=True)
    assert [o for t, o in fb.log if t == "submit"] == []


# ------------------------------------- fills close rows; folds lift

def test_store_folds_execution_into_journal(pg, owner_id):
    from dqengine.live import executions as ex_mod
    from dqengine.live import journal
    from datetime import datetime, timezone
    seed_conns(pg, owner_id, "c1")
    with pg() as s:
        journal.record_sending(
            s, "c1", {"symbol": "SPY", "qty": 5.0, "side": "buy",
                      "order_type": "market", "limit_price": None,
                      "client_order_id": "sl-mkt-SPY-fx",
                      "deployment_id": None, "rule_tag": None})
        journal.mark_submitted(s, "c1", "sl-mkt-SPY-fx", "", "submitted")
        s.commit()
    row = {"broker_order_id": "bo77", "broker_exec_id": "ex77",
           "client_order_id": "sl-mkt-SPY-fx", "symbol": "SPY",
           "side": "buy", "qty": 5.0, "price": 100.0, "fees": 0.0,
           "filled_at": datetime.now(timezone.utc),
           "order_level_avg": False}
    with pg() as s:
        assert ex_mod.store(s, "c1", [row]) == 1
        s.commit()
    with pg() as s:
        j = s.query(persistence.OrderJournal).one()
        assert j.state == "filled" and j.filled_qty == 5.0
        assert j.broker_order_id == "" or j.broker_order_id is None \
            or j.broker_order_id == "bo77"


def test_gather_fold_lift_when_sleeve_matches_ledger(pg, owner_id, monkeypatch):
    """enforce: once the sleeves' confirmed folded qty equals the ledger's
    filled total for a symbol, the freeze lifts (row marked folded). On a
    mismatch (the duplicate-order shape) it must stand."""
    from dqengine.live import executor
    from datetime import datetime
    fb = blind_rig(pg, owner_id, monkeypatch, "cj8", "dj8", truth="enforce")
    fb.pos = {"SPY": 5.0}                      # broker caught up
    from dqengine.live.book import book_for
    book_for("cj8").apply_audit({"SPY": 5.0}, [])
    today = datetime.now(executor.ET).date().isoformat()
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="cj8", client_order_id="sl-mkt-SPY-ff",
            symbol="SPY", side="buy", qty=5.0, filled_qty=5.0,
            kind="market", state="filled"))
        d = s.get(persistence.Deployment, "dj8")
        d.fills = [{"day": today, "ms": 0, "qty": 5, "sym": "SPY",
                    "px": 100.0, "tag": "t", "fees": 0,
                    "confirmed": True}]
        s.commit()
    executor.sync_broker_account("cj8", fast=True)
    with pg() as s:
        assert s.query(persistence.OrderJournal).one().note == "folded"
    # mismatch case: a second filled row makes ledger 10 vs model 5
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="cj8", client_order_id="sl-mkt-SPY-f2",
            symbol="SPY", side="buy", qty=5.0, filled_qty=5.0,
            kind="market", state="filled"))
        s.commit()
    executor._GATHER_CACHE.pop("cj8", None)
    executor.sync_broker_account("cj8", fast=True)
    with pg() as s:
        r2 = (s.query(persistence.OrderJournal)
              .filter_by(client_order_id="sl-mkt-SPY-f2").one())
        assert r2.note is None, "mismatched totals: the freeze stands"


def test_gates_off_for_truth_off_connections(pg, owner_id, monkeypatch):
    """A truth `off` connection has no executions poll to close rows: the
    net/freeze gates must not apply (an open row would net forever), while
    the write-ahead insert itself still records."""
    from dqengine.live import executor
    fb = blind_rig(pg, owner_id, monkeypatch, "cj9", "dj9", truth="off")
    with pg() as s:   # an open journal row that would net the whole want
        s.add(persistence.OrderJournal(
            connection_id="cj9", client_order_id="sl-mkt-SPY-off",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="open"))
        s.commit()
    assert executor.sync_broker_account("cj9", fast=True) == "submitted"
    subs = [o for t, o in fb.log if t == "submit"]
    assert len(subs) == 1, "off-mode: gates inert, the delta still trades"
    with pg() as s:
        assert s.query(persistence.OrderJournal).count() == 2, \
            "the write-ahead insert still records in off mode"


# --------------------------------------------------- recovery (sending)

def test_resolve_stale_sending_rows(pg, owner_id):
    from dqengine.live import journal
    from datetime import datetime, timedelta, timezone
    seed_conns(pg, owner_id, "c1")
    old = datetime.now(timezone.utc) - timedelta(seconds=90)
    with pg() as s:
        for cid, sym in (("sl-mkt-AAA-x", "AAA"), ("sl-mkt-BBB-x", "BBB"),
                         ("sl-mkt-CCC-x", "CCC")):
            r = persistence.OrderJournal(
                connection_id="c1", client_order_id=cid, symbol=sym,
                side="buy", qty=1.0, kind="market", state="sending")
            s.add(r)
            s.flush()
            r.created_at = old
        # a young row must stay untouched
        s.add(persistence.OrderJournal(
            connection_id="c1", client_order_id="sl-mkt-DDD-x",
            symbol="DDD", side="buy", qty=1.0, kind="market",
            state="sending"))
        s.commit()
    with pg() as s:
        # BBB's fill already advanced it (fold path)
        class Ex:
            client_order_id = "sl-mkt-BBB-x"
            broker_order_id = "boB"
            symbol = "BBB"
            signed_qty = 1.0
        journal.fold_execution(s, "c1", Ex)
        acts = journal.resolve_stale(
            s, "c1",
            [{"id": "boA", "symbol": "AAA", "qty": 1.0, "side": "buy",
              "type": "market", "client_order_id": "sl-mkt-AAA-x"}],
            poll_ok=True)
        s.commit()
    assert any("reopened" in a for a in acts)
    assert any("ABANDONED" in a for a in acts)
    with pg() as s:
        by = {r.client_order_id: r for r in s.query(persistence.OrderJournal)}
        assert by["sl-mkt-AAA-x"].state == "open"
        assert by["sl-mkt-AAA-x"].broker_order_id == "boA"
        assert by["sl-mkt-BBB-x"].state == "filled"
        assert by["sl-mkt-CCC-x"].state == "abandoned"
        assert by["sl-mkt-DDD-x"].state == "sending", "young row untouched"


def test_resolve_stale_never_abandons_without_evidence(pg, owner_id):
    from dqengine.live import journal
    from datetime import datetime, timedelta, timezone
    seed_conns(pg, owner_id, "c1")
    with pg() as s:
        r = persistence.OrderJournal(
            connection_id="c1", client_order_id="sl-mkt-EEE-x",
            symbol="EEE", side="buy", qty=1.0, kind="market",
            state="sending")
        s.add(r)
        s.flush()
        r.created_at = datetime.now(timezone.utc) - timedelta(seconds=90)
        s.commit()
    with pg() as s:
        journal.resolve_stale(s, "c1", [], poll_ok=False)   # poll failed
        journal.resolve_stale(s, "c1", None, poll_ok=True)  # no fetch
        s.commit()
    with pg() as s:
        assert s.query(persistence.OrderJournal).one().state == "sending", \
            "absence is not evidence while the ledger/fetch is unreadable"


# ============================================================== the reversal
# A correct SELL, then a BUY of the same size, then another BUY: the position
# ends up reversed instead of reduced. Every test below is one of the paths
# that produced one of those buys. Scaled to the seeded sleeve (want SPY 5).

def _fill_row(cid, side, qty, exec_id, boid=""):
    from datetime import datetime, timezone
    return {"broker_order_id": boid, "broker_exec_id": exec_id,
            "client_order_id": cid, "symbol": "SPY", "side": side,
            "qty": float(qty), "price": 400.0, "fees": 0.0,
            "filled_at": datetime.now(timezone.utc), "order_level_avg": False}


def _poll_with(rows):
    """A _poll_executions stand-in that runs the REAL executions.poll with a
    canned adapter, so store()->fold_execution and note_fills all fire."""
    from dqengine.live import executions as ex_mod

    class A:
        def executions(self, creds, since=None):
            return rows
    return lambda adapter, creds, conn_id: ex_mod.poll(A(), creds, conn_id)


def test_reversal_scenario_never_buys_back(pg, owner_id, monkeypatch):
    """want 5, broker holds 8 (a manual buy). Correction sells 3. Then:
    (B2) the next full sweep polls the fill AFTER gathering gates; (B1) a
    resting TP limit sits in the journal; (B3) a fast pass reads the 2s
    cache. None of these may produce a BUY. Exactly one order all day."""
    from dqengine.live import executor
    from dqengine.live.book import book_for
    fb = blind_rig(pg, owner_id, monkeypatch, "cr1", "dr1", truth="enforce")
    fb.pos = {"SPY": 8.0}
    b = book_for("cr1")
    b.apply_audit({"SPY": 8.0}, [])
    assert executor.sync_broker_account("cr1", fast=True) == "submitted"
    subs = [o for t, o in fb.log if t == "submit"]
    assert [(o["side"], o["qty"]) for o in subs] == [("sell", 3.0)]
    sell_cid = subs[0]["client_order_id"]
    # the venue fills it; its position read now says 5; nothing polled yet
    fb.pos = {"SPY": 5.0}
    fb.orders = []
    # a resting take-profit limit (exit) also lives at the venue + journal
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="cr1", client_order_id="sl-tp-SPY-x", symbol="SPY",
            side="sell", qty=5.0, kind="limit", limit_price=450.0,
            state="submitted"))
        s.commit()
    tp = {"id": "tp1", "symbol": "SPY", "qty": 5.0, "side": "sell",
          "type": "limit", "limit_price": 450.0,
          "client_order_id": "sl-tp-SPY-x"}
    fb.orders = [tp]
    # --- (B2) full sweep: gather (row still open) -> poll ingests the fill
    monkeypatch.setattr(executor, "_poll_executions",
                        _poll_with([_fill_row(sell_cid, "sell", 3, "e-s3")]))
    executor._SYNC_GATE.pop("cr1", None)
    assert executor.sync_broker_account("cr1") == "audited"
    assert len([o for t, o in fb.log if t == "submit"]) == 1, \
        "post-poll sweep must not buy back the fill it just ingested"
    # --- (B3) fast pass right after, on the 2s gather cache
    assert executor.sync_broker_account("cr1", fast=True) in \
        ("submitted", "fallback")
    assert len([o for t, o in fb.log if t == "submit"]) == 1, \
        "cached gates must not net a filled row"
    # --- (B1) fresh gates, TP visible at venue/book: must not net the TP
    executor._GATHER_CACHE.pop("cr1", None)
    b.apply_audit({"SPY": 5.0}, [tp])
    b.recent.clear()
    executor.sync_broker_account("cr1", fast=True)
    executor._SYNC_GATE.pop("cr1", None)
    executor.sync_broker_account("cr1")
    assert len([o for t, o in fb.log if t == "submit"]) == 1, \
        "an exit order must never be netted into the market delta"


def test_invisible_open_row_freezes_instead_of_netting(pg, owner_id, monkeypatch):
    """want 5, have 5 (fill already reflected), journal row still `open`
    and NOT visible at the venue: netting would compute +5; the rule is
    freeze (no order) until evidence resolves the row."""
    from dqengine.live import executor
    from dqengine.live.book import book_for
    fb = blind_rig(pg, owner_id, monkeypatch, "cr2", "dr2", truth="enforce")
    fb.pos = {"SPY": 5.0}
    book_for("cr2").apply_audit({"SPY": 5.0}, [])
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="cr2", client_order_id="sl-mkt-SPY-ghost",
            symbol="SPY", side="buy", qty=5.0, kind="market",
            state="submitted"))
        s.commit()
    executor.sync_broker_account("cr2", fast=True)
    with pg() as s:
        d = s.get(persistence.Deployment, "dr2")
    acts = d.position["execution"]["actions"]
    assert any("frozen" in a and "SPY" in a for a in acts), acts
    executor._SYNC_GATE.pop("cr2", None)
    executor.sync_broker_account("cr2")
    assert [o for t, o in fb.log if t == "submit"] == []


def test_audit_transmit_enters_settle_window():
    """B5: an order the AUDITOR transmits must mark the symbol recent, or
    apply_audit wipes its ack with the pre-submit fetch and the fast path
    re-sends it."""
    from dqengine.live import executor
    from dqengine.live.book import Book
    from rig import IdlessBroker
    b = Book("cr4")
    b.apply_audit({}, [])
    fb = IdlessBroker(positions={})
    wrapped = executor.AuditAdapter(fb, b, transmit=True)
    wrapped.submit({}, "SPY", 5.0, "buy", client_order_id="sl-mkt-SPY-au")
    assert "SPY" in b.recent_symbols()
    assert len(b.open_orders_view()) == 1
    # the pre-submit fetch must not erase it
    b.apply_audit({}, [])
    assert len(b.open_orders_view()) == 1


def test_cancel_transitions_journal_row(pg, owner_id):
    """B6: a cancel closes the row; an open-but-canceled row would false-
    freeze its symbol under the visibility rule."""
    from dqengine.live import executor
    from dqengine.live import journal
    seed_conns(pg, owner_id, "c1")
    with pg() as s:
        journal.record_sending(
            s, "c1", {"symbol": "SPY", "qty": 5.0, "side": "sell",
                      "order_type": "limit", "limit_price": 450.0,
                      "client_order_id": "sl-tp-SPY-c", "deployment_id": None,
                      "rule_tag": None})
        journal.mark_submitted(s, "c1", "sl-tp-SPY-c", "", "submitted")
        s.commit()
    executor._journal_cancel("c1", {"action": "cancel", "symbol": "SPY",
                                       "client_order_id": "sl-tp-SPY-c",
                                       "broker_order_id": "bo1"})
    with pg() as s:
        assert s.query(persistence.OrderJournal).one().state == "canceled"


def test_fold_lift_is_per_side(pg, owner_id, monkeypatch):
    """B7: a sell and an equal buy sum to zero like 'nothing happened';
    per-side totals do not, so the freeze must stand."""
    from dqengine.live import executor
    from dqengine.live.book import book_for
    fb = blind_rig(pg, owner_id, monkeypatch, "cr5", "dr5", truth="enforce")
    fb.pos = {"SPY": 8.0}                      # want 5: a sell 3 is pending
    book_for("cr5").apply_audit({"SPY": 8.0}, [])
    with pg() as s:
        for cid, side in (("sl-mkt-SPY-a", "sell"), ("sl-mkt-SPY-b", "buy")):
            s.add(persistence.OrderJournal(
                connection_id="cr5", client_order_id=cid, symbol="SPY",
                side=side, qty=2.0, filled_qty=2.0, kind="market",
                state="filled"))
        s.commit()
    executor.sync_broker_account("cr5", fast=True)
    assert [o for t, o in fb.log if t == "submit"] == [], \
        "offsetting unfolded fills must still FREEZE the symbol"
    with pg() as s:
        notes = [r.note for r in s.query(persistence.OrderJournal)]
    assert notes == [None, None], "offsetting fills must not lift the freeze"
