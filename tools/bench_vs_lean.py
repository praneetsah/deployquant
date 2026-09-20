"""Head-to-head benchmark: DQengine vs LEAN, same algorithm, same data.

Runs the SAME unmodified QCAlgorithm file two ways and reports wall time,
peak RSS and the end equity each one produced:

  1. LEAN            -- via `lean backtest` on quantconnect/lean:latest
  2. DQengine / dqengine.runtime -- the LEAN-API path, running the file unmodified

With --skip-lean, LEAN is represented by LEAN_REFERENCE below: the published
result of that same container on that same window, committed so the
agreement check still has two numbers to compare. It used to have a second
ENGINE to compare against (the IR engine); that engine was deleted in Phase
3 of the one-engine cleanup, and an agreement check with one participant is
a check that cannot fail.

The end equity is the point of the exercise. A speed number only means
something if they agree to the penny; if they ever diverge, this script
says so and exits non-zero rather than printing a benchmark nobody should
trust.

    python tools/bench_vs_lean.py            # both engines, 3 trials
    python tools/bench_vs_lean.py --trials 5 --skip-lean

LEAN's own reported compute time is parsed out of its log as well, because it
excludes container start, JIT and data mounting -- quoting it is the
conservative comparison, and the one worth publishing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(ENGINE, "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
ALGO = os.path.join(ENGINE, "dqengine", "examples", "tqqq_weekly.py")
LEAN_PROJECT = "dqengine_bench_tqqq_weekly"

WINDOW = {"start": "2021-01-04", "end": "2026-06-09", "cash": 1000.0}

# LEAN's own result for this algorithm, window and data set -- the run of
# 2026-09-19 on this algorithm (dqengine/examples/tqqq_weekly.py), which is the same number
# tests/runtime/golden/acceptance_tqqq_weekly.json pins. This is the external
# oracle, in constant form, so `--skip-lean` compares dqengine.runtime against
# LEAN rather than against nothing.
LEAN_REFERENCE = {
    "source": "LEAN 2026-09-19, dqengine/examples/tqqq_weekly.py",
    "end_equity": 13603.38, "fills": 520, "cagr_pct": 61.698,
}

MiB = 1024.0 * 1024.0


def _peak_rss_mib() -> float:
    """Peak RSS of this process. ru_maxrss is bytes on macOS, KiB on Linux."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / MiB if sys.platform == "darwin" else rss / 1024.0


def _child_peak_rss_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return rss / MiB if sys.platform == "darwin" else rss / 1024.0


def bench_subprocess(label: str, argv: list, parse_equity) -> dict:
    """Run a backtest in a child process so peak RSS is measured in isolation.

    Measuring in-process would attribute the first trial's warm caches to
    every later one; a fresh interpreter per trial is the honest cost of the
    thing a user actually runs.
    """
    before = _child_peak_rss_mib()
    t0 = time.time()
    out = subprocess.run(argv, capture_output=True, text=True)
    wall = time.time() - t0
    peak = max(_child_peak_rss_mib() - before, _child_peak_rss_mib())
    if out.returncode != 0:
        raise RuntimeError(f"{label} failed:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    return {"wall": wall, "peak_mib": peak,
            "equity": parse_equity(out.stdout + out.stderr),
            "log": out.stdout + out.stderr}


def run_dq_python() -> dict:
    """dqengine.runtime path, in-process: the LEAN algorithm file, unmodified."""
    sys.path.insert(0, ENGINE)
    from dqengine.runtime import run_python_backtest

    code = open(ALGO).read()
    t0 = time.time()
    res = run_python_backtest(code, data_root=DATA, overrides=dict(WINDOW))
    if "error" in res:
        raise RuntimeError(res["error"])
    return {"wall": time.time() - t0, "peak_mib": _peak_rss_mib(),
            "equity": res["stats"]["end_equity"], "fills": len(res["fills"])}


