"""The long-lived engine, inside the sandbox.

    python -m dqengine.runtime.sandbox_entry --serve /work

Loads /work/main.py and /work/run.json ONCE, builds a WarmPyEngine, then
serves requests over BOTH transports in engine_rpc -- a unix socket at
/work/engine.sock and the file protocol -- until told to stop or until it
has been idle for DQENGINE_SERVE_IDLE_S. The socket is bound, made 0o666 and
listening BEFORE state.json says alive, so a driver that connects the
instant it sees alive never finds it missing. The op table itself lives in
engine_ops.EngineOps. This is what turns a live
python tick from "replay the whole history in a fresh process" (~0.4s
pooled, growing ~0.3s per year of deployment age) into "step the bars that
closed since the last call" -- the sub-second path.

Operations:

    ping         -> {"alive": true, "dead": <reason|None>}
    warm         {through: "YYYY-MM-DD"}            -> {"seconds": float}
    advance      {now_ms_et: int, today: "YYYY-MM-DD",
                  ledger?: <payload>}               -> {"stepped": bool}
    end_session  {today: "YYYY-MM-DD"}              -> {}
    snapshot     {}                                 -> the payload dict
    stale        {reason: str}                      -> {}
    stop         {}                                 -> {} and exit

Posture, inherited from ir_engine/live_engine.py: the engine never guesses.
Any exception inside an op is returned to the driver as an error AND, if
the engine marked itself dead, the response says so. A dead engine refuses
every op except ping/stale/stop -- there is no path from dead to alive
except the driver discarding this container and starting another, which
re-warms from scratch. That is deliberate: dying is cheap, silently
diverging is how a sleeve trades on numbers nobody computed.

The broker ledger is set ONCE, at build (run.json), and never swapped. A
fresh ExecutionLedger per advance would arrive with an empty `_taken` set,
re-exposing rows a fill already consumed: a later SELL's take() finds no
row tagged for it, falls through to pool order, and returns the earlier
BUY's row -- booking the sell as a buy. New broker rows reach the engine
the way they reach the IR warm engine: the driver's stale marker forces a
rebuild.
"""
from __future__ import annotations

import json
import os
import selectors
import socket
import sys
import time
import traceback

from . import engine_rpc as rpc
from .build import build_engine
from .engine_ops import STOP_OP as STOP_OP_SENTINEL, EngineOps


# Request poll interval. 1 ms costs ~1000 directory scans/s on an idle
# engine (negligible) and bounds the server's share of the round trip to
# 1 ms on the file path; the socket path is woken by the selector.
POLL_S = float(os.environ.get("DQENGINE_SERVE_POLL_S", "0.001"))
HEARTBEAT_S = 1.0
# A client that sends a partial frame and stalls must not wedge the single
# server thread (heartbeat, file scan): its frame gets this long.
SERVER_RECV_S = 5.0


