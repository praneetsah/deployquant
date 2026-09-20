"""Sandbox runner integration + hostile-code containment. These launch real
Docker containers; the whole module skips when docker or the pyrunner image
is absent."""
import os
import shutil
import subprocess
import time

import pytest

from dqengine.config import DATA_ROOT as DATA
from dqengine.sandbox import pyrunner


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", pyrunner.PYRUN_IMAGE],
                       capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ready(),
                                reason="docker or pyrunner image not available")

needs_data = pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "tqqq")),
    reason="local TQQQ minute data not present")

GOOD = '''
from AlgorithmImports import *

class My(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 6, 1)
        self.set_end_date(2026, 6, 3)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ", Resolution.MINUTE).symbol
    def on_data(self, data):
        if not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 5, tag="entry")
'''

HOSTILE_INIT = '''
from AlgorithmImports import *
{payload}

class My(QCAlgorithm):
    def initialize(self):
        hostile()
        self.set_start_date(2026, 6, 1)
        self.set_end_date(2026, 6, 3)
        self.set_cash(1000)
        self.add_equity("TQQQ")
'''


def run(code, timeout_s=60, mode="full"):
    return pyrunner.run_sandboxed(
        code, {"mode": mode, "start": "2026-06-01", "end": "2026-06-03",
               "cash": 1000}, data_dir=DATA, timeout_s=timeout_s)


@needs_data
def test_happy_path_backtest():
    res = run(GOOD)
    assert "error" not in res, res.get("error")
    assert res["stats"]["fills"] >= 1


def test_manifest_mode():
    res = run(GOOD, mode="manifest")
    assert res["manifest"]["subscriptions"] == ["TQQQ"]


def test_network_is_unreachable():
    code = HOSTILE_INIT.format(payload='''
def hostile():
    import urllib.request
    urllib.request.urlopen("http://1.1.1.1", timeout=5)
''')
    res = run(code)
    assert "error" in res, "network egress must fail inside the sandbox"


def test_infinite_loop_is_killed():
    code = HOSTILE_INIT.format(payload='''
def hostile():
    while True:
        pass
''')
    t0 = time.time()
    res = run(code, timeout_s=15)
    assert res["error"]["type"] == "Timeout"
    assert time.time() - t0 < 60


def test_memory_balloon_is_contained():
    code = HOSTILE_INIT.format(payload='''
def hostile():
    blocks = []
    while True:
        blocks.append(bytearray(64 * 1024 * 1024))
''')
    res = run(code, timeout_s=90)
    assert "error" in res, "memory balloon must be stopped"


def test_fork_bomb_is_contained():
    code = HOSTILE_INIT.format(payload='''
def hostile():
    import os
    for _ in range(10000):
        os.fork()
''')
    res = run(code, timeout_s=30)
    assert "error" in res


@needs_data
def test_filesystem_is_read_only_outside_work():
    code = HOSTILE_INIT.format(payload='''
def hostile():
    try:
        open("/etc/pwned", "w")
        raise AssertionError("rootfs writable!")
    except OSError:
        pass
    try:
        open("/data/pwned", "w")
        raise AssertionError("data mount writable!")
    except OSError:
        pass
''')
    # hostile() raising nothing means the probes failed as required —
    # the backtest then proceeds and succeeds
    res = run(code)
    assert "error" not in res, res.get("error")


def test_svc_run_delegates_and_auths(monkeypatch):
    # service-level test: no docker needed
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from dqengine.sandbox import service as pyrunner_svc

    monkeypatch.setattr(pyrunner_svc.pyrunner, "run_sandboxed",
                        lambda code, cfg, data_dir, timeout_s=None:
                        {"stats": {"fills": 1}, "echo": cfg["mode"]})
    client = TestClient(pyrunner_svc.app)
    r = client.post("/run", json={"code": "x", "run_cfg": {"mode": "full"}})
    assert r.status_code == 200 and r.json()["echo"] == "full"

    monkeypatch.setenv("PYRUN_SVC_TOKEN", "sekrit")
    assert client.post("/run", json={"code": "x", "run_cfg": {}}).status_code == 401
    r = client.post("/run", json={"code": "x", "run_cfg": {"mode": "full"}},
                    headers={"Authorization": "Bearer sekrit"})
    assert r.status_code == 200
    assert client.get("/health").status_code == 200


