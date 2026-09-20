"""The engine's op table, with no transport attached.

Every way of driving a WarmPyEngine -- the file protocol and the unix
socket inside the sandbox (engine_server.py), and the in-process mode for
self-hosted boxes (inproc.py) -- calls THIS and nothing else, so the
refusals that protect the money path exist exactly once:

  * a dead engine refuses everything but ping / stale / stop;
  * advance/tick never accept a ledger (a fresh ledger loses the rows a
    fill already took; new broker truth arrives by rebuild);
  * tick rolls the session only if one is open, and reports what the
    engine holds (`pushed`) so the driver's push bookkeeping mirrors the
    engine rather than guessing.

Ops (args -> result):

    ping         {}                                  -> {"alive", "dead"}
    warm         {through}                           -> {"seconds"}
    advance      {now_ms_et, today, bars?}           -> {"stepped"}
    tick         {now_ms_et, today, roll?, bars?, since?, prices?}
                 -> {"stepped", "rolled", "session_open", "pushed",
                     "snapshot", "primed", "next_fire_ms"}
    end_session  {today}                             -> {}
    snapshot     {since?}                            -> the payload dict
    stale        {reason}                            -> {}

Wall-clock schedule fire (spec 2026-09-18). `prices` is OPTIONAL:
{SYM: {"last": float, "at_ms": epoch_ms}} from whatever realtime feed the
driver has. When present, tick advances on `bars` FIRST and then primes any
scheduled callback the clock has reached that no bar has run, pricing
Security.price / algo._prices from it (LEAN live semantics: events fire on
the clock, Price is the latest tick, Close is the last bar). When absent,
the engine is exactly bar-driven -- the contract a self-hosted driver
without a quote feed relies on. `next_fire_ms` is the next scheduled fire
still ahead (None outside a session) so the driver knows when to wake.
Staleness is the DRIVER's to judge: the engine has no epoch clock.

`stop` is a transport concern (it ends the server loop) and is not here.

One deliberate delta from the pre-refactor `EngineServer._op`: `advance`
now forwards `bars` (the pushed-bars argument `tick` already forwarded),
so the two ops accept the same inputs.
"""
from __future__ import annotations

from datetime import date

STOP_OP = "stop"


class EngineOps:
    def __init__(self, engine):
        self.engine = engine

    def call(self, op: str, args: dict) -> dict:
        e = self.engine
        if op == "ping":
            return {"alive": True, "dead": e.dead if e else "not built"}
        if op == "stale":
            e.stale(str(args.get("reason") or "driver marked stale"))
            return {}
        if e.dead and op not in ("ping", "stale", STOP_OP):
            raise RuntimeError(f"engine dead: {e.dead}")
        if op == "warm":
            return {"seconds": e.warm(through=date.fromisoformat(args["through"]))}
        if op == "advance":
            if "ledger" in args:
                raise ValueError("advance must not carry a ledger: a fresh "
                                 "ledger loses the rows already taken -- "
                                 "set it at build, rebuild on new truth")
            return {"stepped": e.advance(int(args["now_ms_et"]),
                                         date.fromisoformat(args["today"]),
                                         bars=args.get("bars"))}
        if op == "tick":
            # advance (+ optional prime, + optional roll) + snapshot in ONE
            # round trip: the driver's whole per-bar cycle. Same refusals
            # as `advance`. Bars first: if the real bar for a due event is
            # already in the push, the bar fires it and prime finds nothing.
            if "ledger" in args:
                raise ValueError("tick must not carry a ledger: a fresh "
                                 "ledger loses the rows already taken -- "
                                 "set it at build, rebuild on new truth")
            today = date.fromisoformat(args["today"])
            now = int(args["now_ms_et"])
            stepped = e.advance(now, today, bars=args.get("bars"))
            primed = None
            if args.get("prices") is not None:
                primed = e.prime(now, today, args["prices"])
            rolled = False
            if args.get("roll") and e.session_open:
                e.end_session(today)
                rolled = True
            return {"stepped": stepped, "rolled": rolled,
                    "session_open": e.session_open,
                    "pushed": e.pushed_state(),
                    "primed": primed,
                    "next_fire_ms": e.next_fire_ms(),
                    "snapshot": e.snapshot(since=args.get("since"))}
        if op == "end_session":
            e.end_session(date.fromisoformat(args["today"]))
            return {}
        if op == "snapshot":
            return e.snapshot(since=args.get("since"))
        raise ValueError(f"unknown op {op!r}")
