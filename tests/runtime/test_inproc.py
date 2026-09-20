"""In-process mode: same ops, same error shapes as the sandbox transports,
and a watchdog that turns a hung strategy into a dead session instead of
a held tick lock. What it does NOT give -- memory caps, isolation -- is
stated in the module docstring, and the leaked thread is asserted here so
nobody mistakes it for cleanup."""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dqengine.runtime.inproc import InProcSession                              # noqa: E402
from test_engine_server import _StubEngine                               # noqa: E402


def _sess(engine_cls=_StubEngine):
    return InProcSession("s1", "pass", {"start": "2026-01-02", "cash": 1000},
                         data_root="/nowhere", engine_cls=engine_cls)


def test_every_op_round_trips_and_matches_the_op_table():
    s = _sess()
    assert s.call("ping", {})["alive"] is True
    assert s.call("warm", {"through": "2026-01-01"})["seconds"] == 0.01
    t = s.call("tick", {"now_ms_et": 1, "today": "2026-01-02", "roll": True})
    assert t["rolled"] is True and "snapshot" in t
    assert [c[0] for c in s.engine.calls] == ["warm", "advance", "end_session", "snapshot"]
    assert s.alive()
    s.stop()
    assert not s.alive()


def test_errors_carry_the_shared_envelope_shape():
    s = _sess()
    with pytest.raises(RuntimeError, match=r"^ValueError: advance must not carry a ledger"):
        s.call("advance", {"now_ms_et": 1, "today": "2026-01-02", "ledger": {}})
    with pytest.raises(RuntimeError, match=r"\(engine dead: timestamp went backwards\)$"):
        s.call("advance", {"now_ms_et": -1, "today": "2026-01-02"})
    assert not s.alive()
    s.stop()


def test_a_build_failure_raises_from_start():
    class Boom:
        def __init__(self, *a, **k):
            raise ValueError("no such symbol")
    with pytest.raises(RuntimeError, match="engine failed to build: ValueError"):
        _sess(engine_cls=Boom)


class _Hang(_StubEngine):
    def advance(self, now_ms, today, bars=None):
        time.sleep(5)
        return True


def test_a_hung_op_kills_the_session_on_time_and_leaks_the_thread():
    s = _sess(engine_cls=_Hang)
    before = threading.active_count()
    t = time.monotonic()
    with pytest.raises(RuntimeError, match="did not answer advance within 0.3s"):
        s.call("advance", {"now_ms_et": 1, "today": "2026-01-02"}, timeout_s=0.3)
    assert time.monotonic() - t < 0.6
    assert not s.alive() and "exceeded" in s.dead
    with pytest.raises(RuntimeError, match="dead"):
        s.call("ping", {})
    t = time.monotonic()
    s.stop()
    assert time.monotonic() - t < 2.5, "stop must never join without a bound"
    assert s._thread.is_alive(), "the wedged worker is leaked, by design"
    assert threading.active_count() >= before


def test_a_replaced_session_leaves_the_old_thread_alone():
    a = _sess(engine_cls=_Hang)
    with pytest.raises(RuntimeError):
        a.call("advance", {"now_ms_et": 1, "today": "2026-01-02"}, timeout_s=0.2)
    b = _sess()
    assert b.alive() and b.call("ping", {})["alive"]
    assert a._thread is not b._thread and a._thread.is_alive()
    b.stop()


def test_a_hung_build_raises_the_build_shape_on_time():
    class Slow(_StubEngine):
        def __init__(self, *a, **k):
            time.sleep(3)
            super().__init__()
    t = time.monotonic()
    with pytest.raises(RuntimeError, match="engine failed to build: build exceeded 0.3s"):
        InProcSession("s1", "pass", {"start": "2026-01-02", "cash": 1000,
                                     "build_timeout_s": 0.3},
                      data_root="/nowhere", engine_cls=Slow)
    assert time.monotonic() - t < 1.0