def test_run_dispatcher_local_and_http(monkeypatch, tmp_path):
    # local path: delegates to run_sandboxed with the pydata root
    monkeypatch.delenv("PYRUNNER_URL", raising=False)
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path))
    seen = {}

    def fake_sandboxed(code, cfg, data_dir, timeout_s=None):
        seen.update(code=code, data_dir=data_dir, timeout_s=timeout_s)
        return {"ok": 1}

    monkeypatch.setattr(pyrunner, "run_sandboxed", fake_sandboxed)
    assert pyrunner.run("c", {"mode": "full"}, timeout_s=42) == {"ok": 1}
    assert seen["data_dir"] == str(tmp_path) and seen["timeout_s"] == 42

    # http path: unreachable service comes back as a soft error, not a raise
    monkeypatch.setenv("PYRUNNER_URL", "http://127.0.0.1:1")
    res = pyrunner.run("c", {"mode": "full"}, timeout_s=1)
    assert res["error"]["type"] == "RunnerUnavailable"


@needs_data
def test_start_poll_streaming_docker():
    # real container: at least one live snapshot with equity, final result
    # identical in stats to the blocking path
    rid = pyrunner.start_run(GOOD, {"mode": "full", "start": "2026-06-01",
                                    "end": "2026-06-03", "cash": 1000},
                             data_dir=DATA, timeout_s=90)
    saw_progress = False
    for _ in range(180):
        st = pyrunner.poll_run(rid)
        if st["status"] == "done":
            break
        if st.get("progress") and st["progress"].get("equity"):
            saw_progress = True
        time.sleep(0.2)
    assert st["status"] == "done"
    assert "error" not in st["result"], st["result"].get("error")
    assert st["result"]["stats"]["fills"] >= 1
    # a 3-day sim can finish inside one snapshot interval; progress.json is
    # still written at least once (the forced final flush precedes exit)
    assert saw_progress or st["result"]["stats"]["days"] == 3


def test_run_streaming_local_relays_progress(monkeypatch, tmp_path):
    monkeypatch.delenv("PYRUNNER_URL", raising=False)
    monkeypatch.setenv("PYDATA_ROOT", str(tmp_path))

    def fake_execute(name, workdir, data_dir, image, timeout_s, out_name):
        import json as _json
        import os as _os
        with open(_os.path.join(workdir, "progress.json"), "w") as fh:
            _json.dump({"pct": 0.5, "equity": [1, 2], "sim_date": "2026-06-02"}, fh)
        time.sleep(1.6)     # let one poll tick observe the snapshot
        return {"stats": {"fills": 1}, "equity": [1, 2, 3]}

    monkeypatch.setattr(pyrunner, "_execute", fake_execute)
    seen = []
    res = pyrunner.run_streaming(GOOD, {"mode": "full"}, timeout_s=30,
                                 on_progress=seen.append)
    assert res["stats"]["fills"] == 1
    assert seen and seen[0]["pct"] == 0.5


def test_svc_runs_endpoints(monkeypatch):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    from dqengine.sandbox import service as pyrunner_svc

    monkeypatch.setattr(pyrunner_svc.pyrunner, "start_run",
                        lambda code, cfg, data_dir, timeout_s=None: "rid123")
    monkeypatch.setattr(pyrunner_svc.pyrunner, "poll_run",
                        lambda rid: {"status": "running",
                                     "progress": {"pct": 0.3}})
    pyrunner_svc._image_state.update(status="ready", detail="")
    client = TestClient(pyrunner_svc.app)
    r = client.post("/runs", json={"code": "x", "run_cfg": {"mode": "full"}})
    assert r.status_code == 200 and r.json()["run_id"] == "rid123"
    r = client.get("/runs/rid123")
    assert r.json()["progress"]["pct"] == 0.3
    # image gate applies to /runs too
    pyrunner_svc._image_state.update(status="error", detail="boom")
    assert client.post("/runs", json={"code": "x", "run_cfg": {}}).status_code == 503
    pyrunner_svc._image_state.update(status="ready", detail="")


# ------------------------------------------------------------- warm pool

def _drain_pool():
    while True:
        with pyrunner._POOL_LOCK:
            e = pyrunner._POOL.pop() if pyrunner._POOL else None
        if not e:
            break
        subprocess.run(["docker", "kill", e["name"]], capture_output=True)
        shutil.rmtree(e["workdir"], ignore_errors=True)


def _no_cold(*a, **k):
    raise AssertionError("cold path used — warm pool did not serve the run")


