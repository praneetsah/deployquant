"""The quote-breach emergency exit.

When a live quote crosses a stop that the broker cannot rest natively (a
simulated trailing stop on Webull, for example), `execute_quote_breach_exit`
sends a market order at once instead of waiting up to a minute for the bar to
close. It deliberately goes AROUND the normal order path: no book, no journal,
no connection lock, no session refresh. Its own protections are therefore the
only ones it has, and none of them had a test.

  * it sends at most one exit per deployment and symbol per cooldown window,
    however many quote frames breach the level (otherwise: sold twice);
  * it does nothing when the connection is paused, needs reconnecting, or is a
    live account whose deployment has not been confirmed for live trading;
  * it records an `sl-qb-` row, which is what the next sync cycle reads to
    avoid buying back the shares just sold for safety.

Rewriting it to go through the normal path would change behaviour. Pin it as
it is first.
"""
from datetime import datetime, timedelta, timezone

import pytest

from dqengine.live import executor
from dqengine.adapters.base import BrokerRejected

from rig import FakeBroker
from rig import seed
from dqengine.live import persistence

CONN, DEP = "cqb", "dqb-0123456789"


@pytest.fixture()
def rig(pg, owner_id, monkeypatch):
    seed(pg, owner_id, conn_id=CONN, dep_id=DEP)
    fb = FakeBroker(positions={"SPY": 5.0})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter", lambda name: fb)
    return fb


def _rows(pg):
    with pg() as s:
        return [(r.action, r.symbol, r.side, r.qty, r.client_order_id, r.status, r.rule_tag)
                for r in s.query(persistence.BrokerOrder).filter_by(connection_id=CONN)
                .order_by(persistence.BrokerOrder.created_at).all()]


def _set_conn(pg, **fields):
    with pg() as s:
        c = s.get(persistence.BrokerConnection, CONN)
        for k, v in fields.items():
            setattr(c, k, v)
        s.commit()


def test_a_breach_sells_a_long_at_market_and_records_the_row(pg, rig):
    entry = executor.execute_quote_breach_exit(CONN, DEP, "spy", -5.0, rule_id="ir:trail")
    sent = [o for tag, o in rig.log if tag == "submit"]
    assert [(o["symbol"], o["side"], o["qty"], o["type"]) for o in sent] == [("SPY", "sell", 5.0, "market")]
    assert entry["client_order_id"].startswith("sl-qb-SPY-dqb-0123-")     # sl-qb-{sym}-{dep[:8]}-{8 hex}
    assert len(entry["client_order_id"]) == len("sl-qb-SPY-dqb-0123-") + 8
    rows = _rows(pg)
    assert [(r[0], r[1], r[2], r[3], r[6]) for r in rows] == [("submit", "SPY", "sell", 5.0, "ir:trail")]
    assert rows[0][4] == sent[0]["client_order_id"]


def test_a_breach_buys_back_a_short(pg, rig):
    executor.execute_quote_breach_exit(CONN, DEP, "SPY", 5.0)
    assert [(o["side"], o["qty"]) for tag, o in rig.log if tag == "submit"] == [("buy", 5.0)]


def test_a_burst_of_quote_frames_sends_one_exit(pg, rig):
    first = executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0)
    again = [executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) for _ in range(5)]
    assert first is not None and again == [None] * 5
    assert len([1 for tag, _ in rig.log if tag == "submit"]) == 1
    assert len(_rows(pg)) == 1


def test_the_window_is_per_deployment_and_symbol_and_it_expires(pg, rig):
    assert executor.QB_COOLDOWN_S == 90
    executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0)
    # another symbol on the same deployment is not held back
    assert executor.execute_quote_breach_exit(CONN, DEP, "QQQ", -2.0) is not None
    # once the first row is older than the window, SPY can exit again
    with pg() as s:
        for r in s.query(persistence.BrokerOrder).filter_by(connection_id=CONN, symbol="SPY").all():
            r.created_at = datetime.now(timezone.utc) - timedelta(seconds=executor.QB_COOLDOWN_S + 5)
        s.commit()
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is not None
    assert len([1 for tag, _ in rig.log if tag == "submit"]) == 3


@pytest.mark.parametrize("status", ["reconnect_needed", "error", "pending"])
def test_it_does_nothing_on_a_connection_that_is_not_usable(pg, rig, status):
    _set_conn(pg, status=status)
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is None
    assert rig.log == [] and _rows(pg) == []


def test_it_does_nothing_while_paused(pg, rig):
    _set_conn(pg, settings={"paused": True})
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is None
    assert rig.log == [] and _rows(pg) == []


def test_a_live_account_needs_the_deployment_confirmed_for_live(pg, rig):
    _set_conn(pg, mode="live")
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is None
    assert rig.log == []
    with pg() as s:
        s.get(persistence.Deployment, DEP).live_confirmed = True
        s.commit()
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is not None
    assert len([1 for tag, _ in rig.log if tag == "submit"]) == 1


def test_dry_run_records_the_row_and_sends_nothing(pg, rig):
    _set_conn(pg, settings={"dry_run": True})
    entry = executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0)
    assert entry["status"] == "dry_run" and rig.log == []
    assert [(r[0], r[5]) for r in _rows(pg)] == [("submit", "dry_run")]
    # and the recorded row still starts the cooldown
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0) is None


def test_a_broker_rejection_is_recorded_as_refused_and_does_not_start_the_cooldown(pg, rig, monkeypatch):
    def reject(*a, **k):
        raise BrokerRejected("insufficient shares")
    monkeypatch.setattr(rig, "submit", reject)
    entry = executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0)
    assert entry["action"] == "refused" and "insufficient shares" in entry["status"]
    assert [(r[0]) for r in _rows(pg)] == ["refused"]
    # the cooldown looks for action == "submit": a refusal leaves the next frame free to try again
    monkeypatch.undo()


def test_zero_and_sub_step_quantities_send_nothing(pg, rig):
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", 0) is None
    assert executor.execute_quote_breach_exit(CONN, DEP, "SPY", -0.4) is None     # floors to 0 at qty_step 1
    assert rig.log == [] and _rows(pg) == []


def test_it_stays_off_the_normal_order_path(pg, rig):
    """No journal row, by design: this exit is not deduplicated by the journal
    but by its own sl-qb- cooldown. If a refactor routes it through the normal
    submit path, a journal row appears here and this fails -- on purpose."""
    executor.execute_quote_breach_exit(CONN, DEP, "SPY", -5.0)
    with pg() as s:
        assert s.query(persistence.OrderJournal).count() == 0
