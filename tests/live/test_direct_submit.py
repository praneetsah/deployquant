"""Direct-submit OMS (the live book): unit, parity-by-construction,
single-transmitter ownership, and the intent consumer."""
import time

from dqengine.live import executor, persistence
from dqengine.live.book import Book, book_for
from rig import FakeBroker, IdlessBroker, seed


# ------------------------------------------------------------------ book

def test_book_lifecycle_and_fast_path_gate(monkeypatch):
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    b = Book("c1")
    ok, why = b.fast_path_ok()
    assert not ok and "never audited" in why
    b.apply_audit({"SPY": 5.0}, [{"id": "o1", "symbol": "SPY", "qty": 2.0,
                                  "side": "sell", "type": "limit",
                                  "limit_price": 410.0}])
    assert b.fast_path_ok()[0]
    assert b.positions_view() == {"SPY": 5.0}
    assert len(b.open_orders_view()) == 1
    b.note_submit("cid1", "SPY", "buy", 3)
    assert "SPY" in b.recent_symbols()
    b.note_ack("cid1", {"id": "o2", "symbol": "SPY", "qty": 3.0,
                        "side": "buy", "type": "market", "status": "new"})
    assert not b.inflight and len(b.open_orders_view()) == 2
    b.note_fills([("o2", None, "SPY", 3.0)])
    assert b.positions_view() == {"SPY": 8.0}
    assert len(b.open_orders_view()) == 1, "filled order leaves the book"
    b.freeze("test")
    assert not b.fast_path_ok()[0]


def test_book_inflight_timeout_freezes(monkeypatch):
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    b = Book("c2")
    b.apply_audit({}, [])
    b.note_submit("cid", "SPY", "buy", 1)
    b.inflight["cid"]["at"] -= 30           # age it past INFLIGHT_MAX_S
    ok, why = b.fast_path_ok()
    assert not ok and "inflight" in why and b.frozen


def test_intent_seq_idempotency():
    b = Book("c3")
    assert b.intent_is_new("d", 5)
    assert not b.intent_is_new("d", 5)
    assert not b.intent_is_new("d", 4)
    assert b.intent_is_new("d", 6)


# --------------------------------------------- fast path + single owner

def test_fast_submits_from_book_and_audit_agrees(pg, owner_id, monkeypatch):
    """Parity by construction, live: audit builds the book and transmits
    (want 5, have 0 -> BUY 5); the NEXT want change goes out via the FAST
    path with ZERO broker reads; the following audit finds no drift and
    keeps the fast path open."""
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id)
    fb = FakeBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    # bootstrap: book empty -> fast refuses -> audit transmits and builds
    assert executor.sync_broker_account("cb", fast=True) == "fallback"
    assert executor.sync_broker_account("cb") == "audited"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert [(o["symbol"], o["qty"], o["side"]) for o in subs] \
        == [("SPY", 5.0, "buy")]
    fb.pos = {"SPY": 5.0}                   # the order filled
    fb.orders.clear()
    book_for("cb").apply_audit({"SPY": 5.0}, [])
    # the sleeve now wants 8
    with pg() as s:
        d = s.get(persistence.Deployment, "db1")
        d.position = {"holdings": [{"symbol": "SPY", "qty": 8,
                                    "last_price": 400.0}]}
        s.commit()
    reads_before = len([x for x in fb.log if x[0] in ("positions",)])
    assert executor.sync_broker_account("cb", fast=True) == "submitted"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert (subs[-1]["symbol"], subs[-1]["qty"], subs[-1]["side"]) \
        == ("SPY", 3.0, "buy")
    # the fast submit entered the book: pending covers it, a second fast
    # pass submits nothing
    assert executor.sync_broker_account("cb", fast=True) == "submitted"
    assert len([o for tag, o in fb.log if tag == "submit"]) == 2
    # audit while fast owns: broker shows the fill; no transmit, no drift
    fb.pos = {"SPY": 8.0}
    fb.orders.clear()
    n_subs = len([o for tag, o in fb.log if tag == "submit"])
    assert executor.sync_broker_account("cb") == "audited"
    assert len([o for tag, o in fb.log if tag == "submit"]) == n_subs, \
        "the auditor never transmits while the fast path owns"
    assert book_for("cb").frozen is None