@needs_data
def test_warm_pool_serves_a_run(monkeypatch):
    monkeypatch.setenv("PYRUN_WARM_POOL", "1")
    pyrunner.ensure_pool(DATA)
    try:
        assert len(pyrunner._POOL) >= 1
        monkeypatch.setattr(pyrunner, "_execute", _no_cold)
        t0 = time.time()
        res = run(GOOD, timeout_s=90)
        wall = time.time() - t0
        assert "error" not in res, res.get("error")
        assert res["stats"]["fills"] >= 1
        # the pool replenishes behind the run
        deadline = time.time() + 10
        while time.time() < deadline and not pyrunner._POOL:
            time.sleep(0.2)
        assert len(pyrunner._POOL) >= 1
        print(f"warm wall: {wall:.2f}s")
    finally:
        time.sleep(1.0)   # let any replenish thread land its entry first
        _drain_pool()


@needs_data
def test_warm_pool_streaming(monkeypatch):
    monkeypatch.setenv("PYRUN_WARM_POOL", "1")
    pyrunner.ensure_pool(DATA)
    try:
        monkeypatch.setattr(pyrunner, "_execute", _no_cold)
        rid = pyrunner.start_run(
            GOOD, {"mode": "full", "start": "2026-06-01", "end": "2026-06-03",
                   "cash": 1000}, data_dir=DATA, timeout_s=90)
        deadline = time.time() + 90
        res = None
        while time.time() < deadline:
            st = pyrunner.poll_run(rid)
            if st["status"] == "done":
                res = st["result"]
                break
            time.sleep(0.3)
        assert res is not None, "streaming warm run never finished"
        assert "error" not in res, res.get("error")
        assert res["stats"]["fills"] >= 1
    finally:
        time.sleep(1.0)
        _drain_pool()


def test_dead_warm_entry_is_discarded(tmp_path):
    ghost_dir = str(tmp_path / "ghost")
    os.makedirs(ghost_dir)
    with pyrunner._POOL_LOCK:
        pyrunner._POOL.append({"name": "pyrun-warm-ghost",
                               "workdir": ghost_dir, "data_dir": DATA})
    assert pyrunner._acquire_warm(DATA) is None
    assert all(e["name"] != "pyrun-warm-ghost" for e in pyrunner._POOL)
    assert not os.path.exists(ghost_dir)


def test_ensure_pool_replaces_dead_entries(monkeypatch):
    """The 8/30 outage shape: the warm container idled out but its entry
    lingered, so ensure_pool thought the pool was full and never refilled."""
    monkeypatch.setenv("PYRUN_WARM_POOL", "1")
    pyrunner.ensure_pool(DATA)
    try:
        assert len(pyrunner._POOL) == 1
        # kill the container behind the entry (simulates idle-out)
        subprocess.run(["docker", "kill", pyrunner._POOL[0]["name"]],
                       capture_output=True)
        time.sleep(1.0)
        pyrunner.ensure_pool(DATA)
        live = [e for e in pyrunner._POOL if pyrunner._alive(e["name"])]
        assert len(live) == 1, "dead entry not replaced by a live one"
    finally:
        _drain_pool()


def test_svc_gate_self_heals_missing_image(monkeypatch):
    """Image pruned while state says ready -> 503 + a rebuild kicked,
    never a doomed docker run."""
    from dqengine.sandbox import service as svc
    TestClient = pytest.importorskip("fastapi.testclient").TestClient

    monkeypatch.setitem(svc._image_state, "status", "ready")
    monkeypatch.setattr(svc, "_image_exists", lambda: False)
    kicked = []
    monkeypatch.setattr(svc, "_kick_rebuild", lambda: kicked.append(1))
    client = TestClient(svc.app)
    r = client.post("/runs", json={"code": "x", "run_cfg": {}})
    assert r.status_code == 503
    assert kicked, "rebuild was not kicked"
    assert svc._image_state["status"] == "building"


def test_rpc_module_resolves_without_stubs():
    """`_rpc()` is the first thing every real engine-session call runs. It
    once referenced `sys` without importing it; every test stubbed the
    session functions, so prod would have found it first."""
    from dqengine.sandbox import pyrunner as pr
    mod = pr._rpc()
    assert hasattr(mod, "req_path") and hasattr(mod, "read_state")



# ------------------------------------------------------- socket transport

