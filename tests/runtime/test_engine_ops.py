"""The op table without a transport: the refusals that protect the money
path are asserted here once, and every transport is tested only for
delivering to it."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dqengine.runtime.engine_ops import EngineOps                # noqa: E402
from test_engine_server import _StubEngine                 # noqa: E402


def _ops():
    e = _StubEngine()
    return e, EngineOps(e)


def test_every_op_reaches_the_engine():
    e, ops = _ops()
    assert ops.call("ping", {})["alive"] is True
    assert ops.call("warm", {"through": "2026-01-01"})["seconds"] == 0.01
    assert ops.call("advance", {"now_ms_et": 1, "today": "2026-01-02"})["stepped"] is True
    t = ops.call("tick", {"now_ms_et": 2, "today": "2026-01-02", "roll": True})
    assert t["rolled"] is True and t["session_open"] is False and "snapshot" in t
    assert ops.call("snapshot", {})["stats"]["end_equity"] == 1.0
    assert ops.call("end_session", {"today": "2026-01-02"}) == {}
    assert [c[0] for c in e.calls] == ["warm", "advance", "advance", "end_session",
                                       "snapshot", "snapshot", "end_session"]


def test_advance_and_tick_refuse_a_ledger():
    e, ops = _ops()
    for op in ("advance", "tick"):
        with pytest.raises(ValueError, match="ledger"):
            ops.call(op, {"now_ms_et": 1, "today": "2026-01-02", "ledger": {}})
    assert e.calls == []


def test_tick_rolls_only_when_a_session_is_open():
    e, ops = _ops()
    e.session_open = False
    # advance opens it (stub), so roll happens on the same tick
    assert ops.call("tick", {"now_ms_et": 1, "today": "2026-01-02", "roll": True})["rolled"]
    # now closed: a second roll request must not end_session again
    n = [c[0] for c in e.calls].count("end_session")
    ops.call("tick", {"now_ms_et": 2, "today": "2026-01-02", "roll": True})
    assert [c[0] for c in e.calls].count("end_session") == n + 1  # advance re-opened it (stub)


def test_a_dead_engine_refuses_everything_but_ping_and_stale():
    e, ops = _ops()
    e.dead = "gone"
    assert ops.call("ping", {})["dead"] == "gone"
    assert ops.call("stale", {"reason": "x"}) == {}
    for op, args in (("warm", {"through": "2026-01-01"}),
                     ("advance", {"now_ms_et": 1, "today": "2026-01-02"}),
                     ("tick", {"now_ms_et": 1, "today": "2026-01-02"}),
                     ("snapshot", {}), ("end_session", {"today": "2026-01-02"})):
        with pytest.raises(RuntimeError, match="engine dead"):
            ops.call(op, args)


def test_unknown_op_is_an_error():
    _, ops = _ops()
    with pytest.raises(ValueError, match="unknown op"):
        ops.call("frobnicate", {})


def test_tick_primes_after_advancing_and_reports_the_next_fire():
    e, ops = _ops()
    px = {"TQQQ": {"last": 10.0, "at_ms": 1}}
    t = ops.call("tick", {"now_ms_et": 5, "today": "2026-01-02", "prices": px})
    assert [c[0] for c in e.calls][:2] == ["advance", "prime"], "bars first, then prime"
    assert e.calls[1] == ("prime", 5, __import__("datetime").date(2026, 1, 2), px)
    assert t["primed"] == {"fired": [{"name": "_go", "fire_ms": 5}],
                           "priced": 1, "unpriced": []}
    assert t["next_fire_ms"] == 57_540_000


def test_tick_without_prices_never_primes_and_matches_today():
    e, ops = _ops()
    t = ops.call("tick", {"now_ms_et": 5, "today": "2026-01-02"})
    assert "prime" not in [c[0] for c in e.calls]
    assert t["primed"] is None
    assert t["next_fire_ms"] == 57_540_000
    assert {"stepped", "rolled", "session_open", "pushed", "snapshot"} <= set(t)


def test_advance_op_never_primes():
    e, ops = _ops()
    ops.call("advance", {"now_ms_et": 5, "today": "2026-01-02",
                         "prices": {"TQQQ": {"last": 1.0, "at_ms": 1}}})
    assert [c[0] for c in e.calls] == ["advance"]
