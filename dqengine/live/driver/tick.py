"""One tick of one deployment: lock it, route it, write what came back.

The failure posture is the shape of this module. A tick that cannot run
raises into `tick_deployment`'s handler, which records the error and leaves
the last good payload alone; it never writes a flat payload, because a flat
sleeve is how the executor is told to liquidate. A sandbox that is not
answering is not even an error: the row is left untouched for the next tick.
"""
import threading
import traceback

from dqengine.live.driver import engine, ports
from dqengine.live.driver.deployment import (_capped_live_from, _dep_universe,
                                             _python_code_for, _resolution_of)

# ONE TICK PER DEPLOYMENT, not one tick globally.
#
# This was a single global lock wrapping the whole of tick_deployment, so a
# slow tick blocked every other deployment -- and a python tick can take up
# to LIVE_TICK_TIMEOUT_S (25s) when its sandbox is cold. Inside the close
# window that means one deployment's protective-exit sweep waits on an
# unrelated strategy's backtest replay.
#
# The invariant that actually matters is per deployment: a deployment's warm
# engine and its payload must not be advanced twice concurrently. The warm
# registry (engine._WARM_PY) is keyed by deployment id, so
# per-deployment locking preserves exactly what the global lock was
# protecting.
#
# What the global lock ALSO hid: two deployments refreshing the same
# symbol's bar cache at once. That is now guarded per symbol behind the
# bar source's refresh rather than by accident here.
_dep_locks: dict[str, threading.Lock] = {}
_dep_locks_guard = threading.Lock()


def _tick_lock_for(dep_id: str) -> threading.Lock:
    with _dep_locks_guard:
        lk = _dep_locks.get(dep_id)
        if lk is None:
            lk = _dep_locks[dep_id] = threading.Lock()
        return lk


def _tick_python(tx, dep, events: list, refresh_bars: bool,
                 code: str | None = None) -> None:
    """One tick of a python-code deployment. Raises on any failure so the
    caller's handler freezes the row -- never write a flat payload here, it
    is what the executor reconciles against.

    `code` is the source to run: dep.code for a python deployment, or the
    compiled IR for a block deployment on the python engine. Passed rather than
    read off dep so a block deployment's own `code` column (an ejected copy,
    say) can never be mistaken for what it should tick on.

    Commits itself (through the store port) because the caller returns
    straight after; the failure path records the error on the way out
    instead."""
    if refresh_bars:
        for sym in _dep_universe(dep):
            try:
                ports.bars().refresh(sym)
            except Exception as fe:
                print(f"[live] feed refresh failed for {sym}: {fe}",
                      flush=True)
    # Broker truth, exactly as the IR branch builds it. None outside
    # `enforce`. Capped for the same reason: the reconcile sweep runs AFTER
    # the step that signals, so today's absence of a row is UNKNOWN, not a
    # confirmed no-fill — an uncapped ledger would drop the fill as "missed"
    # before the executor ever saw the want.
    tick_ledger = ports.store().ledger(dep)
    if tick_ledger is not None:
        from dqengine.runtime.core.ledger import LiveCappedLedger
        tick_ledger = LiveCappedLedger(tick_ledger,
                                       live_from=_capped_live_from())
    out = None
    if dep.paused_at is None and _resolution_of(dep) != "daily":
        # A daily deployment ticks on the REPLAY path, always. Its session
        # is one bar and a warm engine cannot step a session's lone first
        # bar until a second arrives, so a warm daily engine would hold
        # yesterday's position all day and publish it over a replay that
        # had already acted (a churn in a shape no rail catches). The
        # replay is stateless, so a mid-day restart is a non-event; the
        # engine refuses daily out loud as well.
        #
        # the sub-second path: step a long-lived engine. Any anomaly drops
        # it and serves the replay THIS tick, exactly like the IR branch --
        # a RunnerUnavailable (sandbox service down) propagates unchanged
        # so the caller leaves the row alone rather than erroring it.
        try:
            out = engine.warm_tick_python(dep, events, ledger=tick_ledger,
                                          code=code)
        except engine.RunnerUnavailable:
            raise
        except Exception as we:                        # noqa: BLE001
            engine._drop_engine(dep.id, f"{type(we).__name__}: {we}")
            print(f"[warm-py] falling back to replay dep={dep.id}: {we}",
                  flush=True)
    if out is None:
        out = engine.replay_python_deployment(
            dep, events, ledger=tick_ledger, code=code)
    # I7: MERGE, don't replace. `position["execution"]` is written by the
    # broker sweep and carries the execution-poll error the executor reads
    # to decide a symbol's fill state is UNKNOWN rather than flat. The
    # replay knows nothing about that key, so a plain assignment made a
    # recorded poll error survive exactly one tick -- and any out-of-band
    # tick not followed by a sync (an operator tick, an add-cash) erased it
    # outright. During a broker outage that is a window
    # where unknown flattens to no-fill, which is precisely the
    # duplicate-position failure this feature exists to make unreachable.
    # Replay-owned keys are still overwritten; only keys the replay never
    # emits survive. The merge is the DRIVER's rule and reads the same row
    # instance this tick loaded; the store is handed the merged payload.
    out = {**out, "position": {**(dep.position or {}), **out["position"]}}
    tx.commit_payload(out)


def tick_deployment(dep_id: str, refresh_bars: bool = True) -> None:
    """Tick one deployment against current data: compile (or read) its
    python source and hand it to _tick_python. There is no second engine.

    refresh_bars=False skips the per-symbol REST bar refresh _tick_python
    does — used by the stream path, where the closed bar that triggered
    this tick was already written into the cache by the feed. For a
    58-symbol strategy that refresh is ~17.7s of sequential REST fetching
    for bars we already have. A poll path leaves it on: it is the safety
    net for a dead stream.

    Failure posture, which is the whole point of this function's shape: a
    strategy that cannot compile, or a replay that fails, raises into the
    handler below, which records tick_error and leaves stats/equity/
    position/fills alone. A tick NEVER writes a flat payload — the executor
    reads a flat sleeve as an instruction to liquidate. A RunnerUnavailable
    is the one exception that is not even an error: the sandbox is down,
    so the row is left untouched and unerrored for the next tick."""
    with _tick_lock_for(dep_id):
        with ports.store().open(dep_id) as tx:
            dep = tx.dep
            if dep is None or dep.status == "stopped":
                return
            events = tx.events
            try:
                # Routes or RAISES; never None. A deployment whose IR
                # codegen cannot express fails its tick with the block
                # named, and the handler below keeps its last good payload.
                # It must NOT get an empty position -- that is how the
                # executor is told to sell everything.
                py_code = _python_code_for(dep)
                try:
                    _tick_python(tx, dep, events, refresh_bars, code=py_code)
                except engine.RunnerUnavailable as ru:
                    # Infrastructure, not the strategy. Leave the row
                    # exactly as it is -- last good payload, no scary
                    # error -- and let the next tick try again. Marking
                    # it errored would tell the user their code broke
                    # during what is usually an engine deploy.
                    print(f"[live] python runner unavailable "
                          f"dep={dep_id}: {ru}", flush=True)
                return
            except Exception:
                tx.commit_error(traceback.format_exc()[-2000:])