def _fake_socket_session(monkeypatch, tmp_path, transport_env="auto"):
    """A registry entry wired to one end of a socketpair; the other end is
    the 'server' the test drives by hand. No docker."""
    import socket as _s
    import threading as _t
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", transport_env)
    a, b = _s.socketpair()
    entry = {"name": "fake", "workdir": str(tmp_path), "data_dir": "/x", "seq": 0,
             "sock": a, "transport": "socket", "lock": _t.Lock()}
    pr._SESSIONS["sess"] = entry
    stops = []
    monkeypatch.setattr(pr, "session_stop",
                        lambda sid: (stops.append(sid), pr._SESSIONS.pop(sid, None)))
    return pr, entry, b, stops


def test_socket_call_round_trip_checks_seq(monkeypatch, tmp_path):
    import threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()

    def serve_one():
        req = rpc.recv_frame(server, time.monotonic() + 2)
        rpc.send_frame(server, {"seq": req["seq"], "ok": True, "result": {"pong": 1},
                                "dead": None}, time.monotonic() + 2)
    threading.Thread(target=serve_one, daemon=True).start()
    assert pr.session_call("sess", "ping", {}, timeout_s=2) == {"pong": 1}
    assert entry["transport"] == "socket"
    pr._SESSIONS.pop("sess", None)


def test_a_wrong_seq_kills_the_transport_and_never_reads_again(monkeypatch, tmp_path):
    import threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()

    def serve_stale():
        req = rpc.recv_frame(server, time.monotonic() + 2)
        rpc.send_frame(server, {"seq": req["seq"] - 1, "ok": True, "result": {},
                                "dead": None}, time.monotonic() + 2)
        rpc.send_frame(server, {"seq": req["seq"], "ok": True, "result": {},
                                "dead": None}, time.monotonic() + 2)
    threading.Thread(target=serve_stale, daemon=True).start()
    with pytest.raises(RuntimeError, match="socket failed"):
        pr.session_call("sess", "tick", {}, timeout_s=2)
    assert entry["transport"] == "dead" and entry["sock"] is None
    # sticky: the correct frame is still queued, but the session is dead
    with pytest.raises(RuntimeError, match="dead"):
        pr.session_call("sess", "ping", {}, timeout_s=1)
    pr._SESSIONS.pop("sess", None)


def test_an_error_envelope_raises_the_shared_shape(monkeypatch, tmp_path):
    import threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()

    def serve_err():
        req = rpc.recv_frame(server, time.monotonic() + 2)
        rpc.send_frame(server, {"seq": req["seq"], "ok": False,
                                "error": {"type": "ValueError", "message": "nope"},
                                "dead": "x"}, time.monotonic() + 2)
    threading.Thread(target=serve_err, daemon=True).start()
    with pytest.raises(RuntimeError, match=r"^ValueError: nope \(engine dead: x\)$"):
        pr.session_call("sess", "advance", {}, timeout_s=2)
    assert entry["transport"] == "socket", "an op error is not a transport failure"
    pr._SESSIONS.pop("sess", None)


def test_a_timeout_kills_the_transport(monkeypatch, tmp_path):
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    t = time.monotonic()
    with pytest.raises(RuntimeError, match="socket failed"):
        pr.session_call("sess", "ping", {}, timeout_s=0.3)
    assert time.monotonic() - t < 1.0
    assert entry["transport"] == "dead"
    pr._SESSIONS.pop("sess", None)


def test_a_hostile_body_that_breaks_the_parser_kills_the_transport(monkeypatch, tmp_path):
    import struct, threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()

    def serve_deep():
        rpc.recv_frame(server, time.monotonic() + 2)
        body = (b"[" * 200000) + (b"]" * 200000)
        server.sendall(struct.pack(">I", len(body)) + body)
    threading.Thread(target=serve_deep, daemon=True).start()
    with pytest.raises(RuntimeError, match="socket failed"):
        pr.session_call("sess", "ping", {}, timeout_s=3)
    assert entry["transport"] == "dead"
    pr._SESSIONS.pop("sess", None)