def test_auditor_freezes_on_unexplained_drift(pg, owner_id, monkeypatch):
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id="cd", dep_id="dd1")
    fb = FakeBroker(positions={"SPY": 5.0})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    b = book_for("cd")
    b.apply_audit({"SPY": 5.0}, [])          # book believes 5, in sync
    # someone sells 3 shares manually at the broker: want 5, have 2
    fb.pos = {"SPY": 2.0}
    assert executor.sync_broker_account("cd") == "audited"
    assert b.frozen and "drift" in b.frozen
    assert not [o for tag, o in fb.log if tag == "submit"], \
        "drift is evidence, never an order"
    # frozen -> fast refuses -> the NEXT audit transmits the correction
    assert executor.sync_broker_account("cd", fast=True) == "fallback"
    assert executor.sync_broker_account("cd") == "audited"
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert (subs[-1]["symbol"], subs[-1]["qty"]) == ("SPY", 3.0)
    assert b.frozen is None, "clean correcting audit reopens the fast path"


def test_fast_pass_preserves_poll_error_state(pg, owner_id, monkeypatch):
    """2x-depth audit finding #2 (duplicate-position class): the fast path
    never polls, so it must carry the last audit's executions verdict
    forward -- clobbering a live poll-error with {error: None} would drop
    the UNKNOWN protection while the broker is unreachable."""
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id="cf", dep_id="df1")
    with pg() as s:
        d = s.get(persistence.Deployment, "df1")
        pos = dict(d.position or {})
        pos["execution"] = {"executions": {"new": 0, "skipped": 0,
                                           "error": "broker down"}}
        d.position = pos
        s.commit()
    fb = FakeBroker(positions={"SPY": 5.0})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    book_for("cf").apply_audit({"SPY": 5.0}, [])
    assert executor.sync_broker_account("cf", fast=True) == "submitted"
    with pg() as s:
        d = s.get(persistence.Deployment, "df1")
        ex = d.position["execution"]["executions"]
    assert ex["error"] == "broker down", \
        "the fast pass must not erase the outage verdict"


# ------------------------------------------------------- blind launcher

class SlowBroker(FakeBroker):
    """FakeBroker with per-submit latency + monotonic timing capture."""

    def __init__(self, *a, delay=0.05, **k):
        super().__init__(*a, **k)
        self.delay = delay
        self.times = []            # (symbol, side, start, end)

    def submit(self, creds, symbol, qty, side, **kw):
        t0 = time.monotonic()
        time.sleep(self.delay)
        out = super().submit(creds, symbol, qty, side, **kw)
        self.times.append((symbol, side, t0, time.monotonic()))
        return out


def _delta_batch(n_sell=3, n_buy=3):
    sells = [{"fn": None, "entry": {"action": "submit", "symbol": f"S{i}",
                                    "qty": 1.0, "side": "sell",
                                    "order_type": "market",
                                    "limit_price": None,
                                    "broker_order_id": "",
                                    "client_order_id": f"sl-mkt-S{i}-x",
                                    "status": "", "deployment_id": None},
              "sym": f"S{i}", "side": "sell", "qty": 1.0, "px": 100.0}
             for i in range(n_sell)]
    buys = [{"fn": None, "entry": {**sells[0]["entry"],
                                   "symbol": f"B{i}", "side": "buy",
                                   "client_order_id": f"sl-mkt-B{i}-x"},
             "sym": f"B{i}", "side": "buy", "qty": 1.0, "px": 100.0}
            for i in range(n_buy)]
    return sells, buys


def _run_launch(broker, sells, buys, buying_power, monkeypatch, pool="4"):
    import threading
    monkeypatch.setenv("SUBMIT_POOL", pool)
    for b in sells + buys:
        sym, side, cid = b["sym"], b["side"], b["entry"]["client_order_id"]
        b["fn"] = (lambda s=sym, sd=side, c=cid:
                   broker.submit({}, s, 1.0, sd, client_order_id=c))
    report = {"actions": [], "errors": []}
    entries = []
    executor._launch_market_batch(
        sells + buys, report, entries.append, threading.Lock(),
        {"tripped": False}, lambda e, x: None, buying_power)
    return report, entries