def run_lean() -> dict:
    """LEAN via the CLI. Wall time includes container start, JIT and mount --
    that is what a user waits for -- while `compute` is LEAN's own number."""
    # LEAN runs a project directory inside a `lean init` workspace whose
    # data folder holds the same zips: the algorithm is copied in verbatim,
    # so both engines run one file.
    ws = os.environ.get("LEAN_WORKSPACE") or os.path.join(ROOT, "qc")
    if not os.path.isfile(os.path.join(ws, "lean.json")):
        raise RuntimeError(f"{ws} is not a LEAN workspace (no lean.json): run "
                           "`lean init` there, point its data folder at the same "
                           "zips, and set LEAN_WORKSPACE -- or pass --skip-lean")
    proj = os.path.join(ws, LEAN_PROJECT)
    os.makedirs(proj, exist_ok=True)
    shutil.copyfile(ALGO, os.path.join(proj, "main.py"))
    t0 = time.time()
    out = subprocess.run(["lean", "backtest", LEAN_PROJECT],
                         cwd=ws, capture_output=True, text=True)
    wall = time.time() - t0
    log = out.stdout + out.stderr
    if out.returncode != 0:
        raise RuntimeError(f"lean backtest failed:\n{log[-3000:]}")
    eq = re.search(r"End Equity\s+([\d.]+)", log)
    compute = re.search(r"completed in ([\d.]+) seconds", log)
    return {"wall": wall,
            "compute": float(compute.group(1)) if compute else None,
            "equity": float(eq.group(1)) if eq else None}


def summarize(name: str, trials: list, key: str = "wall") -> str:
    vals = [t[key] for t in trials if t.get(key) is not None]
    if not vals:
        return f"{name}: n/a"
    return (f"{name}: mean {sum(vals)/len(vals):.2f}s "
            f"(min {min(vals):.2f}, max {max(vals):.2f})")


def agreement(equities: dict) -> int:
    """0 when at least two independently-produced end equities agree to the
    cent, 1 otherwise. The "at least two" is not pedantry: it is the failure
    this function exists to prevent. A set built from one number always has
    one distinct member, so a one-participant check prints success no matter
    what the engine did."""
    for name, eq in equities.items():
        print(f"  {name}: ${eq:,.2f}")
    if len(equities) < 2:
        print("\nDIVERGENCE -- only one result to compare "
              f"({list(equities)}). Nothing was verified.")
        return 1
    if len({round(e, 2) for e in equities.values()}) > 1:
        print("\nDIVERGENCE -- the engines did not agree. The speed numbers "
              "above are meaningless until this is explained.")
        return 1
    print("\nall engines agree to the penny.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--skip-lean", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "tqqq")):
        print(f"no TQQQ minute data under {DATA} -- nothing to benchmark")
        return 2

    results: dict = {}
    equities: dict = {}

    for label, fn, script in (
            ("DQengine / dqengine.runtime", run_dq_python, "run_dq_python"),):
        trials = []
        for i in range(args.trials):
            # fresh interpreter per trial: no warm caches carried across
            t = bench_subprocess(
                label,
                [sys.executable, os.path.abspath(__file__), "--_one", script],
                lambda s: json.loads(s.strip().splitlines()[-1])["equity"])
            trials.append(t)
            print(f"  {label} trial {i+1}: {t['wall']:.2f}s "
                  f"{t['peak_mib']:.1f} MiB  ${t['equity']:,.2f}")
        results[label] = trials
        equities[label] = trials[0]["equity"]

    # the reference always participates: it is what makes the agreement
    # check below a comparison rather than a tautology
    equities["LEAN (reference)"] = LEAN_REFERENCE["end_equity"]

    if not args.skip_lean:
        trials = []
        for i in range(args.trials):
            t = run_lean()
            trials.append(t)
            print(f"  LEAN trial {i+1}: {t['wall']:.2f}s e2e, "
                  f"{t['compute']:.2f}s compute  ${t['equity']:,.2f}")
        results["LEAN"] = trials
        equities["LEAN"] = trials[0]["equity"]

    print("\n=== summary ===")
    for name, trials in results.items():
        print(" ", summarize(name, trials))
        if name == "LEAN":
            print("   ", summarize("LEAN compute", trials, "compute"))

    print("\n=== agreement ===")
    return agreement(equities)


if __name__ == "__main__":
    # `--_one <fn>`: run a single in-process trial and print JSON. Used by the
    # parent to get a clean interpreter (and clean peak RSS) per trial.
    if "--_one" in sys.argv:
        fn = {"run_dq_python": run_dq_python}[
            sys.argv[sys.argv.index("--_one") + 1]]
        print(json.dumps(fn()))
        raise SystemExit(0)
    raise SystemExit(main())