class EngineServer:
    def __init__(self, workdir: str, engine_cls=None, data_root: str = "/data"):
        self.workdir = workdir
        # where THIS process reads bars: /data is the sandbox mount. Taken
        # from the caller, never from run.json (build.py's invariant).
        self.data_root = data_root
        # injectable so the PROTOCOL can be tested with a stub engine; the
        # default is the real WarmPyEngine, imported lazily at build time
        self._engine_cls = engine_cls
        self.engine = None
        self.ops = None
        self.idle_s = int(os.environ.get("DQENGINE_SERVE_IDLE_S", str(4 * 3600)))
        self.seq_done = -1
        # the last seq answered on EITHER transport; a seq at or below it
        # is a retry, refused without executing (advance is not something
        # to run twice)
        self.last_seq = 0
        self._listener = None
        self._client = None

    # ------------------------------------------------------------ setup

    def build(self) -> None:
        with open(os.path.join(self.workdir, "run.json")) as fh:
            cfg = json.load(fh)
        with open(os.path.join(self.workdir, "main.py")) as fh:
            code = fh.read()
        self.engine = build_engine(cfg, code, self.data_root,
                                   engine_cls=self._engine_cls)
        self.ops = EngineOps(self.engine)

    def _bind(self) -> None:
        path = rpc.sock_path(self.workdir)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ls.bind(path)
        # the engine runs as an unprivileged uid; the driver may be another
        # uid (a --user'd service, local Linux dev). Connecting needs write
        # permission on the socket file.
        os.chmod(path, 0o666)
        ls.listen(1)
        ls.setblocking(False)
        self._listener = ls

    # -------------------------------------------------------------- ops

    def _dead(self):
        return self.engine.dead if self.engine else "not built"

    def _handle(self, seq, op: str, args: dict) -> tuple[dict, bool]:
        """One request -> (envelope, stop). Applies the seq discipline and
        the stop op; everything else goes to EngineOps."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            return {"seq": seq, "ok": False,
                    "error": {"type": "BadSeq", "message": f"seq {seq!r}"},
                    "dead": self._dead()}, False
        if seq <= self.last_seq:
            reason = (f"duplicate seq {seq} (last answered {self.last_seq}): "
                      f"a caller retried, which the contract forbids")
            if self.engine is not None:
                self.engine.stale(reason)
            return {"seq": seq, "ok": False,
                    "error": {"type": "DuplicateSeq", "message": reason},
                    "dead": self._dead()}, False
        self.last_seq = seq
        self.seq_done = seq
        if op == STOP_OP_SENTINEL:
            return {"seq": seq, "ok": True, "result": {}, "dead": self._dead()}, True
        try:
            result = self.ops.call(op, args)
            return {"seq": seq, "ok": True, "result": result,
                    "dead": self._dead()}, False
        except Exception as ex:                        # noqa: BLE001
            return {"seq": seq, "ok": False,
                    "error": {"type": type(ex).__name__, "message": str(ex),
                              "traceback": traceback.format_exc()[-3000:]},
                    "dead": self._dead()}, False

    # -------------------------------------------------------------- loop

    def serve(self) -> int:
        try:
            return self._serve()
        finally:
            for s_ in (self._client, self._listener):
                if s_ is not None:
                    try:
                        s_.close()
                    except OSError:
                        pass
            try:
                os.unlink(rpc.sock_path(self.workdir))
            except OSError:
                pass

    def _serve(self) -> int:
        # The socket is bound BEFORE alive is announced. A bind that fails
        # (an AF_UNIX path over the OS limit -- macOS pytest tmp dirs; prod's
        # /data/pyrun/work/<id>/engine.sock is short) is not fatal here:
        # the server serves files only and SAYS so in state.json, and a
        # driver configured for the socket then fails the session loudly.
        sock_err = None
        try:
            self._bind()
        except OSError as ex:
            sock_err = repr(ex)
        try:
            self.build()
        except Exception as ex:                       # noqa: BLE001
            rpc.write_state(self.workdir, alive=False,
                            dead=f"build failed: {ex!r}", seq_done=-1,
                            traceback=traceback.format_exc()[-2000:])
            return 1
        rpc.write_state(self.workdir, alive=True, dead=None, seq_done=-1,
                        socket=self._listener is not None, socket_error=sock_err)
        sel = selectors.DefaultSelector()
        if self._listener is not None:
            sel.register(self._listener, selectors.EVENT_READ, "listen")
        last_activity = time.time()
        last_beat = last_activity
        while True:
            if self._listener is None:
                time.sleep(POLL_S)
                events = []
            else:
                events = sel.select(timeout=POLL_S)
            for key, _ in events:
                if key.data == "listen":
                    try:
                        conn, _ = self._listener.accept()
                    except OSError:
                        continue
                    if self._client is not None:
                        # one driver per session. A second connection means
                        # the first is a stale thread: it gets EOF, not
                        # silence. (User code in this container can connect
                        # here too and evict the driver -- it can only DoS
                        # its own deployment: EOF -> kill -> rebuild.)
                        sel.unregister(self._client)
                        self._client.close()
                    conn.setblocking(True)
                    self._client = conn
                    sel.register(conn, selectors.EVENT_READ, "client")
                    continue
                last_activity = time.time()
                try:
                    req = rpc.recv_frame(self._client,
                                         time.monotonic() + SERVER_RECV_S)
                except Exception:                      # noqa: BLE001
                    sel.unregister(self._client)
                    self._client.close()
                    self._client = None
                    continue
                res, stop = self._handle(req.get("seq"), req.get("op"),
                                         req.get("args") or {})
                try:
                    rpc.send_frame(self._client, res,
                                   time.monotonic() + SERVER_RECV_S)
                except Exception:                      # noqa: BLE001
                    sel.unregister(self._client)
                    self._client.close()
                    self._client = None
                # no state.json write per socket request: the reply IS the
                # signal, and a file write + os.replace is ~100 µs on a
                # path that is now ~100 µs. The idle heartbeat carries
                # seq_done/dead within a second.
                if stop:
                    rpc.write_state(self.workdir, alive=False, dead="stopped",
                                    seq_done=self.seq_done)
                    return 0

            pending = rpc.pending_requests(self.workdir)
            if not pending:
                now = time.time()
                if now - last_activity > self.idle_s:
                    rpc.write_state(self.workdir, alive=False, dead="idle timeout",
                                    seq_done=self.seq_done)
                    return 0
                # Idle heartbeat once a second, so the driver can tell a
                # live-but-idle server from a hung one without shelling
                # out to docker on every poll.
                if now - last_beat >= HEARTBEAT_S:
                    rpc.write_state(self.workdir, alive=True, dead=self._dead(),
                                    seq_done=self.seq_done)
                    last_beat = now
                continue
            for seq in pending:
                last_activity = time.time()
                req = rpc.read_json(rpc.req_path(self.workdir, seq))
                if req is None:
                    continue
                res, stop = self._handle(seq, req.get("op"), req.get("args") or {})
                rpc.write_atomic(rpc.res_path(self.workdir, seq), res)
                try:
                    os.remove(rpc.req_path(self.workdir, seq))
                except OSError:
                    pass
                rpc.write_state(self.workdir, alive=not stop,
                                dead="stopped" if stop else self._dead(),
                                seq_done=self.seq_done)
                if stop:
                    return 0


def main(workdir: str, data_root: str = "/data") -> int:
    return EngineServer(workdir, data_root=data_root).serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/work"))
