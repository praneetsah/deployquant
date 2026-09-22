"""The first-sync preflight: a position nothing on this connection accounts
for stops the whole sweep.

Point a strategy at an account that already holds one of its universe
symbols and the replay, which starts flat, reads `want 0, have 74` and
market-sells 74 shares nobody asked it to touch. Every test below is either
that refusal or one of the things it must not break: the backoff, the
journal, the fast lane, a venue's rounding, and the adoption that works by
backdating the replay's start until it reproduces the shares held.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from dqengine.adapters.base import Caps
from dqengine.live import book as _book
from dqengine.live import executor, persistence
from dqengine.live.book import book_for

from rig import FakeBroker, seed, submitted


def _rig(pg, owner_id, monkeypatch, conn, dep, *, universe=("TQQQ",),
         holdings=(), at_broker=None, truth="observe", settings=None,
         fast=False, qty_step=1.0):
    """One deployment whose replay holds `holdings`, on an account holding
    `at_broker`. The default is the dangerous shape: a universe symbol the
    strategy holds none of and has never ordered."""
    seed(pg, owner_id, conn_id=conn, dep_id=dep, truth=truth)
    with pg() as s:
        d = s.get(persistence.Deployment, dep)
        d.ir = None
        d.universe = [u.upper() for u in universe]
        d.position = {"holdings": [{"symbol": sym, "qty": q,
                                    "last_price": 100.0}
                                   for sym, q in holdings],
                      "last_prices": {u.upper(): 100.0 for u in universe}}
        s.commit()
        if settings is not None:
            c = s.get(persistence.BrokerConnection, conn)
            c.settings = dict(settings)
            s.commit()
    at_broker = dict(at_broker or {})
    fb = FakeBroker(positions=at_broker)
    fb.caps = Caps(qty_step=qty_step)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    monkeypatch.setattr(_book, "FAST_PATH", fast)
    book_for(conn).apply_audit(dict(at_broker), [])
    executor._GATHER_CACHE.pop(conn, None)
    executor._SYNC_GATE.pop(conn, None)
    return fb


def _report(pg, dep) -> dict:
    with pg() as s:
        d = s.get(persistence.Deployment, dep)
        return ((d.position or {}).get("execution") or {})


def _order(conn, dep, sym, **kw):
    row = {"connection_id": conn, "deployment_id": dep, "symbol": sym,
           "qty": 1.0, "side": "buy", "order_type": "market",
           "action": "submit"}
    row.update(kw)
    return persistence.BrokerOrder(**row)


# ------------------------------------------------------------ the refusal

def test_a_position_nothing_accounts_for_refuses_the_whole_sweep(
        pg, owner_id, monkeypatch, capsys):
    fb = _rig(pg, owner_id, monkeypatch, "pf1", "pfd1",
              at_broker={"TQQQ": 74.0})

    assert executor.sync_broker_account("pf1") == "audited"

    assert submitted(fb) == [], "nothing was sent"
    assert fb.log == [], "not even a cancel"
    rep = _report(pg, "pfd1")
    assert any("TQQQ" in e and "74" in e for e in rep["errors"])
    assert any("dqengine adopt" in e for e in rep["errors"])
    out = capsys.readouterr().out
    assert "first-sync check refused this sweep" in out and "TQQQ" in out


def test_the_refusal_names_the_symbol_the_quantity_and_the_way_out(
        pg, owner_id, monkeypatch):
    _rig(pg, owner_id, monkeypatch, "pf2", "pfd2", at_broker={"TQQQ": 74.0})
    executor.sync_broker_account("pf2")
    line = [e for e in _report(pg, "pfd2")["errors"] if "TQQQ" in e][0]
    assert "74" in line
    assert "holds none of it" in line
    assert "nothing on this connection has ever ordered TQQQ" in line
    assert "dqengine adopt" in line
    assert len(line) <= 300, "the report truncates at 300 characters"


def test_a_short_position_nothing_accounts_for_is_refused_too(
        pg, owner_id, monkeypatch):
    fb = _rig(pg, owner_id, monkeypatch, "pf3", "pfd3",
              at_broker={"TQQQ": -12.0})
    executor.sync_broker_account("pf3")
    assert submitted(fb) == []
    assert any("-12" in e for e in _report(pg, "pfd3")["errors"])


def test_several_unaccounted_symbols_report_the_first_and_the_count(
        pg, owner_id, monkeypatch, capsys):
    _rig(pg, owner_id, monkeypatch, "pf4", "pfd4",
         universe=("TQQQ", "QQQ", "SPY"),
         at_broker={"TQQQ": 5.0, "QQQ": 7.0})
    executor.sync_broker_account("pf4")
    errs = _report(pg, "pfd4")["errors"]
    assert any("QQQ" in e and "and 1 more symbol" in e for e in errs)
    # every one of them is printed, not just the one the report carries
    out = capsys.readouterr().out
    assert out.count("first-sync check refused this sweep") == 2


# --------------------------------------------------------- what still passes

def test_a_replay_that_already_holds_the_shares_passes_silently(
        pg, owner_id, monkeypatch, capsys):
    """The backdated-start adoption: `start_date` moved back until the
    replay reproduces what the account holds. Nothing to reconcile, so
    nothing to say."""
    fb = _rig(pg, owner_id, monkeypatch, "pf5", "pfd5",
              holdings=(("TQQQ", 74.0),), at_broker={"TQQQ": 74.0})

    assert executor.sync_broker_account("pf5") == "audited"

    assert submitted(fb) == []
    assert _report(pg, "pfd5")["errors"] == []
    assert "first-sync check" not in capsys.readouterr().out


def test_a_quantity_the_two_disagree_on_is_ordinary_reconciliation(
        pg, owner_id, monkeypatch):
    """want 5, have 8 -- a manual trade in a symbol the strategy holds. The
    replay vouches for the symbol, so this is the disagreement the fold
    rail, the journal's visibility rule and the auditor's drift freeze are
    for, and the preflight stays out of it."""
    fb = _rig(pg, owner_id, monkeypatch, "pf6", "pfd6",
              holdings=(("TQQQ", 5.0),), at_broker={"TQQQ": 8.0})

    executor.sync_broker_account("pf6")

    assert [(o["side"], o["qty"]) for o in submitted(fb)] == [("sell", 3.0)]
    assert _report(pg, "pfd6")["errors"] == []


def test_a_universe_symbol_both_sides_are_flat_in_is_not_a_position(
        pg, owner_id, monkeypatch):
    """The switcher shape: a wide universe, most of it never traded. A
    symbol nobody holds has nothing to disagree about."""
    fb = _rig(pg, owner_id, monkeypatch, "pf7", "pfd7",
              universe=tuple(f"S{i}" for i in range(40)) + ("TQQQ",),
              holdings=(("TQQQ", 3.0),), at_broker={"TQQQ": 3.0})

    assert executor.sync_broker_account("pf7") == "audited"
    assert submitted(fb) == [] and _report(pg, "pfd7")["errors"] == []


def test_a_symbol_outside_the_universe_is_ignored(pg, owner_id, monkeypatch):
    """Unchanged behaviour: the executor trades its universe and leaves the
    rest of the account alone, so a holding it was never pointed at is not
    its business to refuse over."""
    fb = _rig(pg, owner_id, monkeypatch, "pf8", "pfd8",
              universe=("TQQQ",), holdings=(("TQQQ", 2.0),),
              at_broker={"TQQQ": 2.0, "GME": 100.0})

    assert executor.sync_broker_account("pf8") == "audited"
    assert submitted(fb) == [] and _report(pg, "pfd8")["errors"] == []


def test_a_zero_quantity_row_from_the_broker_is_not_a_position(
        pg, owner_id, monkeypatch):
    fb = _rig(pg, owner_id, monkeypatch, "pf9", "pfd9",
              at_broker={"TQQQ": 0.0})

    assert executor.sync_broker_account("pf9") == "audited"
    assert submitted(fb) == [] and _report(pg, "pfd9")["errors"] == []


def test_dust_below_the_venues_step_is_not_a_position(pg, owner_id,
                                                      monkeypatch):
    """A fractional remainder no order on this venue could express. Decided
    by the same rounding every order in the executor goes through."""
    fb = _rig(pg, owner_id, monkeypatch, "pf10", "pfd10",
              at_broker={"TQQQ": 0.4}, qty_step=1.0)

    assert executor.sync_broker_account("pf10") == "audited"
    assert submitted(fb) == [] and _report(pg, "pfd10")["errors"] == []


def test_the_same_dust_is_a_position_on_a_venue_that_trades_fractions(
        pg, owner_id, monkeypatch):
    fb = _rig(pg, owner_id, monkeypatch, "pf11", "pfd11",
              at_broker={"TQQQ": 0.4}, qty_step=0.0001)

    executor.sync_broker_account("pf11")
    assert submitted(fb) == []
    assert any("TQQQ" in e for e in _report(pg, "pfd11")["errors"])


# ----------------------------------------------------- what counts as history

def test_an_order_this_executor_placed_makes_the_symbol_known(
        pg, owner_id, monkeypatch):
    """Prod inertness: every symbol a live connection holds was ordered
    through this executor, and `broker_orders` is append-only."""
    fb = _rig(pg, owner_id, monkeypatch, "pf12", "pfd12",
              at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(_order("pf12", "pfd12", "TQQQ"))
        s.commit()
    executor._GATHER_CACHE.pop("pf12", None)

    executor.sync_broker_account("pf12")

    assert _report(pg, "pfd12")["errors"] == [], "no refusal"
    assert [(o["side"], o["qty"]) for o in submitted(fb)] == [("sell", 74.0)]


@pytest.mark.parametrize("action", ["submit", "cancel", "refused", "replace"])
def test_any_broker_order_row_counts_whatever_it_did(pg, owner_id,
                                                    monkeypatch, action):
    _rig(pg, owner_id, monkeypatch, f"pf13{action}", f"pfd13{action}",
         at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(_order(f"pf13{action}", f"pfd13{action}", "TQQQ",
                     action=action))
        s.commit()
    executor._GATHER_CACHE.pop(f"pf13{action}", None)

    executor.sync_broker_account(f"pf13{action}")
    assert _report(pg, f"pfd13{action}")["errors"] == []


def test_a_write_ahead_journal_row_alone_makes_the_symbol_known(
        pg, owner_id, monkeypatch):
    """An order whose wire call was never answered leaves a journal row and
    no broker_orders row. Those shares are ours."""
    _rig(pg, owner_id, monkeypatch, "pf14", "pfd14",
         at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id="pf14", client_order_id="sl-mkt-TQQQ-1",
            symbol="TQQQ", side="buy", qty=74.0, kind="market",
            state="sending"))
        s.commit()
    executor._GATHER_CACHE.pop("pf14", None)

    executor.sync_broker_account("pf14")
    assert _report(pg, "pfd14")["errors"] == []


def _exec_row(conn, cid, sym="TQQQ", exec_id="e1"):
    return persistence.Execution(
        connection_id=conn, broker_exec_id=exec_id, client_order_id=cid,
        symbol=sym, signed_qty=74.0, price=50.0,
        filled_at=datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("cid", ["sl-mkt-TQQQ-1", "en-stp-TQQQ-1"])
def test_a_fill_of_our_own_order_id_makes_the_symbol_known(
        pg, owner_id, monkeypatch, cid):
    _rig(pg, owner_id, monkeypatch, "pf15", "pfd15",
         at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(_exec_row("pf15", cid))
        s.commit()
    executor._GATHER_CACHE.pop("pf15", None)

    executor.sync_broker_account("pf15")
    assert _report(pg, "pfd15")["errors"] == []


@pytest.mark.parametrize("cid", [None, "", "someone-elses-order"])
def test_the_accounts_own_history_does_not_make_the_symbol_known(
        pg, owner_id, monkeypatch, cid):
    """THE POINT OF THE RESTRICTION. `executions.poll` pulls the ACCOUNT'S
    history, the user's own trades included, and on a connection with no
    stored fills it asks for everything the broker will give -- earlier in
    this same sweep. If any execution row counted as history, the check
    would refuse one sweep and then sell the shares on the next."""
    fb = _rig(pg, owner_id, monkeypatch, "pf16", "pfd16",
              at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(_exec_row("pf16", cid))
        s.commit()
    executor._GATHER_CACHE.pop("pf16", None)

    executor.sync_broker_account("pf16")
    assert submitted(fb) == []
    assert any("TQQQ" in e for e in _report(pg, "pfd16")["errors"])


def test_a_second_sweep_after_a_real_poll_still_refuses(pg, owner_id,
                                                        monkeypatch):
    """The whole hazard, end to end: the poll stores the account's own TQQQ
    history on sweep one, and sweep two must still refuse."""
    fb = _rig(pg, owner_id, monkeypatch, "pf17", "pfd17",
              at_broker={"TQQQ": 74.0}, truth="observe")
    rows = [{"broker_order_id": "o9", "broker_exec_id": "e9",
             "client_order_id": "", "symbol": "TQQQ", "side": "buy",
             "qty": 74.0, "price": 50.0, "fees": 0.0,
             "order_level_avg": False,
             "filled_at": datetime.now(timezone.utc) - timedelta(days=3)}]

    class Vendor:
        def executions(self, creds, since=None):
            return rows
    from dqengine.live import executions as ex_mod
    monkeypatch.setattr(
        executor, "_poll_executions",
        lambda adapter, creds, conn_id: ex_mod.poll(Vendor(), creds, conn_id))
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: False)

    executor.sync_broker_account("pf17")
    with pg() as s:
        assert s.query(persistence.Execution).count() == 1, "the poll ran"
    executor._GATHER_CACHE.pop("pf17", None)
    executor._SYNC_GATE.pop("pf17", None)

    executor.sync_broker_account("pf17")

    assert submitted(fb) == [], "the account's own history is not our history"
    assert any("TQQQ" in e for e in _report(pg, "pfd17")["errors"])


# --------------------------------------------------------------- the hazards

def test_a_refused_sweep_writes_no_journal_row_and_no_broker_order(
        pg, owner_id, monkeypatch):
    _rig(pg, owner_id, monkeypatch, "pf18", "pfd18",
         at_broker={"TQQQ": 74.0})

    executor.sync_broker_account("pf18")

    with pg() as s:
        assert s.query(persistence.OrderJournal).count() == 0
        assert s.query(persistence.BrokerOrder).count() == 0


def test_a_refused_sweep_records_no_backoff_verdict(pg, owner_id,
                                                    monkeypatch):
    """`report["submit_backoff"]` is set by the line reconcile never
    reaches, so the tail has no verdict to persist and a connection with no
    backoff keeps none."""
    _rig(pg, owner_id, monkeypatch, "pf19", "pfd19",
         at_broker={"TQQQ": 74.0})

    executor.sync_broker_account("pf19")

    assert "submit_backoff" not in _report(pg, "pfd19")
    assert executor._get_submit_backoff("pf19") is None
    with pg() as s:
        assert s.query(persistence.KV).count() == 0


def test_a_refused_sweep_leaves_a_live_backoff_exactly_as_it_was(
        pg, owner_id, monkeypatch):
    """The other half: a still-active gateway backoff from an earlier sweep
    must survive a refusal untouched -- neither cleared nor re-dated."""
    _rig(pg, owner_id, monkeypatch, "pf20", "pfd20",
         at_broker={"TQQQ": 74.0})
    until = (datetime.now(executor.ET) + timedelta(minutes=30)).isoformat()
    executor._save_submit_backoff("pf20", {"until": until,
                                           "reason": "market not ready"})

    executor.sync_broker_account("pf20")

    assert executor._get_submit_backoff("pf20") == {
        "until": until, "reason": "market not ready"}


def test_the_fast_lane_refuses_the_same_way(pg, owner_id, monkeypatch):
    fb = _rig(pg, owner_id, monkeypatch, "pf21", "pfd21",
              at_broker={"TQQQ": 74.0}, fast=True)

    assert executor.sync_broker_account("pf21", fast=True) == "error"

    assert submitted(fb) == []
    assert any("TQQQ" in e for e in _report(pg, "pfd21")["errors"])
    with pg() as s:
        assert s.query(persistence.OrderJournal).count() == 0


def test_the_fast_lane_falling_back_refuses_again_and_sends_nothing(
        pg, owner_id, monkeypatch):
    """`handle_intent` falls back to the full sweep on anything short of
    `submitted`, and the full sweep is the same entry point."""
    fb = _rig(pg, owner_id, monkeypatch, "pf22", "pfd22",
              at_broker={"TQQQ": 74.0}, fast=True)
    calls = []

    def sweep(conn_id):
        calls.append(conn_id)
        executor._SYNC_GATE.pop(conn_id, None)
        return executor.sync_broker_account(conn_id)

    assert executor.handle_intent("pf22", sweep) == "fallback:error"
    assert calls == ["pf22"]
    assert submitted(fb) == []
    assert any("TQQQ" in e for e in _report(pg, "pfd22")["errors"])


def test_the_audit_only_pass_refuses_too(pg, owner_id, monkeypatch):
    """When the fast path owns transmission the auditor runs the same
    reconcile read-only. It is a second call site, and it gets the same
    history: an auditor that sailed past the refusal would report a clean
    pass on an account the fast lane is refusing."""
    fb = _rig(pg, owner_id, monkeypatch, "pf28", "pfd28",
              at_broker={"TQQQ": 74.0}, fast=True)

    assert executor.sync_broker_account("pf28") == "audited"

    assert submitted(fb) == []
    errs = _report(pg, "pfd28")["errors"]
    assert any("TQQQ" in e for e in errs)
    assert not any("auditor drift" in e for e in errs), \
        "the refusal is the verdict, not a drift freeze on top of it"
    assert book_for("pf28").frozen is None


def test_a_dry_run_shows_the_refusal(pg, owner_id, monkeypatch):
    """A rehearsal against the real account must show what the real run
    would do, and the real run refuses."""
    fb = _rig(pg, owner_id, monkeypatch, "pf23", "pfd23",
              at_broker={"TQQQ": 74.0}, settings={"dry_run": True})

    executor.sync_broker_account("pf23")

    assert any("TQQQ" in e for e in _report(pg, "pfd23")["errors"])
    assert submitted(fb) == []
    with pg() as s:
        assert s.query(persistence.BrokerOrder).count() == 0, \
            "not even the dry_run preview row"


def test_a_refused_sweep_still_records_the_frame(pg, owner_id, monkeypatch):
    """The recorder runs in the tail, off both locks: a refusal is exactly
    the sweep a replay wants, and the history it was handed is in the
    frame."""
    monkeypatch.setenv("DQENGINE_RECORD_RECONCILE", "1")
    from dqengine.live import frames
    monkeypatch.setattr(frames, "_LAST_RETENTION", 1e18)
    _rig(pg, owner_id, monkeypatch, "pf24", "pfd24",
         at_broker={"TQQQ": 74.0})
    with pg() as s:
        s.add(_order("pf24", "pfd24", "QQQ"))
        s.commit()
    executor._GATHER_CACHE.pop("pf24", None)

    executor.sync_broker_account("pf24")
    assert frames.flush(10.0)

    with pg() as s:
        rows = (s.query(persistence.ReconcileFrame)
                .filter_by(connection_id="pf24").all())
    assert rows, "the refused sweep was recorded"
    assert rows[0].frame["known_symbols"] == ["QQQ"]


# ------------------------------------------------------- the pure function

def _pf(positions, desired, known=(), step=1.0):
    return executor.preflight_unknown_positions(positions, desired, known,
                                                step)


def test_the_pure_function_flags_only_unaccounted_holdings():
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 0.0}) != []
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 0.0}, known={"TQQQ"}) == []
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 74.0}) == []
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 5.0}) == []
    assert _pf({}, {"TQQQ": 0.0}) == []
    assert _pf({"TQQQ": 0.0}, {"TQQQ": 0.0}) == []
    assert _pf({"GME": 9.0}, {"TQQQ": 0.0}) == []


def test_the_pure_function_is_case_insensitive_about_known_symbols():
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 0.0}, known={"tqqq"}) == []


def test_the_pure_function_reports_one_line_per_symbol_in_symbol_order():
    lines = _pf({"AAA": 1.0, "ZZZ": 2.0, "MMM": 3.0},
                {"AAA": 0.0, "ZZZ": 0.0, "MMM": 0.0})
    assert [ln.split(":")[0] for ln in lines] == ["AAA", "MMM", "ZZZ"]


def test_the_pure_function_rounds_both_sides_on_the_venues_step():
    # dust the venue cannot express is not a position
    assert _pf({"TQQQ": 0.9}, {"TQQQ": 0.0}, step=1.0) == []
    assert _pf({"TQQQ": 0.9}, {"TQQQ": 0.0}, step=0.1) != []
    # a model holding dust does not vouch for a whole-share position
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 0.9}, step=1.0) != []
    assert _pf({"TQQQ": 74.0}, {"TQQQ": 0.9}, step=0.1) == []


# ------------------------------------------------------------ the history read

def test_the_known_read_is_one_set_from_three_tables(pg, owner_id):
    seed(pg, owner_id, conn_id="pfk", dep_id="pfkd")
    seed(pg, owner_id, conn_id="pfk2", dep_id="pfkd2")
    with pg() as s:
        s.add(_order("pfk", "pfkd", "AAA"))
        s.add(_order("pfk", "pfkd", "aaa"))
        s.add(_order("pfk2", "pfkd2", "OTHER"))
        s.add(persistence.OrderJournal(
            connection_id="pfk", client_order_id="sl-mkt-BBB-1", symbol="BBB",
            side="buy", qty=1.0, kind="market", state="sending"))
        s.add(_exec_row("pfk", "sl-mkt-CCC-1", sym="CCC", exec_id="x1"))
        s.add(_exec_row("pfk", "", sym="DDD", exec_id="x2"))
        s.commit()
    with pg() as s:
        got = executor._known_symbols_read(s, "pfk")
    assert got == {"AAA", "BBB", "CCC"}, \
        "our own rows only, upper-cased, this connection only"


def test_the_known_read_is_cached_with_the_rest_of_the_gather(
        pg, owner_id, monkeypatch):
    """One query pass per sweep, served from the 2s cache to a fast pass --
    like every other gathered set."""
    _rig(pg, owner_id, monkeypatch, "pf25", "pfd25",
         holdings=(("TQQQ", 1.0),), at_broker={"TQQQ": 1.0}, fast=True)
    calls = []
    real = executor._known_symbols_read
    monkeypatch.setattr(executor, "_known_symbols_read",
                        lambda s, c: (calls.append(c), real(s, c))[1])

    executor.sync_broker_account("pf25")
    assert len(calls) == 1
    executor.sync_broker_account("pf25", fast=True)
    assert len(calls) == 1, "the fast pass read the cache"
    assert executor._GATHER_CACHE["pf25"]["known_symbols"] == set()


def test_a_deployment_with_no_universe_has_nothing_to_check(
        pg, owner_id, monkeypatch):
    fb = _rig(pg, owner_id, monkeypatch, "pf26", "pfd26", universe=(),
              at_broker={"TQQQ": 74.0})
    assert executor.sync_broker_account("pf26") == "audited"
    assert submitted(fb) == [] and _report(pg, "pfd26")["errors"] == []


def test_the_start_date_is_untouched_by_a_refusal(pg, owner_id, monkeypatch):
    """A refusal changes nothing about the deployment but its report: the
    replay, and therefore what it holds, is exactly what it was."""
    _rig(pg, owner_id, monkeypatch, "pf27", "pfd27",
         at_broker={"TQQQ": 74.0})
    executor.sync_broker_account("pf27")
    with pg() as s:
        d = s.get(persistence.Deployment, "pfd27")
        assert d.start_date == date(2026, 8, 1)
        assert d.status == "running" and d.reconciled_from is None
