"""In-container entry point for the sandbox runner.

Contract (spec Component 2): the container mounts a per-run workdir at
/work containing main.py (user code) and run.json:

    {"mode": "manifest" | "full",
     "start": "YYYY-MM-DD"?, "end": "YYYY-MM-DD"?, "cash": float?,
     "data_root": "/data"}

manifest mode -> /work/manifest.json     (subscription discovery, no data)
full mode     -> /work/result.json       (the backtest result)

User-code failures are part of the output JSON and exit 0; a nonzero exit
means the runner itself broke.

    python -m dqengine.runtime.sandbox_entry /work
"""
import json
import os
import sys
import traceback


def main(workdir: str) -> int:
    out_name = "result.json"
    try:
        with open(os.path.join(workdir, "run.json")) as fh:
            cfg = json.load(fh)
        mode = cfg.get("mode", "full")
        out_name = "manifest.json" if mode == "manifest" else "result.json"
        with open(os.path.join(workdir, "main.py")) as fh:
            code = fh.read()

        from dqengine.runtime import run_python_backtest
        overrides = {k: cfg[k] for k in ("start", "end", "cash", "project_calendar",
                                         "cash_events", "bar_ms",
                                         # live daily only: the driver's clock
                                         "live_today", "live_now_ms")
                     if cfg.get(k) is not None}
        data_root = cfg.get("data_root", "/data")

        # live-progress channel: atomic snapshots the runner polls while the
        # sim runs (mode "full" only; observation, never load-bearing)
        progress_cb = None
        if mode == "full":
            import time as _time

            prog_path = os.path.join(workdir, "progress.json")

            def progress_cb(snap):
                snap = dict(snap)
                snap["ts"] = _time.time()
                tmp = prog_path + ".tmp"
                with open(tmp, "w") as pf:
                    json.dump(snap, pf)
                os.replace(tmp, prog_path)
        if mode == "full" and cfg.get("resolution") == "second":
            from datetime import date as _date

            from dqengine.runtime.stores import HybridStore
            overrides["store"] = HybridStore(
                data_root,
                second_from=_date.fromisoformat(cfg["second_from"]),
                cache_version=int(cfg["second_cache_version"]))
        # Broker truth crosses the sandbox boundary as plain rows (the
        # container has no DB and no network), and is rebuilt into the same
        # ExecutionLedger the IR engine uses. Absent = backtest.
        if cfg.get("ledger") is not None:
            from datetime import date as _d

            from dqengine.runtime.core.ledger import (ExecutionLedger, LedgerFill,
                                                LiveCappedLedger)
            led = cfg["ledger"]
            _inner = ExecutionLedger(
                [LedgerFill(day=_d.fromisoformat(f["day"]),
                            time_ms=int(f["ms"]), symbol=f["sym"],
                            qty=int(f["qty"]), price=float(f["px"]),
                            fees=float(f.get("fees") or 0.0),
                            rule_tag=f.get("rule_tag"),
                            broker_order_id=f.get("broker_order_id"))
                 for f in led.get("fills") or []],
                unknown=set(led.get("unknown") or []),
                reconciled_from=(_d.fromisoformat(led["reconciled_from"])
                                 if led.get("reconciled_from") else None),
                unknown_from=(_d.fromisoformat(led["unknown_from"])
                              if led.get("unknown_from") else None))
            # The CAP is not decoration. The reconcile sweep runs AFTER the
            # step that signals, so on the live day an absent row means
            # UNKNOWN, not "the broker confirmed nothing filled". Rebuilding
            # a bare ExecutionLedger here dropped that distinction at the
            # process boundary: take() returned [] for every same-day model
            # fill, the book cancelled its own order and applied nothing --
            # so a position was never opened, or never CLOSED.
            overrides["ledger"] = (
                LiveCappedLedger(_inner,
                                 live_from=_d.fromisoformat(led["live_from"]))
                if led.get("live_from") else _inner)

        res = run_python_backtest(code, data_root=data_root,
                                  overrides=overrides,
                                  manifest_only=(mode == "manifest"),
                                  progress_cb=progress_cb)
        with open(os.path.join(workdir, out_name), "w") as fh:
            json.dump(res, fh)
        return 0
    except BaseException:  # noqa: BLE001 — runner failure, not user failure
        try:
            with open(os.path.join(workdir, out_name), "w") as fh:
                json.dump({"error": {"type": "RunnerError",
                                     "message": "sandbox runner failed",
                                     "traceback": traceback.format_exc()}}, fh)
        except OSError:
            pass
        sys.stderr.write(traceback.format_exc())
        return 1


def warm_main(workdir: str) -> int:
    """Warm-pool mode: pay the import bill up front, then wait for the
    runner to drop main.py + run.json into the workdir and execute once.
    Exits after DQENGINE_WARM_IDLE_S (default 30min) of idling so stale warm
    containers don't linger; the runner replenishes the pool."""
    import time

    import numpy  # noqa: F401 — the point is the import cost
    try:
        import pandas  # noqa: F401
    except ImportError:
        pass
    from dqengine.runtime import run_python_backtest  # noqa: F401 — warm the graph

    deadline = time.time() + int(os.environ.get("DQENGINE_WARM_IDLE_S", "1800"))
    run_path = os.path.join(workdir, "run.json")
    main_path = os.path.join(workdir, "main.py")
    while time.time() < deadline:
        # run.json is written LAST by the runner — both present = go
        if os.path.exists(run_path) and os.path.exists(main_path):
            return main(workdir)
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    workdir = args[0] if args else "/work"
    if "--serve" in sys.argv:
        # long-lived engine: build once, step bars on request (engine_server)
        from .engine_server import main as serve_main
        sys.exit(serve_main(workdir, data_root="/data"))
    if "--warm" in sys.argv:
        sys.exit(warm_main(workdir))
    sys.exit(main(workdir))
