"""In-process engine mode: the strategy runs INSIDE the caller's process.

What this is NOT: isolation. The strategy's `exec` runs in the API
interpreter with no memory cap, no CPU cap, no network cut, and it can
monkeypatch `sys.modules` -- the OMS, the broker adapters, every OTHER
deployment in the process. It exists for a box whose operator owns every
strategy on it (self-hosted, the open-source engine embedded in someone's
own code). The hosted platform must be structurally unable to enter it:
dqengine.sandbox.pyrunner refuses the mode at import unless
PYRUN_INPROC_ALLOWED=1 AND PYRUNNER_URL is unset, and dispatches to it only
AFTER the remote branch.

Watchdog, and its limits. Each session owns one worker thread that runs
ops from a queue; `call` waits on the op's Future for `timeout_s`. A
pure-Python hang releases the GIL every 5 ms, so the watchdog fires on
time: the session is marked dead (a plain attribute assignment, safe from
any thread), the caller raises, the tick lock is released -- and the
stuck thread burns a core until the process restarts. A hang inside a C
call that HOLDS the GIL (a pathological numpy/pandas op, a user C
extension) freezes every thread including the watchdog until the GIL
returns; `timeout_s` cannot fire. The sandbox is the mitigation; inproc
accepts this. A dead session's registry entry is REPLACED on the next
start (new thread, new engine); the old thread and engine are leaked by
design -- one leaked engine per incident, unbounded over time. Logged at
ERROR with the session id when it happens.

The worker holds NO shared lock while running an op.
"""
from __future__ import annotations

import queue
import sys
import threading
import traceback
from concurrent.futures import Future

from .build import build_engine
from .engine_ops import STOP_OP, EngineOps
from .engine_rpc import envelope_error


def _deliver(fut: Future, res: dict) -> None:
    """A result for a caller that already gave up (watchdog fired, future
    cancelled) is dropped: the session is dead, nobody acts on it."""
    try:
        fut.set_result(res)
    except Exception:                                  # noqa: BLE001 — cancelled
        pass


class InProcSession:
    def __init__(self, session_id: str, code: str, cfg: dict, data_root: str,
                 engine_cls=None):
        self.session_id = session_id
        self.dead: str | None = None
        self._q: "queue.Queue[tuple[str, dict, Future] | None]" = queue.Queue()
        self._built = Future()
        self._thread = threading.Thread(
            target=self._run, args=(code, cfg, data_root, engine_cls),
            name=f"inproc-engine-{session_id}", daemon=True)
        self._thread.start()
        # build on the worker so a build failure surfaces the same way a
        # sandbox build failure does: a raised RuntimeError from start
        build_s = float(cfg.get("build_timeout_s", 60))
        try:
            self.engine = self._built.result(timeout=build_s)
        except TimeoutError:
            self.dead = f"build exceeded {build_s}s in-process"
            print(f"[inproc] ERROR session {session_id}: {self.dead}; the worker "
                  f"thread (and the engine it may finish building) is leaked "
                  f"until restart", file=sys.stderr, flush=True)
            raise RuntimeError(f"engine failed to build: {self.dead}")

    # ------------------------------------------------------------ worker

    def _run(self, code, cfg, data_root, engine_cls):
        try:
            engine = build_engine(cfg, code, data_root, engine_cls=engine_cls)
        except Exception as ex:                        # noqa: BLE001
            self._built.set_exception(
                RuntimeError(f"engine failed to build: {ex!r}"))
            return
        self._built.set_result(engine)
        ops = EngineOps(engine)
        while True:
            item = self._q.get()
            if item is None:
                return
            op, args, fut = item
            if fut.cancelled():
                continue                              # the caller gave up
            try:
                if op == STOP_OP:
                    _deliver(fut, {"ok": True, "result": {}, "dead": engine.dead})
                    return
                result = ops.call(op, args)
                _deliver(fut, {"ok": True, "result": result, "dead": engine.dead})
            except Exception as ex:                    # noqa: BLE001
                _deliver(fut, {"ok": False,
                               "error": {"type": type(ex).__name__,
                                         "message": str(ex),
                                         "traceback": traceback.format_exc()[-3000:]},
                               "dead": engine.dead})

    # ------------------------------------------------------------ driver

    def call(self, op: str, args: dict | None = None, timeout_s: float = 20.0) -> dict:
        """Same contract as pyrunner.session_call: RuntimeError on a dead
        session, an op error (shaped by envelope_error), or a timeout."""
        if self.dead:
            raise RuntimeError(f"engine session is dead: {self.dead}")
        fut: Future = Future()
        self._q.put((op, args or {}, fut))
        try:
            res = fut.result(timeout=timeout_s)
        except TimeoutError:
            fut.cancel()
            self.dead = f"{op} exceeded {timeout_s}s in-process"
            try:
                self.engine.stale(self.dead)
            except Exception:                          # noqa: BLE001
                pass
            print(f"[inproc] ERROR session {self.session_id}: {self.dead}; the "
                  f"worker thread is leaked until restart", file=sys.stderr,
                  flush=True)
            raise RuntimeError(f"engine did not answer {op} within {timeout_s}s "
                               f"(in-process; session dead)")
        if not res["ok"]:
            raise envelope_error(res)
        return res["result"]

    def alive(self) -> bool:
        return self.dead is None and self._thread.is_alive() \
            and not getattr(self.engine, "dead", None)

    def stop(self) -> None:
        """Never an unbounded join: a wedged worker is leaked, not waited on."""
        if self.dead is None:
            fut: Future = Future()
            self._q.put((STOP_OP, {}, fut))
            try:
                fut.result(timeout=2.0)
            except Exception:                          # noqa: BLE001
                pass
        self._q.put(None)
        self.dead = self.dead or "stopped"
        self._thread.join(timeout=2.0)