def test_blind_proven_launches_everything_at_once(pg, monkeypatch):
    fb = SlowBroker(positions={}, delay=0.05)
    sells, buys = _delta_batch()
    _run_launch(fb, sells, buys, buying_power=100000.0,
                monkeypatch=monkeypatch, pool="8")
    assert len(fb.times) == 6
    starts = [t0 for _, _, t0, _ in fb.times]
    assert max(starts) - min(starts) < 0.04, \
        "proven buying power: all six launch together (blind)"


def test_unproven_waves_sells_then_buys(pg, monkeypatch):
    fb = SlowBroker(positions={}, delay=0.05)
    sells, buys = _delta_batch()
    _run_launch(fb, sells, buys, buying_power=None, monkeypatch=monkeypatch)
    sell_ends = [e for _, sd, _, e in fb.times if sd == "sell"]
    buy_starts = [t0 for _, sd, t0, _ in fb.times if sd == "buy"]
    assert len(sell_ends) == 3 and len(buy_starts) == 3
    assert min(buy_starts) >= max(sell_ends) - 0.005, \
        "unproven: buys wait for every sell's acknowledgment"


def test_parallel_trip_stops_the_second_wave(pg, monkeypatch):
    import threading
    monkeypatch.setenv("SUBMIT_POOL", "4")
    fb = SlowBroker(positions={}, delay=0.01,
                    fail_submit_msg="Webull refused the request: "
                    "CAN_NOT_TRADING_FOR_FIXGW_NOT_READY_MARKET")
    sells, buys = _delta_batch()
    gateway = {"tripped": False}
    tripped_calls = []

    def rejecter(entry, e):
        gateway["tripped"] = True
        tripped_calls.append(entry["symbol"])

    for b in sells + buys:
        sym, sd, cid = b["sym"], b["side"], b["entry"]["client_order_id"]
        b["fn"] = (lambda s=sym, x=sd, c=cid:
                   fb.submit({}, s, 1.0, x, client_order_id=c))
    executor._launch_market_batch(
        sells + buys, {"actions": [], "errors": []}, lambda e: None,
        threading.Lock(), gateway, rejecter, None)
    buy_attempts = [s for (tag, s) in fb.log if tag == "submit_attempt"
                    and s.startswith("B")]
    assert buy_attempts == [], \
        "a tripped first wave must stop the buys entirely"


def test_launcher_parity_same_orders_as_serial(pg, monkeypatch):
    """Burst parity: pool=4 produces exactly the same ORDER SET as pool=1
    (order of arrival may differ; contents may not)."""
    outs = []
    for pool in ("1", "4"):
        fb = SlowBroker(positions={}, delay=0.005)
        sells, buys = _delta_batch()
        _, entries = _run_launch(fb, sells, buys, buying_power=1e9,
                                 monkeypatch=monkeypatch, pool=pool)
        outs.append({(e["symbol"], e["side"], e["qty"]) for e in entries})
    assert outs[0] == outs[1]


def test_launch_stagger_paces_the_wave(pg, monkeypatch):
    """Webull's limiter 429s on rapid bursts: the launcher paces launches
    by SUBMIT_STAGGER_MS within a wave."""
    monkeypatch.setenv("SUBMIT_STAGGER_MS", "40")
    fb = SlowBroker(positions={}, delay=0.01)
    sells, buys = _delta_batch(n_sell=3, n_buy=0)
    _run_launch(fb, sells, buys, buying_power=1e9,
                monkeypatch=monkeypatch, pool="8")
    starts = sorted(t0 for _, _, t0, _ in fb.times)
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.035 and starts[2] - starts[1] >= 0.035


