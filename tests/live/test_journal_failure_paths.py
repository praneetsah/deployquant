"""The order journal when its own bookkeeping fails.

`executor._journal_before` is the write-ahead step in front of every
submit, and its docstring states two rules:

  1. a bookkeeping failure must never block an order -- a database blip at
     15:59 must not freeze every order for the close;
  2. the duplicate verdict always stands -- if the journal already holds this
     exact intent, the submit is called off.

The happy path and the duplicate path had tests. The failure path (`except ->
return True`) did not, and it is the easiest of the three to invert while
moving the code: turn that `True` into `False` and a database hiccup stops all
trading; drop the duplicate `False` and the gate stops gating.
"""
from dqengine.live import persistence
from dqengine.live import executor as be

from rig import seed_conns

CONN = "conn-h4"


def _entry(cid="sl-mkt-TQQQ-0badc0de", **over):
    e = {"action": "submit", "client_order_id": cid, "symbol": "TQQQ", "side": "buy",
         "qty": 100.0, "order_type": "market", "deployment_id": None}
    e.update(over)
    return e


def _rows(pg):
    with pg() as s:
        return [(r.client_order_id, r.state) for r in s.query(persistence.OrderJournal).all()]


def test_a_new_intent_is_recorded_and_goes_to_the_wire(pg, owner_id):
    seed_conns(pg, owner_id, CONN)
    assert be._journal_before(CONN, _entry()) is True
    assert _rows(pg) == [("sl-mkt-TQQQ-0badc0de", "sending")]


def test_the_same_intent_again_is_called_off(pg, owner_id):
    seed_conns(pg, owner_id, CONN)
    assert be._journal_before(CONN, _entry()) is True
    assert be._journal_before(CONN, _entry()) is False           # the duplicate verdict
    assert len(_rows(pg)) == 1


def test_a_database_failure_never_blocks_the_order(pg, owner_id, monkeypatch, capsys):
    seed_conns(pg, owner_id, CONN)

    def boom():
        raise RuntimeError("connection reset by peer")
    monkeypatch.setattr(persistence, "SessionLocal", boom)
    assert be._journal_before(CONN, _entry()) is True            # the order still goes
    assert "[journal] pre-write failed" in capsys.readouterr().out   # ...and it says so


def test_a_failure_inside_the_write_never_blocks_the_order(pg, owner_id, monkeypatch, capsys):
    seed_conns(pg, owner_id, CONN)
    from dqengine.live import journal

    def boom(*a, **k):
        raise RuntimeError("deadlock detected")
    monkeypatch.setattr(journal, "record_sending", boom)
    assert be._journal_before(CONN, _entry()) is True
    assert "[journal] pre-write failed" in capsys.readouterr().out


def test_the_duplicate_verdict_survives_a_later_failure(pg, owner_id, monkeypatch):
    """Rule 2 outranks rule 1 only while the journal can answer. Once the row
    exists, a healthy journal says False every time."""
    seed_conns(pg, owner_id, CONN)
    assert be._journal_before(CONN, _entry()) is True
    for _ in range(3):
        assert be._journal_before(CONN, _entry()) is False


def test_things_the_journal_does_not_gate(pg, owner_id):
    """The read-only auditor (conn_id None), cancels and replaces, and an entry
    with no client id pass straight through and write nothing."""
    seed_conns(pg, owner_id, CONN)
    assert be._journal_before(None, _entry()) is True
    assert be._journal_before(CONN, _entry(action="cancel")) is True
    assert be._journal_before(CONN, _entry(action="replace")) is True
    assert be._journal_before(CONN, _entry(cid="")) is True
    assert _rows(pg) == []


def test_ack_and_cancel_bookkeeping_swallow_their_own_failures(pg, owner_id, monkeypatch, capsys):
    """An order is already at the broker when these run. Raising here would
    abort the sweep with rows unsaved; they log and carry on instead."""
    seed_conns(pg, owner_id, CONN)

    def boom():
        raise RuntimeError("connection reset by peer")
    monkeypatch.setattr(persistence, "SessionLocal", boom)
    be._journal_after(CONN, _entry(), {"id": "B1", "status": "open"})
    be._journal_after(CONN, _entry(), None, rejected_note="insufficient buying power")
    be._journal_cancel(CONN, _entry())
    out = capsys.readouterr().out
    assert out.count("[journal] post-write failed") == 2
    assert out.count("[journal] cancel-write failed") == 1


def test_an_ack_moves_the_row_and_a_reject_closes_it(pg, owner_id):
    seed_conns(pg, owner_id, CONN)
    be._journal_before(CONN, _entry("sl-mkt-TQQQ-aaaaaaaa"))
    be._journal_before(CONN, _entry("sl-mkt-TQQQ-bbbbbbbb"))
    be._journal_after(CONN, _entry("sl-mkt-TQQQ-aaaaaaaa"), {"id": "B1", "status": "open"})
    be._journal_after(CONN, _entry("sl-mkt-TQQQ-bbbbbbbb"), None, rejected_note="nope")
    states = dict(_rows(pg))
    assert states["sl-mkt-TQQQ-aaaaaaaa"] in ("submitted", "open")
    assert states["sl-mkt-TQQQ-bbbbbbbb"] == "rejected"
