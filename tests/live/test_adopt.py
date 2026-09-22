"""`dqengine adopt`: one command from an account that already holds shares
to a connection whose own fills drive the accounting.

The two things worth pinning are the order and the gate. The order, because
a run that is stopped or refused must have written nothing to the ledger and
moved no boundary. The gate, because there is no `--yes`: the only way past
the confirmation is to type the account's own label, which is the mistake
worth catching -- adopting the wrong account.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from dqengine.live import adopt, persistence, setup, vault
from dqengine.sandbox import pyrunner

ALGO = '''"""A tiny algorithm."""
from AlgorithmImports import *


class Tiny(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 9, 1)
        self.set_cash(1000)
        self.add_equity("SPY", Resolution.MINUTE)

    def on_data(self, data):
        pass
'''

CREDS = {"key_id": "k", "secret_key": "s", "paper": True}


@pytest.fixture(autouse=True)
def _inproc(monkeypatch):
    """The manifest pass runs in THIS process: no docker in a test."""
    monkeypatch.setattr(pyrunner, "ENGINE_MODE", "inproc")


class Vendor:
    """A broker that holds shares and remembers what it was asked."""

    def __init__(self, positions=None, orders=None, fills=None, skipped=0):
        self.pos = dict(positions or {})
        self.orders = list(orders or [])
        self.fills = list(fills or [])
        self.skipped = skipped
        self.sessions = 0

    def ensure_session(self, creds):
        self.sessions += 1
        return None

    def positions(self, creds):
        return dict(self.pos)

    def open_orders(self, creds):
        return list(self.orders)

    def executions(self, creds, since=None):
        if not self.skipped:
            return list(self.fills)
        from dqengine.adapters.base import ExecutionBatch
        batch = ExecutionBatch(list(self.fills))
        batch.skipped = self.skipped
        return batch


def fill(sym="SPY", qty=10.0, px=50.0, exec_id="e1", when=None):
    return {"broker_order_id": "o1", "broker_exec_id": exec_id,
            "client_order_id": "", "symbol": sym, "side": "buy",
            "qty": qty, "price": px, "fees": 0.0, "order_level_avg": False,
            "filled_at": when or datetime(2026, 1, 5, 15, 0,
                                          tzinfo=timezone.utc)}


@pytest.fixture()
def rig(pg, tmp_path, monkeypatch):
    """One algorithm file, one fake venue, and the two ports the replay
    reads. `held` is what the replay is made to hold."""
    algo = tmp_path / "tiny.py"
    algo.write_text(ALGO)
    state = {"vendor": Vendor(), "held": {}, "out": []}

    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: state["vendor"])
    monkeypatch.setattr("dqengine.adapters.catalog.adapter_class",
                        lambda name: type(state["vendor"]))
    monkeypatch.setattr(adopt, "replay_view", lambda dep_id, refresh_bars=True:
                        {"holdings": dict(state["held"]),
                         "universe": ["SPY"], "start_date": "2026-09-01",
                         "open_orders": []})

    monkeypatch.setattr(setup, "creds_from_env", lambda *a, **k: CREDS)

    def run(**kw):
        state["out"] = []
        path = kw.pop("algorithm", str(algo))
        return adopt.run(path, "fake", init=False,
                         out=state["out"].append, **kw)

    state["run"] = run
    state["algo"] = str(algo)
    state["text"] = lambda: "\n".join(state["out"])
    return state


def _ledger(pg) -> int:
    with pg() as s:
        return s.query(persistence.Execution).count()


def _truth(pg) -> str:
    with pg() as s:
        return s.query(persistence.BrokerConnection).one().execution_truth


def _boundary(pg):
    with pg() as s:
        return s.query(persistence.Deployment).one().reconciled_from


# --------------------------------------------------------------- the ladder

def test_the_ladder_climbs_from_off_through_set_mode(pg, owner_id,
                                                    monkeypatch):
    """Two calls, both through `set_mode`, so the no-skip rule is applied
    rather than worked around by writing the column."""
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cl1", user_id=owner_id,
                                            broker="fake",
                                            execution_truth="off"))
        s.commit()
    seen = []
    real = adopt.set_mode
    monkeypatch.setattr(adopt, "set_mode",
                        lambda c, m: seen.append((c, m)) or real(c, m))

    assert adopt.raise_to_enforce("cl1") == ["observe", "enforce"]

    assert seen == [("cl1", "observe"), ("cl1", "enforce")]
    with pg() as s:
        assert s.get(persistence.BrokerConnection,
                     "cl1").execution_truth == "enforce"


def test_the_ladder_climbs_one_step_from_observe(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cl2", user_id=owner_id,
                                            broker="fake",
                                            execution_truth="observe"))
        s.commit()
    assert adopt.raise_to_enforce("cl2") == ["enforce"]


def test_an_enforcing_connection_is_not_stepped_back_down(pg, owner_id):
    """Walking enforce -> observe -> enforce would leave a real account
    momentarily not enforcing for no reason at all."""
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cl3", user_id=owner_id,
                                            broker="fake",
                                            execution_truth="enforce"))
        s.commit()
    seen = []
    import dqengine.live.adopt as mod
    real = mod.set_mode
    try:
        mod.set_mode = lambda c, m: seen.append(m) or real(c, m)
        assert adopt.raise_to_enforce("cl3") == []
    finally:
        mod.set_mode = real
    assert seen == []
    assert _truth(pg) == "enforce"


def test_the_ladder_goes_through_set_mode_and_keeps_its_rule(pg, owner_id):
    """Not a direct write: `off -> enforce` in one assignment is exactly
    what the no-skip rule forbids, and adopt must be subject to it."""
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cl4", user_id=owner_id,
                                            broker="fake",
                                            execution_truth="off"))
        s.commit()
    with pytest.raises(ValueError):
        adopt.set_mode("cl4", "enforce")
    assert _truth(pg) == "off"


# ----------------------------------------------------------- the comparison

def test_the_comparison_marks_a_position_the_strategy_holds_none_of():
    rows = adopt.comparison({"SPY": 74.0}, {}, ["SPY"])
    assert rows[0][:3] == ("SPY", 74.0, 0.0)
    assert rows[0][3].startswith(adopt.MISMATCH)
    assert "refuse this symbol" in rows[0][3]


def test_the_comparison_agrees_when_the_replay_reproduces_the_position():
    rows = adopt.comparison({"SPY": 74.0}, {"SPY": 74.0}, ["SPY"])
    assert rows == [("SPY", 74.0, 74.0, "agrees")]


def test_the_comparison_says_what_the_executor_would_trade():
    rows = adopt.comparison({"SPY": 74.0}, {"SPY": 80.0}, ["SPY"])
    assert "+6" in rows[0][3]


def test_the_comparison_lists_a_holding_outside_the_universe_and_says_so():
    rows = adopt.comparison({"GME": 3.0}, {}, ["SPY"])
    assert rows == [("GME", 3.0, 0.0, "outside the universe — never traded "
                                      "by this strategy")]


def test_the_comparison_skips_symbols_both_sides_are_flat_in():
    assert adopt.comparison({}, {}, [f"S{i}" for i in range(40)]) == []


# ------------------------------------------------------------ the whole run

def test_adopt_backfills_sets_the_boundary_and_enforces(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}

    rc = rig["run"](ask=lambda prompt: "fake (dqengine live)")

    assert rc == 0
    assert _ledger(pg) == 1
    assert _boundary(pg) == date(2026, 1, 5)
    assert _truth(pg) == "enforce"
    text = rig["text"]()
    assert "stored 1 execution(s)" in text
    assert "execution truth: enforce" in text, \
        "a connection setup created starts at observe: one rung"
    assert "dqengine live" in text and "--live" in text


def test_the_screen_shows_both_sides_and_the_history(pg, owner_id, rig):
    rig["vendor"] = Vendor(
        positions={"SPY": 10.0},
        orders=[{"symbol": "SPY", "side": "sell", "qty": 10.0,
                 "type": "limit", "client_order_id": "sl-tp-SPY-1"}],
        fills=[fill()])
    rig["held"] = {"SPY": 10.0}

    rig["run"](ask=lambda prompt: "fake (dqengine live)")

    text = rig["text"]()
    assert "AT BROKER" in text and "STRATEGY" in text
    assert "agrees" in text
    assert "Working orders at the broker: 1" in text
    assert "sl-tp-SPY-1" in text
    assert "Execution history the broker returned: 1 fill(s)" in text
    assert "2026-01-05 15:00  SPY    buy  10 @ 50" in text
    assert "replays from 2026-09-01" in text


def test_a_mismatch_is_marked_and_says_what_happens_next(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 74.0}, fills=[fill()])
    rig["held"] = {}

    rig["run"](ask=lambda prompt: "fake (dqengine live)")

    text = rig["text"]()
    assert adopt.MISMATCH in text
    assert "1 symbol(s) do not agree: SPY" in text
    assert "stops the WHOLE sweep" in text
    assert "Move --start back" in text


# ---------------------------------------------------------- the confirmation

def test_the_wrong_label_writes_nothing(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}

    rc = rig["run"](ask=lambda prompt: "yes")

    assert rc == 1
    assert "that is not the account label" in rig["text"]()
    assert _ledger(pg) == 0, "the ledger is untouched"
    assert _boundary(pg) is None
    assert _truth(pg) == "observe", "a fresh connection, not enforcing"


@pytest.mark.parametrize("typed", ["", "y", "CONFIRM LIVE TRADING",
                                   "fake", "fake (dqengine live) x"])
def test_nothing_but_the_label_gets_past_the_confirmation(pg, owner_id, rig,
                                                          typed):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}
    assert rig["run"](ask=lambda prompt: typed) == 1
    assert _truth(pg) == "observe"


def test_the_label_is_accepted_with_surrounding_whitespace(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}
    assert rig["run"](ask=lambda prompt: "  fake (dqengine live)\n") == 0
    assert _truth(pg) == "enforce"


def test_the_prompt_names_the_account_and_what_enforce_means(pg, owner_id,
                                                            rig):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}
    seen = []
    rig["run"](ask=lambda prompt: seen.append(prompt) or "no")
    assert "fake (dqengine live)" in seen[0]
    assert "enforce" in seen[0] and "real money" in seen[0]


def test_a_non_interactive_run_refuses_rather_than_assuming_yes(pg, owner_id,
                                                                rig):
    """There is no --yes for this command. A cron job that wants the screen
    has --dry-run."""
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}

    rc = rig["run"](ask=lambda prompt: None)

    assert rc == 2
    assert "needs a terminal" in rig["text"]()
    assert "there is no --yes" in rig["text"]()
    assert _ledger(pg) == 0 and _truth(pg) == "observe"


def test_the_default_ask_refuses_when_there_is_no_terminal(monkeypatch):
    class NotATty:
        def isatty(self):
            return False
    monkeypatch.setattr("sys.stdin", NotATty())
    assert adopt._tty_ask("prompt> ") is None


# ---------------------------------------------------------------- the refusals

def test_an_unparsed_row_refuses_before_anything_is_written(pg, owner_id,
                                                            rig):
    """The same rule `apply` has always had, asked before the screen rather
    than after a write: a row nobody could parse would become a permanent
    phantom no-fill inside a range the boundary claims is fully known."""
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()],
                           skipped=2)
    rig["held"] = {"SPY": 10.0}

    rc = rig["run"](ask=lambda prompt: "fake (dqengine live)")

    assert rc == 2
    assert "2 execution row(s)" in rig["text"]()
    assert "Nothing was written" in rig["text"]()
    assert _ledger(pg) == 0 and _boundary(pg) is None
    assert _truth(pg) == "observe"


DAILY = ALGO.replace("Resolution.MINUTE", "Resolution.DAILY")


def test_a_daily_strategy_can_be_adopted_at_all(pg, owner_id, rig,
                                                tmp_path, monkeypatch):
    """A daily deployment needs an enforcing connection, and adopt is the
    command that sets enforce. Asking for enforce before adopt runs would
    make a daily strategy impossible to adopt: the requirement is asked of
    the mode the connection is about to have."""
    from dqengine.adapters.base import Caps
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["vendor"].caps = Caps()
    rig["held"] = {"SPY": 10.0}
    daily = tmp_path / "daily.py"
    daily.write_text(DAILY)

    rc = rig["run"](algorithm=str(daily), ask=lambda p: "fake (dqengine live)")

    assert rc == 0, rig["text"]()
    assert _truth(pg) == "enforce"
    with pg() as s:
        assert s.query(persistence.Deployment).one().resolution == "daily"


def test_a_daily_strategy_the_venue_cannot_carry_is_still_refused(
        pg, owner_id, rig, tmp_path):
    """Only the enforce half is asked of the future. A venue that cannot
    place the orders the strategy uses is refused as it always was."""
    from dqengine.adapters.base import Caps
    rig["vendor"] = Vendor(positions={}, fills=[])
    rig["vendor"].caps = Caps(order_types=frozenset({"limit"}))
    sizing = ('from AlgorithmImports import *\n\n\n'
              'class S(QCAlgorithm):\n'
              '    def initialize(self):\n'
              '        self.set_cash(1000)\n'
              '        self.add_equity("SPY", Resolution.DAILY)\n\n'
              '    def on_data(self, data):\n'
              "        self.set_holdings('SPY', 1.0)\n")
    path = tmp_path / "sizing.py"
    path.write_text(sizing)

    rc = rig["run"](algorithm=str(path), ask=lambda p: "fake (dqengine live)")

    assert rc == 2
    assert "can't place market orders" in rig["text"]()
    with pg() as s:
        assert s.query(persistence.BrokerConnection).count() == 0


def test_dqengine_live_still_asks_a_daily_strategy_for_enforce(pg, owner_id,
                                                               rig,
                                                               tmp_path):
    """The default is unchanged: every caller but adopt asks for the mode
    the connection has now."""
    from dqengine.adapters.base import Caps
    rig["vendor"] = Vendor(positions={})
    rig["vendor"].caps = Caps()
    daily = tmp_path / "d2.py"
    daily.write_text(DAILY)
    with pytest.raises(setup.DeploymentRefused) as e:
        setup.prepare(str(daily), "fake", init=False)
    assert "needs this connection set to `enforce`" in str(e.value)


def test_check_daily_asks_for_enforce_unless_told_otherwise(rig):
    """The parameter's default, pinned where a caller that forgets to pass
    it would land."""
    from dqengine.adapters.base import Caps
    rig["vendor"].caps = Caps()

    class Conn:
        broker = "fake"
        execution_truth = "observe"

    with pytest.raises(setup.DeploymentRefused) as e:
        setup.check_daily(Conn(), DAILY, "fake")
    assert "`enforce`" in str(e.value)
    assert setup.check_daily(Conn(), DAILY, "fake",
                             require_enforce=False) is None


def test_adopting_a_daily_strategy_still_needs_an_adapter_here(
        pg, owner_id, rig, tmp_path, monkeypatch):
    """Only the enforce reason is asked of the future. A connection whose
    venue has no adapter in this install is refused as it always was, which
    is why adopt asks the question against a VIEW of the row rather than
    skipping it."""
    daily = tmp_path / "d3.py"
    daily.write_text(DAILY)
    monkeypatch.setattr(
        "dqengine.adapters.catalog.get_adapter",
        lambda name: (_ for _ in ()).throw(
            LookupError("Fake: no distribution installed")))

    rc = rig["run"](algorithm=str(daily), ask=lambda p: "x")

    assert rc == 2
    assert "no distribution installed" in rig["text"]()
    with pg() as s:
        assert s.query(persistence.BrokerConnection).count() == 0


def test_a_broker_with_no_adapter_here_is_refused_not_traced(pg, owner_id,
                                                             rig,
                                                             monkeypatch):
    """A person is reading this screen. A KeyError out of the adapter
    lookup is a refusal with the reason on one line."""
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}
    monkeypatch.setattr(
        "dqengine.adapters.catalog.get_adapter",
        lambda name: (_ for _ in ()).throw(KeyError(name)))

    rc = rig["run"](ask=lambda p: "fake (dqengine live)")

    assert rc == 2
    assert "has no usable adapter in this install" in rig["text"]()
    assert "dqengine brokers" in rig["text"]()
    assert _ledger(pg) == 0 and _truth(pg) == "observe"


def test_an_unreachable_broker_is_refused_not_traced(pg, owner_id, rig,
                                                     monkeypatch):
    from dqengine.adapters.base import BrokerUnavailable
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}

    def boom(creds, since=None):
        raise BrokerUnavailable("gateway 502")
    rig["vendor"].executions = boom

    rc = rig["run"](ask=lambda p: "fake (dqengine live)")

    assert rc == 2
    assert "the broker could not be read: gateway 502" in rig["text"]()
    assert _ledger(pg) == 0 and _truth(pg) == "observe"


def test_a_strategy_that_cannot_replay_is_refused_not_traced(pg, owner_id,
                                                             rig,
                                                             monkeypatch):
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    monkeypatch.setattr(adopt, "replay_view", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("no data")))

    rc = rig["run"](ask=lambda p: "fake (dqengine live)")

    assert rc == 2
    assert "could not be replayed" in rig["text"]()
    assert _ledger(pg) == 0 and _truth(pg) == "observe"


def test_a_refusal_setup_makes_is_passed_through(pg, owner_id, rig,
                                                 tmp_path):
    """adopt creates its rows through the same `setup.prepare` `dqengine
    live` uses, so it carries the same refusals -- here, an algorithm that
    is not deterministic."""
    bad = tmp_path / "rand.py"
    bad.write_text("import random\n" + ALGO)

    rc = rig["run"](algorithm=str(bad), ask=lambda p: "x")

    assert rc == 2
    assert "isn't deterministic" in rig["text"]()
    with pg() as s:
        assert s.query(persistence.Deployment).count() == 0
        assert s.query(persistence.BrokerConnection).count() == 0


# ------------------------------------------------------------------ dry run

def test_a_dry_run_prints_the_screen_and_writes_nothing(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 74.0}, fills=[fill()])
    rig["held"] = {"SPY": 74.0}
    # the rows have to exist for there to be anything to compare
    rig["run"](ask=lambda prompt: "no")
    before = _truth(pg)

    asked = []
    rc = rig["run"](dry_run=True, ask=lambda p: asked.append(p) or "x")

    assert rc == 0 and asked == [], "nobody was asked to confirm"
    text = rig["text"]()
    assert "AT BROKER" in text
    assert "nothing was written" in text
    assert _ledger(pg) == 0 and _boundary(pg) is None
    assert _truth(pg) == before


def test_a_dry_run_with_no_rows_says_what_to_do(pg, owner_id, rig):
    rig["vendor"] = Vendor(positions={"SPY": 74.0})

    rc = rig["run"](dry_run=True, ask=lambda p: "x")

    assert rc == 2
    assert "has no fake connection yet" in rig["text"]()
    with pg() as s:
        assert s.query(persistence.BrokerConnection).count() == 0


def test_a_dry_run_leaves_the_caps_and_the_mode_alone(pg, owner_id, rig):
    """A real run writes the two rows through `setup.prepare`, so its flags
    own the caps. A dry run touches neither."""
    rig["vendor"] = Vendor(positions={"SPY": 10.0}, fills=[fill()])
    rig["held"] = {"SPY": 10.0}
    rig["run"](max_order_usd=2500, ask=lambda p: "no")
    with pg() as s:
        before = dict(s.query(persistence.BrokerConnection).one().settings)

    rig["run"](dry_run=True, ask=lambda p: "x")

    with pg() as s:
        assert dict(s.query(persistence.BrokerConnection).one().settings) \
            == before
    assert before["max_order_notional"] == 2500.0


# --------------------------------------------------------- the account label

def test_the_label_is_the_connections_own(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cx", user_id=owner_id,
                                            broker="fake",
                                            label="Alpaca — retirement"))
        s.commit()
    assert adopt.account_label("cx") == "Alpaca — retirement"


def test_an_unlabelled_connection_falls_back_to_its_broker(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="cy", user_id=owner_id,
                                            broker="fake"))
        s.commit()
    assert adopt.account_label("cy") == "fake"


# ------------------------------------------------------------ the broker read

def test_the_broker_view_refreshes_the_session_and_keeps_the_newest_fills(
        pg, owner_id, monkeypatch):
    early = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    late = early + timedelta(days=3)
    vendor = Vendor(positions={"spy": 4.0},
                    fills=[fill(exec_id="a", when=early),
                           fill(exec_id="b", when=late)])
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: vendor)
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="cb1", user_id=owner_id, broker="fake",
            creds_encrypted=vault.encrypt_creds(CREDS)))
        s.commit()

    view = adopt.broker_view("cb1", fills=1)

    assert vendor.sessions == 1
    assert view["positions"] == {"SPY": 4.0}, "symbols upper-cased"
    assert [f["broker_exec_id"] for f in view["fills"]] == ["b"]
    assert view["executions_found"] == 2 and view["skipped"] == 0


BUYER = '''"""Buys one share on the second bar it sees, and holds it."""
from AlgorithmImports import *


class Tiny(QCAlgorithm):
    def initialize(self):
        self.set_cash(1000)
        self.sym = self.add_equity("AAA", Resolution.MINUTE).symbol
        self.seen = 0

    def on_data(self, data):
        if not data.contains_key(self.sym):
            return
        self.seen += 1
        if self.seen == 2 and not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 1)
'''


def test_the_replay_view_reports_what_a_tick_would_hold(pg, owner_id,
                                                        monkeypatch,
                                                        tmp_path):
    """The real replay, on the real ports, against real stored bars: the
    strategy buys one AAA and the screen says so. This is the number the
    whole command exists to put next to the broker's, and it comes from the
    same code a tick runs."""
    from dqengine.live import bar_source, run as run_mod
    from dqengine.live.bar_source import SqlBarSource
    from dqengine.live.driver import ports
    from test_live_session import last_session_day
    before = (ports._STORE, ports._BARS, ports._SINK)
    monkeypatch.setattr(bar_source, "SessionLocal", pg)
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path / "pydata"))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: Vendor(positions={"AAA": 1.0}))
    monkeypatch.setattr(setup, "creds_from_env", lambda *a, **k: CREDS)
    day = last_session_day()
    open_ms = 9 * 3600 * 1000 + 30 * 60 * 1000
    with pg() as s:
        s.add(persistence.BarDay(
            symbol="AAA", day=day,
            rows=[[open_ms + i * 60_000, 10.0, 10.0, 10.0, 10.0, 100]
                  for i in range(6)]))
        s.commit()
    path = tmp_path / "buyer.py"
    path.write_text(BUYER)
    rows = setup.prepare(str(path), "fake", init=False, start=day,
                         cash=1000.0)
    run_mod.install_ports(bars=SqlBarSource(history=lambda *a, **k: None,
                                            refresh=lambda sym: 0))
    try:
        view = adopt.replay_view(rows["dep_id"], refresh_bars=False)
    finally:
        ports._STORE, ports._BARS, ports._SINK = before

    assert view["holdings"] == {"AAA": 1.0}
    assert view["universe"] == ["AAA"] and view["start_date"] == str(day)
    assert adopt.comparison({"AAA": 1.0}, view["holdings"],
                            view["universe"]) == [
        ("AAA", 1.0, 1.0, "agrees")]
    with pg() as s:
        dep = s.get(persistence.Deployment, rows["dep_id"])
        assert dep.last_tick is None, "a comparison commits no payload"
        assert not dep.position, "and no payload"


def test_the_broker_view_does_not_write_refreshed_credentials(
        pg, owner_id, monkeypatch):
    """A read-only command must not rotate a stored credential pair; the
    next sweep refreshes from what is stored."""
    vendor = Vendor(positions={})
    vendor.ensure_session = lambda creds: {**creds, "key_id": "rotated"}
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: vendor)
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="cb2", user_id=owner_id, broker="fake",
            creds_encrypted=vault.encrypt_creds(CREDS)))
        s.commit()

    adopt.broker_view("cb2")

    with pg() as s:
        stored = vault.decrypt_creds(
            s.get(persistence.BrokerConnection, "cb2").creds_encrypted)
    assert stored == CREDS