def test_fast_pass_uses_gather_cache_within_ttl(pg, owner_id, monkeypatch):
    """Decision-in-hand work: the four bookkeeping sets serve from a 2s
    cache on fast passes. Proven by planting a sentinel in the cached
    qb-cooldown: the fast pass must skip that symbol's delta."""
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id="cg", dep_id="dg1")
    fb = FakeBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    # bootstrap: never-audited book -> the audit transmits and builds it
    assert executor.sync_broker_account("cg") == "audited"  # fills cache
    assert "cg" in executor._GATHER_CACHE
    executor._GATHER_CACHE["cg"]["qb"] = {"SPY"}       # sentinel
    executor._GATHER_CACHE["cg"]["at"] = time.time()
    n0 = len([o for tag, o in fb.log if tag == "submit"])
    assert executor.sync_broker_account("cg", fast=True) == "submitted"
    assert len([o for tag, o in fb.log if tag == "submit"]) == n0, \
        "cached qb-cooldown must gate the fast pass (cache is live)"
    executor._GATHER_CACHE["cg"]["at"] -= 10           # expire it
    # the bootstrap audit's order still rests at the venue AND in the
    # journal -- resubmitting against it would be the duplicate class the
    # journal closes. Cancel it everywhere so the re-want is legitimate.
    fb.orders.clear()
    with pg() as s:
        from dqengine.live.persistence import OrderJournal
        s.query(OrderJournal).update(
            {OrderJournal.state: "canceled"}, synchronize_session=False)
        s.commit()
    # ... and the BOOK (the auditor's own submit now enters the settle
    # window -- settle-window rule B5 -- so it correctly remembers it until
    # the window lapses; lapse it, then a clean audit forgets the order)
    bk = book_for("cg")
    bk.recent.clear()
    bk.apply_audit({}, [])
    assert executor.sync_broker_account("cg", fast=True) == "submitted"
    assert len([o for tag, o in fb.log if tag == "submit"]) == n0 + 1, \
        "expired cache refreshes from the DB and the delta submits"


# ---------------------------------------- idless acks (the duplicate bug)

def _idless_rig(pg, owner_id, monkeypatch, conn_id, dep_id):
    from dqengine.live import book as _book
    monkeypatch.setattr(_book, "FAST_PATH", True)
    seed(pg, owner_id, conn_id=conn_id, dep_id=dep_id)
    fb = IdlessBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    book_for(conn_id).apply_audit({}, [])
    return fb


def _submits(fb):
    return [o for tag, o in fb.log if tag == "submit"]


def test_idless_ack_second_fast_pass_no_duplicate(pg, owner_id, monkeypatch):
    """The duplicate-order bug: an acked submit whose venue returns no
    order id must still enter the book as pending -- otherwise the next
    fast pass recomputes the identical delta and duplicates the order."""
    fb = _idless_rig(pg, owner_id, monkeypatch, "ci1", "di1")
    assert executor.sync_broker_account("ci1", fast=True) == "submitted"
    assert len(_submits(fb)) == 1
    assert executor.sync_broker_account("ci1", fast=True) == "submitted"
    assert len(_submits(fb)) == 1, \
        "the idless ack must count as pending -- one order, not two"


def test_stale_audit_between_fast_passes_no_duplicate(pg, owner_id, monkeypatch):
    """The audit-lag flavor: a full audit runs while the broker reads are
    stale (the fill is not in positions yet, and the filled order is gone
    from open orders). It must not wipe the book's knowledge of its own
    just-submitted order -- or the next fast pass duplicates it."""
    fb = _idless_rig(pg, owner_id, monkeypatch, "ci2", "di2")
    assert executor.sync_broker_account("ci2", fast=True) == "submitted"
    assert len(_submits(fb)) == 1
    # full sweep against stale broker state: positions {}, open orders []
    assert executor.sync_broker_account("ci2") == "audited"
    b = book_for("ci2")
    assert b.frozen is None, "our own settle-window drift never freezes"
    assert len(_submits(fb)) == 1, "the auditor must not duplicate either"
    assert executor.sync_broker_account("ci2", fast=True) == "submitted"
    assert len(_submits(fb)) == 1, \
        "a stale audit must not reopen the duplicate window"


def test_note_fills_clears_idless_phantom_by_cid():
    b = Book("cu1")
    b.apply_audit({}, [])
    b.note_submit("sl-mkt-SPY-abc", "SPY", "buy", 5)
    b.note_ack("sl-mkt-SPY-abc",
               {"id": "", "symbol": "SPY", "qty": 5.0, "side": "buy",
                "type": "market", "client_order_id": "sl-mkt-SPY-abc",
                "status": "submitted"})
    assert len(b.open_orders_view()) == 1, "idless ack rests as pending"
    # the polled fill is the affirmative evidence that clears it
    b.note_fills([("real-oid-1", "sl-mkt-SPY-abc", "SPY", 5.0)])
    assert b.positions_view() == {"SPY": 5.0}
    assert b.open_orders_view() == []


