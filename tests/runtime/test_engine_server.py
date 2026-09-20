"""The file-RPC engine server, driven in-process with a stub engine.

No docker here: the server loop runs in a thread over a temp workdir and a
stub stands in for WarmPyEngine. What is under test is the PROTOCOL --
sequencing, atomic writes, error surfacing, dead-engine refusal, idle exit
-- because a protocol bug on the money path looks like a stalled tick, and
a stalled tick looks like "no signal", and "no signal" is an instruction to
the executor to do nothing while the market moves.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime import engine_rpc as rpc                 # noqa: E402
from dqengine.runtime import engine_server as srv              # noqa: E402


class _StubEngine:
    def __init__(self, *a, **k):
        self.dead = None
        self.calls = []
        self.ledger = None
        self.session_open = False

    def warm(self, through):
        self.calls.append(("warm", through)); return 0.01

    def advance(self, now_ms, today, bars=None):
        self.calls.append(("advance", now_ms, today))
        if now_ms < 0:
            self.dead = "timestamp went backwards"
            raise RuntimeError(self.dead)
        self.session_open = True
        return True

    def end_session(self, today):
        self.calls.append(("end_session", today))
        self.session_open = False

    def pushed_state(self):
        return {}

    def prime(self, now_ms, today, prices):
        self.calls.append(("prime", now_ms, today, prices))
        if not prices:
            return None
        return {"fired": [{"name": "_go", "fire_ms": now_ms}],
                "priced": len(prices), "unpriced": []}

    def next_fire_ms(self):
        return 57_540_000 if self.session_open else None

    def snapshot(self, since=None):
        self.calls.append(("snapshot",))
        if self.dead:
            raise RuntimeError(f"engine dead: {self.dead}")
        return {"stats": {"end_equity": 1.0}, "position": {"holdings": []},
                "fills": [], "equity_days": [], "equity": [], "logs": []}

    def stale(self, reason):
        self.dead = reason



@pytest.fixture
def server(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("pass")
    (tmp_path / "run.json").write_text('{"start": "2026-01-02", "cash": 1000}')
    monkeypatch.setenv("DQENGINE_SERVE_IDLE_S", "3600")
    s = srv.EngineServer(str(tmp_path), engine_cls=_StubEngine)
    t = threading.Thread(target=s.serve, daemon=True)
    t.start()
    # wait for the server to announce itself
    for _ in range(200):
        st = rpc.read_state(str(tmp_path))
        if st and st.get("alive"):
            break
        time.sleep(0.01)
    yield s, str(tmp_path)
    _call(str(tmp_path), 999, "stop", {})
    t.join(2)


def _call(workdir, seq, op, args, timeout=3.0):
    rpc.write_atomic(rpc.req_path(workdir, seq), {"op": op, "args": args})
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = rpc.read_json(rpc.res_path(workdir, seq))
        if res is not None:
            os.remove(rpc.res_path(workdir, seq))
            return res
        time.sleep(0.005)
    raise TimeoutError(f"no response to {op} #{seq}")


def test_round_trip_and_sequencing(server):
    s, wd = server
    assert _call(wd, 1, "ping", {})["result"]["alive"] is True
    assert _call(wd, 2, "warm", {"through": "2026-01-01"})["ok"]
    r = _call(wd, 3, "advance", {"now_ms_et": 34260000, "today": "2026-01-02"})
    assert r["ok"] and r["result"]["stepped"] is True
    assert s.engine.calls[0][0] == "warm" and s.engine.calls[1][0] == "advance"
    # the request file is consumed once answered: a restarted server cannot
    # replay it
    assert not os.path.exists(rpc.req_path(wd, 3))


def test_an_error_inside_an_op_comes_back_as_an_error_not_a_hang(server):
    s, wd = server
    r = _call(wd, 1, "advance", {"now_ms_et": -1, "today": "2026-01-02"})
    assert r["ok"] is False
    assert "backwards" in r["error"]["message"]
    assert r["dead"] == "timestamp went backwards"


def test_a_dead_engine_refuses_everything_but_ping_stale_stop(server):
    s, wd = server
    _call(wd, 1, "stale", {"reason": "driver said so"})
    r = _call(wd, 2, "snapshot", {})
    assert r["ok"] is False and "dead" in r["error"]["message"]
    assert _call(wd, 3, "ping", {})["result"]["dead"] == "driver said so"


def test_advance_refuses_to_swap_the_ledger(server):
    """A fresh ledger per advance loses `_taken`: a later SELL's take()
    falls through to pool order and returns the earlier BUY's row, booking
    the sell as a buy -- the executor then buys real shares to match a
    position the model invented. Truth arrives via rebuild, never swap."""
    s, wd = server
    r = _call(wd, 1, "advance", {"now_ms_et": 34260000, "today": "2026-01-02",
                                 "ledger": {"fills": []}})
    assert r["ok"] is False and "must not carry a ledger" in r["error"]["message"]


def test_heartbeat_tracks_progress(server):
    s, wd = server
    _call(wd, 7, "ping", {})
    st = rpc.read_state(wd)
    assert st["alive"] is True and st["seq_done"] == 7


def test_a_build_failure_is_written_to_state_not_swallowed(tmp_path):
    class _Boom:
        def __init__(self, *a, **k):
            raise ValueError("no such strategy")
    (tmp_path / "main.py").write_text("pass")
    (tmp_path / "run.json").write_text("{}")
    assert srv.EngineServer(str(tmp_path), engine_cls=_Boom).serve() == 1
    st = rpc.read_state(str(tmp_path))
    assert st["alive"] is False and "build failed" in st["dead"]


def test_tick_is_advance_plus_optional_roll_plus_snapshot(server):
    s, wd = server
    _call(wd, 1, "warm", {"through": "2026-01-01"})
    r = _call(wd, 2, "tick", {"now_ms_et": 34260000, "today": "2026-01-02",
                              "roll": False})
    assert r["ok"] and r["result"]["stepped"] is True
    assert r["result"]["rolled"] is False and "snapshot" in r["result"]
    ops = [c[0] for c in s.engine.calls]
    assert "end_session" not in ops
    r = _call(wd, 3, "tick", {"now_ms_et": 57660000, "today": "2026-01-02",
                              "roll": True})
    assert r["result"]["rolled"] is True and r["result"]["session_open"] is False
    assert [c[0] for c in s.engine.calls][-3:] == ["advance", "end_session", "snapshot"]


def test_tick_refuses_a_ledger_like_advance(server):
    s, wd = server
    _call(wd, 1, "warm", {"through": "2026-01-01"})
    r = _call(wd, 2, "tick", {"now_ms_et": 1, "today": "2026-01-02",
                              "ledger": {"rows": []}})
    assert not r["ok"] and "ledger" in r["error"]["message"]


# ---------------------------------------------------------------- socket

import socket as _socket
import struct as _struct
import tempfile as _tempfile


@pytest.fixture
def sock_server(monkeypatch):
    """A server in a SHORT directory: pytest's tmp_path exceeds the AF_UNIX
    path limit on macOS, which is exactly the bind-failure case the
    file-only tests above cover."""
    d = _tempfile.mkdtemp(prefix="eng-", dir="/tmp")
    with open(os.path.join(d, "main.py"), "w") as fh:
        fh.write("pass")
    with open(os.path.join(d, "run.json"), "w") as fh:
        fh.write('{"start": "2026-01-02", "cash": 1000}')
    monkeypatch.setenv("DQENGINE_SERVE_IDLE_S", "3600")
    s = srv.EngineServer(d, engine_cls=_StubEngine)
    t = threading.Thread(target=s.serve, daemon=True)
    t.start()
    for _ in range(300):
        st = rpc.read_state(d)
        if st and st.get("alive"):
            break
        time.sleep(0.01)
    yield s, d
    try:
        _call(d, 9999, "stop", {}, timeout=1.0)
    except TimeoutError:
        pass                                   # already stopped by the test
    t.join(2)
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _connect(d):
    c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    c.settimeout(1.0)
    c.connect(rpc.sock_path(d))
    return c


def _scall(c, seq, op, args, timeout=3.0):
    dl = time.monotonic() + timeout
    rpc.send_frame(c, {"seq": seq, "op": op, "args": args}, dl)
    return rpc.recv_frame(c, dl)


def test_socket_is_connectable_the_instant_state_says_alive(sock_server):
    s, d = sock_server
    st = rpc.read_state(d)
    assert st["socket"] is True and st.get("socket_error") is None
    c = _connect(d)                                   # no retry, no sleep
    assert _scall(c, 1, "ping", {})["result"]["alive"] is True
    assert (os.stat(rpc.sock_path(d)).st_mode & 0o777) == 0o666


def test_socket_reply_carries_the_seq_and_the_envelope(sock_server):
    s, d = sock_server
    c = _connect(d)
    r = _scall(c, 5, "warm", {"through": "2026-01-01"})
    assert r["seq"] == 5 and r["ok"] is True and r["dead"] is None
    r = _scall(c, 6, "advance", {"now_ms_et": -1, "today": "2026-01-02"})
    assert r["seq"] == 6 and r["ok"] is False and r["dead"]


def test_a_replayed_seq_is_refused_without_executing_and_stales_the_engine(sock_server):
    s, d = sock_server
    c = _connect(d)
    _scall(c, 1, "warm", {"through": "2026-01-01"})
    _scall(c, 2, "advance", {"now_ms_et": 34260000, "today": "2026-01-02"})
    n = len(s.engine.calls)
    r = _scall(c, 2, "advance", {"now_ms_et": 34260000, "today": "2026-01-02"})
    assert r["ok"] is False and r["error"]["type"] == "DuplicateSeq"
    assert len(s.engine.calls) == n, "the op must not have run again"
    assert "duplicate seq" in (s.engine.dead or "")


def test_the_same_seq_by_file_and_socket_executes_once(sock_server):
    s, d = sock_server
    c = _connect(d)
    _scall(c, 1, "warm", {"through": "2026-01-01"})
    r1 = _scall(c, 2, "advance", {"now_ms_et": 34260000, "today": "2026-01-02"})
    r2 = _call(d, 2, "advance", {"now_ms_et": 34260000, "today": "2026-01-02"})
    assert r1["ok"] is True
    assert r2["ok"] is False and r2["error"]["type"] == "DuplicateSeq"
    assert [c_[0] for c_ in s.engine.calls].count("advance") == 1


def test_seq_done_is_one_sequence_across_both_transports(sock_server):
    s, d = sock_server
    c = _connect(d)
    _scall(c, 1, "ping", {})
    _call(d, 2, "ping", {})
    _scall(c, 3, "ping", {})
    assert s.last_seq == 3
    for _ in range(200):                       # the idle heartbeat carries it
        if rpc.read_state(d)["seq_done"] == 3:
            break
        time.sleep(0.01)
    assert rpc.read_state(d)["seq_done"] == 3


def test_a_second_connection_gives_the_first_eof(sock_server):
    s, d = sock_server
    c1 = _connect(d)
    _scall(c1, 1, "ping", {})
    c2 = _connect(d)
    assert _scall(c2, 2, "ping", {})["ok"]
    with pytest.raises(EOFError):
        rpc.recv_frame(c1, time.monotonic() + 1)


def test_a_garbage_frame_closes_the_client_and_the_server_lives(sock_server):
    s, d = sock_server
    c = _connect(d)
    c.send(_struct.pack(">I", 3) + b"xyz")
    with pytest.raises(EOFError):
        rpc.recv_frame(c, time.monotonic() + 2)
    c2 = _connect(d)
    assert _scall(c2, 1, "ping", {})["ok"]


def test_a_stalled_half_frame_does_not_wedge_the_server(sock_server, monkeypatch):
    s, d = sock_server
    monkeypatch.setattr(srv, "SERVER_RECV_S", 0.3)
    c = _connect(d)
    c.send(b"\x00\x00")
    # meanwhile the file path must keep answering
    assert _call(d, 1, "ping", {}, timeout=2.0)["ok"]
    time.sleep(0.5)
    with pytest.raises(EOFError):
        rpc.recv_frame(c, time.monotonic() + 1)
    assert rpc.read_state(d)["alive"] is True


def test_stop_over_the_socket_ends_the_server_and_unlinks(sock_server):
    s, d = sock_server
    c = _connect(d)
    r = _scall(c, 1, "stop", {})
    assert r["ok"] is True
    for _ in range(100):
        if not os.path.exists(rpc.sock_path(d)):
            break
        time.sleep(0.01)
    assert not os.path.exists(rpc.sock_path(d))
    assert rpc.read_state(d)["alive"] is False
