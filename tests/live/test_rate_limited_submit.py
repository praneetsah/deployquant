"""An order the venue refuses for coming too fast (HTTP 429).

A 429 is answered before the request is processed, so the order was not
placed. Before this, the submit left its journal row in `sending`, the
symbol froze, and the row was only abandoned -- and the order re-sent --
ABANDON_S later. At 15:59 that is past the close, and a daily strategy's
at-close order waited for the next morning (2026-10-02, Webull).
"""
import pytest

from dqengine.adapters.base import BrokerUnavailable
from dqengine.live import executor, persistence

WEBULL_429 = "Webull rate-limited us: to many requests"


@pytest.fixture(autouse=True)
def fresh():
    executor._RATE_RETRY.clear()
    yield
    executor._RATE_RETRY.clear()


def _rig(pg, owner_id, monkeypatch, refusals=1, truth="enforce"):
    """One deployment that wants 5 SPY on an account holding none; the venue
    refuses the first `refusals` submits with Webull's 429 message."""
    from rig import FakeBroker, seed
    from dqengine.live import book as _book
    seed(pg, owner_id, conn_id="crl", dep_id="drl", truth=truth)
    with pg() as s:
        d = s.get(persistence.Deployment, "drl")
        d.ir, d.universe, d.live_confirmed = None, ["SPY"], True
        s.commit()

    class Busy(FakeBroker):
        left = refusals

        def submit(self, creds, symbol, qty, side, **kw):
            if Busy.left > 0:
                Busy.left -= 1
                self.log.append(("submit_attempt", symbol))
                raise BrokerUnavailable(WEBULL_429)
            return super().submit(creds, symbol, qty, side, **kw)
    fb = Busy(positions={})
    monkeypatch.setattr(_book, "FAST_PATH", False)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    return fb


def _sweep():
    executor._GATHER_CACHE.pop("crl", None)
    executor._SYNC_GATE.pop("crl", None)
    return executor.sync_broker_account("crl", fast=False)


def _journal(pg):
    with pg() as s:
        return [(r.state, r.client_order_id) for r in
                s.query(persistence.OrderJournal)
                .order_by(persistence.OrderJournal.created_at).all()]


def test_a_rate_limited_order_is_closed_as_not_placed(pg, owner_id,
                                                      monkeypatch):
    monkeypatch.setattr(executor, "_in_close_window", lambda now=None: False)
    fb = _rig(pg, owner_id, monkeypatch)
    _sweep()
    assert [a for a, _ in fb.log] == ["submit_attempt"]
    states = _journal(pg)
    assert [st for st, _ in states] == ["rejected"]      # not "sending"
    with pg() as s:
        rep = s.get(persistence.Deployment, "drl").position["execution"]
        rows = s.query(persistence.BrokerOrder).all()
    assert any("not placed" in e and "rate-limited" in e
               for e in rep["errors"]), rep
    assert [(r.action, r.status) for r in rows] == [("refused",
                                                     "rate_limited")]


def test_the_next_sweep_sends_it_again_without_waiting(pg, owner_id,
                                                       monkeypatch):
    """No 60 s freeze: the journal row is closed, the next sweep computes
    the same delta and sends it under a fresh client id."""
    monkeypatch.setattr(executor, "_in_close_window", lambda now=None: False)
    fb = _rig(pg, owner_id, monkeypatch)
    _sweep()
    _sweep()
    sent = [o for a, o in fb.log if a == "submit"]
    assert len(sent) == 1 and sent[0]["qty"] == 5.0
    states = _journal(pg)
    assert [st for st, _ in states] == ["rejected", "submitted"]
    assert states[0][1] != states[1][1]


def test_inside_the_close_window_a_retry_is_scheduled(pg, owner_id,
                                                      monkeypatch):
    monkeypatch.setattr(executor, "_in_close_window", lambda now=None: True)
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn):
            timers.append((delay, fn))
            self.daemon = False

        def start(self):
            pass
    monkeypatch.setattr(executor._threading, "Timer", FakeTimer)
    fb = _rig(pg, owner_id, monkeypatch)
    _sweep()
    assert [d for d, _ in timers] == [1.5]
    # the retry runs a pass: the order goes out
    executor._GATHER_CACHE.pop("crl", None)
    executor._SYNC_GATE.pop("crl", None)
    timers[0][1]()
    assert [o["qty"] for a, o in fb.log if a == "submit"] == [5.0]


def test_the_retry_ladder_ends(pg, owner_id, monkeypatch):
    monkeypatch.setattr(executor, "_in_close_window", lambda now=None: True)
    delays = []

    class FakeTimer:
        def __init__(self, delay, fn):
            delays.append(delay)
            self.daemon = False

        def start(self):
            pass
    monkeypatch.setattr(executor._threading, "Timer", FakeTimer)
    _rig(pg, owner_id, monkeypatch, refusals=99)
    for _ in range(len(executor.RATE_RETRY_DELAYS_S) + 3):
        _sweep()
    assert delays == list(executor.RATE_RETRY_DELAYS_S)


def test_outside_the_close_window_nothing_is_scheduled(pg, owner_id,
                                                       monkeypatch):
    monkeypatch.setattr(executor, "_in_close_window", lambda now=None: False)
    timers = []
    monkeypatch.setattr(executor._threading, "Timer",
                        lambda d, fn: timers.append(d))
    _rig(pg, owner_id, monkeypatch)
    _sweep()
    assert timers == []


def test_only_a_rate_limit_is_read_as_not_placed():
    assert executor._rate_refused(BrokerUnavailable(WEBULL_429))
    assert executor._rate_refused(BrokerUnavailable("Alpaca 429: slow down"))
    assert not executor._rate_refused(
        BrokerUnavailable("could not reach Webull: timed out"))
    assert not executor._rate_refused(RuntimeError(WEBULL_429))