def test_instant_filled_ack_stays_pending_until_evidence():
    """An ack that reports terminal `filled` must not vanish silently:
    positions lag fills at every broker, and the pending entry bridges
    the gap until a polled fill or post-settle audit clears it."""
    b = Book("cu2")
    b.apply_audit({}, [])
    b.note_submit("cidX", "SPY", "buy", 2)
    b.note_ack("cidX", {"id": "oX", "symbol": "SPY", "qty": 2.0,
                        "side": "buy", "type": "market",
                        "client_order_id": "cidX", "status": "filled"})
    assert len(b.open_orders_view()) == 1
    # a reject really is gone
    b.note_submit("cidY", "SPY", "buy", 2)
    b.note_ack("cidY", {"id": "oY", "symbol": "SPY", "qty": 2.0,
                        "side": "buy", "type": "market",
                        "client_order_id": "cidY", "status": "rejected"})
    assert len(b.open_orders_view()) == 1


def test_apply_audit_settle_window_semantics():
    b = Book("cu3")
    b.apply_audit({}, [])
    b.note_submit("sl-mkt-SPY-a", "SPY", "buy", 5)
    b.note_ack("sl-mkt-SPY-a",
               {"id": "", "symbol": "SPY", "qty": 5.0, "side": "buy",
                "type": "market", "client_order_id": "sl-mkt-SPY-a",
                "status": "submitted"})
    # stale audit inside the settle window: keeps the coherent book pair
    b.apply_audit({}, [])
    assert len(b.open_orders_view()) == 1, "phantom survives a stale audit"
    assert b.positions_view() == {}
    # broker lists the same order under its native id: fetch supersedes
    b.apply_audit({}, [{"id": "real1", "symbol": "SPY", "qty": 5.0,
                        "side": "buy", "type": "market",
                        "client_order_id": "sl-mkt-SPY-a"}])
    view = b.open_orders_view()
    assert [o["id"] for o in view] == ["real1"], \
        "fetched order with the same client id replaces the phantom"
    # settle expires -> broker truth wins wholesale
    for r in b.recent:
        r["at"] -= 30
    b.apply_audit({"SPY": 5.0}, [])
    assert b.open_orders_view() == []
    assert b.positions_view() == {"SPY": 5.0}


def test_poll_ingests_fills_into_the_book(pg, owner_id, monkeypatch):
    """executions.poll must note new fills into the connection's book --
    the affirmative evidence that clears idless phantoms and moves
    positions between audits."""
    from dqengine.live import executions as ex_mod
    from datetime import datetime, timezone
    seed(pg, owner_id, conn_id="ci3", dep_id="di3")
    b = book_for("ci3")
    b.apply_audit({}, [])
    b.note_submit("sl-mkt-SPY-poll", "SPY", "buy", 5)
    b.note_ack("sl-mkt-SPY-poll",
               {"id": "", "symbol": "SPY", "qty": 5.0, "side": "buy",
                "type": "market", "client_order_id": "sl-mkt-SPY-poll",
                "status": "submitted"})

    class FillAdapter:
        def executions(self, creds, since=None):
            return [{"broker_exec_id": "e-poll-1", "broker_order_id": "bo1",
                     "client_order_id": "sl-mkt-SPY-poll", "symbol": "SPY",
                     "side": "buy", "qty": 5.0, "price": 100.0, "fees": 0.0,
                     "filled_at": datetime.now(timezone.utc),
                     "order_level_avg": False}]

    n, skipped = ex_mod.poll(FillAdapter(), {}, "ci3")
    assert n == 1 and skipped == 0
    assert b.positions_view() == {"SPY": 5.0}
    assert b.open_orders_view() == [], "polled fill clears the phantom"
    # second poll of the same execution: deduped, book untouched
    ex_mod.poll(FillAdapter(), {}, "ci3")
    assert b.positions_view() == {"SPY": 5.0}