def test_concurrent_callers_are_serialized_on_the_stream(monkeypatch, tmp_path):
    """Two threads, one session: the server must see whole frames in
    order, and each caller must get ITS reply."""
    import threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()
    seen = []

    def serve_n(n):
        for _ in range(n):
            req = rpc.recv_frame(server, time.monotonic() + 5)
            seen.append(req["seq"])
            time.sleep(0.02)
            rpc.send_frame(server, {"seq": req["seq"], "ok": True,
                                    "result": {"echo": req["args"]["i"]},
                                    "dead": None}, time.monotonic() + 5)
    threading.Thread(target=serve_n, args=(8,), daemon=True).start()
    results = {}

    def caller(i):
        results[i] = pr.session_call("sess", "ping", {"i": i}, timeout_s=5)["echo"]
    ts = [threading.Thread(target=caller, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(6)
    assert results == {i: i for i in range(8)}
    assert seen == sorted(seen)
    pr._SESSIONS.pop("sess", None)


def test_transport_choice_socket_mode_falls_back_visibly(monkeypatch, tmp_path, capsys):
    """A warm engine on the file path beats no warm engine: in `socket`
    mode a connect failure is an ERROR line plus a counter the health
    endpoints expose -- never a session that fails every tick."""
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", "socket")
    pr.TRANSPORT_FALLBACKS.clear()
    entry = {"workdir": str(tmp_path), "sock": None, "transport": "file"}
    stops = []
    monkeypatch.setattr(pr, "session_stop", lambda sid: stops.append(sid))
    pr._connect_transport("s1", entry, {"alive": True, "socket": True})
    assert stops == [] and entry["transport"] == "file"
    assert "s1" in pr.TRANSPORT_FALLBACKS
    assert "ERROR" in capsys.readouterr().out
    pr._connect_transport("s2", entry, {"alive": True, "socket": False,
                                        "socket_error": "path too long"})
    assert "path too long" in pr.TRANSPORT_FALLBACKS["s2"]
    h = pr.transport_health()
    assert h["configured"] == "socket" and set(h["fallbacks"]) >= {"s1", "s2"}
    pr.TRANSPORT_FALLBACKS.clear()


def test_transport_choice_auto_falls_to_file_and_file_never_connects(monkeypatch, tmp_path):
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", "auto")
    entry = {"workdir": str(tmp_path), "sock": None, "transport": "file"}
    pr._connect_transport("s1", entry, {"alive": True, "socket": True})
    assert entry["transport"] == "file" and entry["sock"] is None
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", "file")
    calls = []
    import socket as _s
    monkeypatch.setattr(_s, "socket", lambda *a, **k: calls.append(1))
    pr._connect_transport("s1", entry, {"alive": True, "socket": True})
    assert calls == []


def test_transport_choice_auto_connects_a_real_socket(monkeypatch):
    import socket as _s, tempfile
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", "auto")
    monkeypatch.setattr(pr, "SANDBOX_UID", os.getuid())
    d = tempfile.mkdtemp(prefix="eng-", dir="/tmp")
    rpc = pr._rpc()
    ls = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM); ls.bind(rpc.sock_path(d)); ls.listen(1)
    entry = {"workdir": d, "sock": None, "transport": "file"}
    pr._connect_transport("s1", entry, {"alive": True, "socket": True})
    assert entry["transport"] == "socket"
    entry["sock"].close(); ls.close()


def test_a_socket_path_replaced_by_user_code_is_refused(monkeypatch):
    """User code owns the socket file's directory inside the container.
    A symlink to a host socket, or a socket owned by the wrong uid, must
    never be connected -- in `socket` mode that is a loud session failure."""
    import socket as _s, tempfile
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "ENGINE_TRANSPORT", "socket")
    stops = []
    monkeypatch.setattr(pr, "session_stop", lambda sid: stops.append(sid))
    rpc = pr._rpc()
    # a real socket elsewhere, symlinked into the workdir
    other = tempfile.mkdtemp(prefix="oth-", dir="/tmp")
    ls = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM); ls.bind(os.path.join(other, "x.sock")); ls.listen(1)
    d = tempfile.mkdtemp(prefix="eng-", dir="/tmp")
    os.symlink(os.path.join(other, "x.sock"), rpc.sock_path(d))
    monkeypatch.setattr(pr, "SANDBOX_UID", os.getuid())
    pr.TRANSPORT_FALLBACKS.clear()
    entry = {"workdir": d, "sock": None, "transport": "file"}
    pr._connect_transport("s1", entry, {"alive": True, "socket": True})
    assert entry["sock"] is None and entry["transport"] == "file"
    assert "not a plain socket file" in pr.TRANSPORT_FALLBACKS["s1"]
    # a genuine socket but the wrong owner (the uid we expect is not ours)
    d2 = tempfile.mkdtemp(prefix="eng-", dir="/tmp")
    ls2 = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM); ls2.bind(rpc.sock_path(d2)); ls2.listen(1)
    monkeypatch.setattr(pr, "SANDBOX_UID", os.getuid() + 1)
    entry = {"workdir": d2, "sock": None, "transport": "file"}
    pr._connect_transport("s2", entry, {"alive": True, "socket": True})
    assert entry["sock"] is None and "owned by uid" in pr.TRANSPORT_FALLBACKS["s2"]
    ls.close(); ls2.close()
    pr.TRANSPORT_FALLBACKS.clear()


