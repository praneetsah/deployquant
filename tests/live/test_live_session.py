"""One scripted session, end to end, in one process.

A fake venue, a scripted feed and an in-memory bus stand in for the three
things this test may not touch: a broker, a market and Redis. Everything
between them is the real code -- the row creation, the ports, the feed
runner, the driver's tick, the bus consumers and the executor's sweep --
composed the way `dqengine live` composes it.

What it proves: a bar lands in the store, the tick turns it into a payload,
the payload becomes an intent on the bus, the consumer sweeps, and exactly
one order reaches the venue. Then the same session with --dry-run, which
computes the same order and sends nothing.
"""
import os
from datetime import date, timedelta

import pytest

from dqengine.live import executor, persistence, run, setup, status
from dqengine.live import bus as bus_mod
from dqengine.live.bar_source import SqlBarSource
from dqengine.sandbox import pyrunner

from fakes import FakeBus, FakeQuoteFeed, frame, wait_for
from rig import FakeBroker, submitted

SYMBOL = "AAA"

ALGO = '''"""Buys one share on the second bar it sees, and holds it."""
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


def last_session_day(today=None) -> date:
    """The most recent weekday strictly before today that the engine's own
    calendar calls a session."""
    from dqengine.runtime.core.data import is_market_holiday
    d = (today or date.today()) - timedelta(days=1)
    while d.weekday() >= 5 or is_market_holiday(d):
        d -= timedelta(days=1)
    return d


@pytest.fixture()
def rig(pg, owner_id, monkeypatch, tmp_path):
    """Everything a session needs, and nothing that leaves this process."""
    from dqengine.feeds import registry as feed_registry
    from dqengine.live import bar_source
    # the exports bind the session factory at import (as the module they
    # were cut from did), so a test points it at its own database by name
    monkeypatch.setattr(bar_source, "SessionLocal", pg)
    monkeypatch.setattr(pyrunner, "ENGINE_MODE", "inproc")
    # the bar exports must never land in the curated store the backtests read
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path / "pydata"))
    broker = FakeBroker(positions={})
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: broker)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    monkeypatch.setitem(feed_registry.BUILTIN, "fake", "fakes:FakeQuoteFeed")
    day = last_session_day()
    monkeypatch.setattr(FakeQuoteFeed, "SCRIPT", [
        [frame(day, SYMBOL, ms, 10.0)]
        for ms in range(9 * 3600 * 1000 + 30 * 60 * 1000,
                        9 * 3600 * 1000 + 36 * 60 * 1000, 60_000)])
    monkeypatch.setattr(bus_mod, "BUS", None)
    path = tmp_path / "tiny.py"
    path.write_text(ALGO)
    return {"broker": broker, "day": day, "algo": str(path),
            "bars": SqlBarSource(history=lambda *a, **k: None,
                                 refresh=lambda sym: 0)}


def prepare(rig, **kw):
    kw.setdefault("creds", {"k": "v"})
    kw.setdefault("start", rig["day"])
    kw.setdefault("cash", 1000.0)
    return setup.prepare(rig["algo"], "fake", init=False, **kw)


def session_for(rig, rows, bus):
    return run.LiveSession(rows["dep_id"], rows["conn_id"], feed_name="fake",
                           symbols=rows["universe"], bus=bus,
                           bars=rig["bars"], feed_timeout=0.01)


def drive(rig, rows, bus, session):
    """Run the scripted session: wait for the bars, tick once, wait for the
    consumer to reach the venue."""
    stored = wait_for(lambda: _bar_count(rig["day"]) >= 6)
    assert stored, "the feed never wrote its frames into bar_days"
    state = session.worker().run_once()
    assert state == "ticked", "the bar events did not wake a tick"
    return wait_for(lambda: bus.streams.get("intents"))


def _bar_count(day) -> int:
    with persistence.SessionLocal() as s:
        row = s.get(persistence.BarDay, (SYMBOL, day))
        return len(row.rows) if row is not None else 0


# ------------------------------------------------------------------ paper

def test_a_scripted_session_puts_one_order_on_the_venue(rig, pg):
    rows = prepare(rig)
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    try:
        assert drive(rig, rows, bus, session), "no intent was published"
        assert wait_for(lambda: submitted(rig["broker"])), \
            "the consumer never swept, or the sweep sent nothing"
    finally:
        session.stop()
    orders = submitted(rig["broker"])
    assert len(orders) == 1, f"one order, got {orders}"
    assert orders[0]["symbol"] == SYMBOL and orders[0]["side"] == "buy"
    assert orders[0]["qty"] == 1.0
    with pg() as s:
        dep = s.get(persistence.Deployment, rows["dep_id"])
        assert dep.last_tick is not None and dep.tick_error is None
        held = {h["symbol"]: h["qty"] for h in dep.position["holdings"]}
        assert held.get(SYMBOL) == 1, f"the replay did not hold it: {held}"
    assert bus.streams["sync"], "the connection was never told to reconcile"


def test_the_session_stops_every_thread_it_started(rig):
    rows = prepare(rig)
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    started = list(session._threads)
    assert len(started) == 3, "sync consumer, intent consumer, feed"
    session.stop()
    assert not any(t.is_alive() for t in started), "a thread outlived stop()"
    assert session.feed.closed, "the feed socket was left open"


def test_the_bus_is_published_as_the_singleton_before_anything_runs(rig):
    rows = prepare(rig)
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    try:
        assert bus_mod.BUS is bus, \
            "the feed's publish and the warm engine's marker read this name"
    finally:
        session.stop()


# --------------------------------------------------------------- dry run

def test_dry_run_computes_the_order_and_sends_nothing(rig, pg):
    rows = prepare(rig, dry_run=True)
    assert rows["settings"]["dry_run"] is True
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    try:
        assert drive(rig, rows, bus, session), "no intent was published"
        recorded = wait_for(lambda: _dry_rows())
    finally:
        session.stop()
    assert recorded, "the sweep recorded no dry-run order"
    assert recorded[0].symbol == SYMBOL and recorded[0].qty == 1.0
    assert not submitted(rig["broker"]), "dry run reached the venue"


def _dry_rows():
    with persistence.SessionLocal() as s:
        return (s.query(persistence.BrokerOrder)
                .filter(persistence.BrokerOrder.status == "dry_run").all())


# ---------------------------------------------------------------- status

def test_status_is_clean_after_a_session_and_dirty_after_a_tick_error(rig, pg):
    rows = prepare(rig)
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    try:
        drive(rig, rows, bus, session)
        wait_for(lambda: submitted(rig["broker"]))
    finally:
        session.stop()
    rep = status.report(bus=bus, check_lock=False)
    assert status.problems(rep) == [], f"unexpected problems: {rep}"
    assert rep["deployments"][0]["universe"] == [SYMBOL]
    assert rep["feed"]["connected"] is True
    with pg() as s:
        s.get(persistence.Deployment,
              rows["dep_id"]).tick_error = "Traceback\nRuntimeError: boom"
        s.commit()
    bad = status.problems(status.report(bus=bus, check_lock=False))
    assert any("tick error" in line and "boom" in line for line in bad)


def test_status_names_a_determinism_freeze_for_what_it_is(rig, pg):
    rows = prepare(rig)
    with pg() as s:
        s.get(persistence.Deployment, rows["dep_id"]).tick_error = (
            "RuntimeError: this strategy did not reproduce itself: its "
            "settled history changed between ticks.")
        s.commit()
    bad = status.problems(status.report(bus=FakeBus(), check_lock=False))
    assert any("determinism check" in line for line in bad)


def test_status_reports_orders_and_fills_it_can_see(rig, pg):
    rows = prepare(rig)
    bus = FakeBus()
    session = session_for(rig, rows, bus)
    session.start()
    try:
        drive(rig, rows, bus, session)
        wait_for(lambda: submitted(rig["broker"]))
    finally:
        session.stop()
    orders = status.orders(limit=5)
    assert orders and orders[0]["symbol"] == SYMBOL
    assert orders[0]["side"] == "buy" and orders[0]["qty"] == 1.0
    assert status.fills(limit=5) == [], "the fake venue reported no fills"


def test_status_flags_an_abandoned_journal_row(rig, pg):
    rows = prepare(rig)
    with pg() as s:
        s.add(persistence.OrderJournal(
            connection_id=rows["conn_id"], deployment_id=rows["dep_id"],
            client_order_id="cid-1", symbol=SYMBOL, side="buy", qty=1,
            kind="market", state="abandoned"))
        s.commit()
    bad = status.problems(status.report(bus=FakeBus(), check_lock=False))
    assert any("abandoned" in line for line in bad)


def test_status_flags_a_paused_connection(rig, pg):
    rows = prepare(rig)
    with pg() as s:
        c = s.get(persistence.BrokerConnection, rows["conn_id"])
        c.settings = {**c.settings, "paused": True}
        s.commit()
    bad = status.problems(status.report(bus=FakeBus(), check_lock=False))
    assert any("paused" in line for line in bad)


def test_the_sweep_lock_holder_is_visible(rig, pg):
    from rig import OtherProcess
    rows = prepare(rig)
    other = OtherProcess(persistence.SessionLocal.kw["bind"], rows["conn_id"])
    try:
        assert other.take()
        rep = status.report(bus=FakeBus(), check_lock=True)
        assert rep["connections"][0]["sweep_lock_pid"] is not None
        assert any("sweep lock still held" in line
                   for line in status.problems(rep))
    finally:
        other.release()
        other.close()
    rep = status.report(bus=FakeBus(), check_lock=True)
    assert rep["connections"][0]["sweep_lock_pid"] is None


# ---------------------------------------------------------- the refusals

def test_the_session_refuses_to_start_without_a_bus(rig, monkeypatch):
    rows = prepare(rig)
    monkeypatch.delenv("REDIS_URL", raising=False)
    session = run.LiveSession(rows["dep_id"], rows["conn_id"],
                              feed_name="fake", symbols=rows["universe"],
                              bars=rig["bars"])
    with pytest.raises(RuntimeError) as e:
        session.start()
    assert "REDIS_URL" in str(e.value)
    session.stop()


def test_install_ports_wires_the_three_the_driver_asks_for(rig):
    from dqengine.live.driver import ports
    run.install_ports(rig["bars"])
    assert ports.bars() is rig["bars"]
    assert ports.store().__class__.__name__ == "SqlDeploymentStore"
    assert ports.intent_sink(FakeBus()).__class__.__name__ == "BusIntentSink"


def test_the_engine_mode_decides_where_a_one_shot_run_happens(monkeypatch,
                                                              tmp_path):
    """In-process mode must not reach for docker: the sandbox's own entry
    point runs here instead, on the same run.json."""
    monkeypatch.setattr(pyrunner, "ENGINE_MODE", "inproc")
    monkeypatch.delenv("PYRUNNER_URL", raising=False)
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path))
    monkeypatch.setattr(pyrunner, "run_sandboxed", lambda *a, **k:
                        pytest.fail("docker was reached in inproc mode"))
    out = pyrunner.run(ALGO, {"mode": "manifest", "start": "2026-09-01",
                              "end": "2026-09-02", "cash": 1000})
    assert out["manifest"]["subscriptions"] == [SYMBOL]
    assert out["manifest"]["resolution"] == "minute"