def test_a_dead_transport_is_not_alive(monkeypatch, tmp_path):
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_alive", lambda name: True)
    pr._rpc().write_state(str(tmp_path), alive=True, dead=None, seq_done=0)
    assert pr.session_alive("sess") is True
    pr._kill_socket(entry)
    assert pr.session_alive("sess") is False
    pr._SESSIONS.pop("sess", None)


def test_the_seq_is_allocated_under_the_session_lock(monkeypatch, tmp_path):
    """Allocated outside it, a later seq can reach the server first and the
    earlier one is refused as a retry -- a healthy engine killed."""
    import threading
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    rpc = pr._rpc()
    seen = []

    def serve_n(n):
        for _ in range(n):
            req = rpc.recv_frame(server, time.monotonic() + 5)
            seen.append(req["seq"])
            rpc.send_frame(server, {"seq": req["seq"], "ok": True, "result": {},
                                    "dead": None}, time.monotonic() + 5)
    threading.Thread(target=serve_n, args=(40,), daemon=True).start()
    ts = [threading.Thread(target=lambda: pr.session_call("sess", "ping", {}, timeout_s=5))
          for _ in range(40)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(6)
    assert seen == list(range(1, 41))
    pr._SESSIONS.pop("sess", None)


def test_session_stop_closes_the_socket_first(monkeypatch, tmp_path):
    import subprocess as _sp
    from dqengine.sandbox import pyrunner as pr
    real_stop = pr.session_stop
    pr, entry, server, _ = _fake_socket_session(monkeypatch, tmp_path)
    monkeypatch.setattr(_sp, "run", lambda *a, **k: None)
    sk = entry["sock"]
    real_stop("sess")
    assert sk.fileno() == -1 and entry["transport"] == "dead"
    assert "sess" not in pr._SESSIONS


# --------------------------------------------------------- in-process mode

def test_inproc_mode_is_refused_in_the_hosted_topology():
    from dqengine.sandbox import pyrunner as pr
    with pytest.raises(RuntimeError, match="PYRUNNER_URL"):
        pr._check_engine_mode({"PYRUN_ENGINE_MODE": "inproc", "PYRUNNER_URL": "http://svc",
                               "PYRUN_INPROC_ALLOWED": "1"})
    with pytest.raises(RuntimeError, match="PYRUN_INPROC_ALLOWED"):
        pr._check_engine_mode({"PYRUN_ENGINE_MODE": "inproc"})
    with pytest.raises(RuntimeError, match="expected sandbox or inproc"):
        pr._check_engine_mode({"PYRUN_ENGINE_MODE": "yolo"})
    assert pr._check_engine_mode({}) == "sandbox"
    assert pr._check_engine_mode({"PYRUN_ENGINE_MODE": "inproc",
                                  "PYRUN_INPROC_ALLOWED": "1"}) == "inproc"


def test_hosted_dispatch_never_constructs_an_inproc_session(monkeypatch):
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setenv("PYRUNNER_URL", "http://svc")
    monkeypatch.setattr(pr, "ENGINE_MODE", "inproc")      # even if it somehow were
    calls = []
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("inproc reached"))  # noqa: E731
    for fn in ("_inproc_start", "_inproc_call", "_inproc_alive", "_inproc_stop"):
        monkeypatch.setattr(pr, fn, boom)
    monkeypatch.setattr(pr, "_remote", lambda path, *a, **k: (calls.append(path),
                        {"alive": True, "transport": "socket", "result": {}})[1])
    pr.engine_start("d1", "pass", {})
    pr.engine_call("d1", "ping", {})
    pr.engine_alive("d1")
    pr.engine_transport("d1")
    pr.engine_stop("d1")
    assert [c for c in calls] == ["/engines/d1", "/engines/d1/call", "/engines/d1",
                                  "/engines/d1", "/engines/d1"]


def test_inproc_dispatch_runs_the_session_in_process(monkeypatch, tmp_path):
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.delenv("PYRUNNER_URL", raising=False)
    monkeypatch.setattr(pr, "ENGINE_MODE", "inproc")
    monkeypatch.setattr(pr, "_pydata_root", lambda: str(tmp_path))
    from dqengine.runtime import inproc
    from test_engine_server import _StubEngine       # tests/runtime, via this dir's conftest
    orig = inproc.InProcSession.__init__
    monkeypatch.setattr(inproc.InProcSession, "__init__",
                        lambda self, sid, code, cfg, data_root, engine_cls=None:
                        orig(self, sid, code, cfg, data_root, engine_cls=_StubEngine))
    st = pr.engine_start("d1", "pass", {"start": "2026-01-02", "cash": 1000})
    assert st["transport"] == "inproc"
    assert pr.engine_call("d1", "ping", {})["alive"] is True
    assert pr.engine_alive("d1") and pr.engine_transport("d1") == "inproc"
    pr.engine_stop("d1")
    assert not pr.engine_alive("d1")


def test_a_busy_sandbox_answers_runner_unavailable_within_the_budget(monkeypatch, tmp_path):
    """A live replay queued behind PYRUN_MAX_CONCURRENT long runs must come
    back as RunnerUnavailable (row untouched, retry next tick) inside its
    own timeout, not wait past the API's HTTP timeout."""
    import threading
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.setattr(pr, "_pool_size", lambda: 0)
    monkeypatch.setattr(pr, "_prepare_workdir", lambda code, cfg: (str(tmp_path), "result.json"))
    monkeypatch.setattr(pr, "_execute", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pr.shutil, "rmtree", lambda *a, **k: None)
    sem = threading.Semaphore(1)
    monkeypatch.setattr(pr, "_sem", sem)
    assert sem.acquire()                                  # someone else holds it
    t = time.monotonic()
    out = pr.run_sandboxed("pass", {"mode": "full"}, data_dir="/x", timeout_s=0.3)
    assert time.monotonic() - t < 1.0
    assert out["error"]["type"] == "RunnerUnavailable" and "busy" in out["error"]["message"]
    sem.release()
    assert pr.run_sandboxed("pass", {"mode": "full"}, data_dir="/x", timeout_s=0.3) == {"ok": True}


def test_engine_root_resolves_the_svc_image_layout(monkeypatch, tmp_path):
    """dqengine.runtime is found in the distribution this module ships in first;
    failing that, in the sandbox service's baked build context. The first
    prod engine session failed with 'No module named dqengine.runtime' because
    only the repo layout was tried."""
    from dqengine.sandbox import pyrunner as pr
    monkeypatch.delenv("ENGINE_ROOT", raising=False)
    assert pr._engine_root() == pr.DIST_ROOT
    assert os.path.isdir(os.path.join(pr._engine_root(), "dqengine", "runtime"))
    fake = tmp_path / "sandbox-context" / "platform" / "engine" / "dqengine" / "runtime"
    fake.mkdir(parents=True)
    monkeypatch.setattr(pr, "DIST_ROOT", str(tmp_path / "not-a-distribution"))
    monkeypatch.setattr(pr, "SANDBOX_CONTEXT", str(tmp_path / "sandbox-context"))
    assert pr._engine_root() == str(tmp_path / "sandbox-context" / "platform" / "engine")
    monkeypatch.setenv("ENGINE_ROOT", str(tmp_path / "sandbox-context" / "platform" / "engine"))
    assert pr._engine_root().endswith("engine")


def test_rpc_loads_without_numpy_or_the_engine_package():
    """The driver must not pay for the engine: the RPC module loads by
    file, never through dqengine/runtime/__init__ (numpy) -- a process with no
    numpy at all still gets its engine sessions."""
    import subprocess, sys as _sys
    code = (
        "import sys, os\n"
        "sys.modules['numpy'] = None; sys.modules['pandas'] = None\n"
        "from dqengine.sandbox import pyrunner\n"
        "m = pyrunner._rpc()\n"
        "assert hasattr(m, 'recv_frame') and hasattr(m, 'req_path')\n"
        "assert 'dqengine.runtime' not in sys.modules, 'the engine package was imported'\n"
        "print('ok')\n")
    out = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYRUNNER_URL": ""})
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-800:]
