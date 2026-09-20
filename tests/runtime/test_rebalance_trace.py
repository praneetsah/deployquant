"""The engine says what a rebalance was sized from -- equity, the prices it
marked, and the share counts it wanted -- so a one-share difference (rotation
sleeve, 2026-08-31 REW) can be read out of the journal instead of guessed at.

This used to read the IR engine's journal and the python log side by side.
The numbers below are the ones both engines produced on 2026-09-15, checked
fill for fill one last time before the oracle left the suite; they are
asserted here as literals because a six-session synthetic run is small
enough to state outright, which is a stronger record than a digest file."""
import os
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dqengine import codegen                                                # noqa: E402
from dqengine.runtime.core.data import DayBars                               # noqa: E402
from dqengine.runtime import run_python_backtest                       # noqa: E402

OPEN, BAR = 34200000, 60000


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = weekdays(date(2024, 1, 2), 14)


class Store:
    def __init__(self, spec): self.spec = spec
    def minute_days(self, sym): return sorted(self.spec.get(sym, {}))
    def daily_days(self, sym): return []
    def load_minute_day(self, sym, day):
        m = self.spec.get(sym, {}).get(day)
        if not m:
            return None
        ts = np.array(sorted(m), dtype=np.int64)
        c = np.array([m[t] for t in sorted(m)], dtype=np.float64)
        return DayBars(day=day, start_ms=ts, open=c.copy(), high=c * 1.0005,
                       low=c * 0.9995, close=c.copy(), volume=np.full(len(c), 1000.0))


def _spec():
    return {s: {d: {OPEN + BAR * i: px + 0.01 * j for i in range(390)}
                for j, d in enumerate(DAYS)}
            for s, px in (("AAA", 50.0), ("BBB", 20.0))}


IR = {"ir_version": "0.2", "meta": {"name": "trace"}, "params": {},
      "universe": {"static": ["AAA", "BBB"]},
      "rules": [{"id": "daily-rebalance",
                 "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
                 "action": {"type": "set_weights",
                            "weights": {"equal": [{"asset": "AAA"}, {"asset": "BBB"}]}}}]}


# What both engines produced for this spec, checked head to head on
# 2026-09-15: the IR journal's {"AAA": 50.08, "BBB": 20.08} marks and
# {"AAA": 99, "BBB": 249} wanted shares, and these fills.
FILLS = [("2024-01-12", 57540000, "AAA", 99, 50.08),
         ("2024-01-12", 57540000, "BBB", 249, 20.08),
         ("2024-01-15", 57540000, "BBB", -1, 20.09)]


def _run(spec):
    code = codegen.generate_python(IR, margin_max=1.0)
    py = run_python_backtest(code, data_root=".", overrides={
        "start": DAYS[8].isoformat(), "end": DAYS[-1].isoformat(),
        "cash": 10000.0, "store": Store(spec)})
    assert "error" not in py, py.get("error")
    return py


def _fills(py):
    return [(f["day"], f["ms"], f["sym"], f["qty"], f["px"]) for f in py["fills"]]


def test_the_rebalance_log_carries_equity_prices_and_wanted_shares():
    py = _run(_spec())
    line = [ln for ln in py["logs"] if "[rebalance]" in ln][0]
    assert DAYS[8].isoformat() in line
    assert "equity=10000.00" in line
    assert f"AAA={int(5000 / 50.08)}" in line and f"BBB={int(5000 / 20.08)}" in line
    # BOTH marks, not just AAA's: the whole point of the journal is that a
    # per-symbol mark can be read back, and BBB's is the one the thin-symbol
    # case below turns on.
    assert "AAA@50.0800" in line and "BBB@20.0800" in line, line
    assert _fills(py) == FILLS, _fills(py)


def test_a_thin_symbol_is_marked_at_its_latest_completed_bar_at_the_fire():
    """Prod 2026-08-31 (rotation sleeve): REW printed at 15:56 (11.52) and
    15:57 (11.4809) and nothing after. At the 15:59 fire the IR engine
    marked 11.4809; the python engine marked 11.52 -- one bar behind --
    and wanted one share fewer. The python engine inferred each symbol's
    bar span from the gap between its FIRST TWO bars of the day, so a thin
    symbol whose second bar came minutes after its first was treated as
    printing multi-minute bars, and its 15:57 bar 'ended' after the fire.
    A minute bar spans a minute."""
    spec = _spec()
    thin = {}
    for j, d in enumerate(DAYS):
        full = spec["BBB"][d]
        keep = {t: c for t, c in full.items()
                if t == OPEN or t >= OPEN + 3 * BAR}       # 09:30, then 09:33 on
        keep.pop(OPEN + 388 * BAR, None)                  # no 15:58 bar
        keep.pop(OPEN + 389 * BAR, None)                  # no 15:59 bar
        keep[OPEN + 386 * BAR] = 20.0 + 0.01 * j + 0.40   # 15:56 prints high
        keep[OPEN + 387 * BAR] = 20.0 + 0.01 * j           # 15:57 back down
        thin[d] = keep
    spec["BBB"] = thin
    py = _run(spec)
    line = [ln for ln in py["logs"] if "[rebalance]" in ln][0]
    assert "BBB@20.0800" in line, line          # the 15:57 bar, not 15:56's 20.48
    # and the whole run is unmoved by the thinning. Two INDEPENDENT things
    # are being compared here -- the same engine over two different bar
    # feeds -- and before the 2026-09-08 span fix they disagreed: BBB marked
    # 20.48, the rebalance wanted one share fewer, and the live adoption
    # gate refused the sleeve over it.
    assert _fills(py) == _fills(_run(_spec())), _fills(py)
    assert _fills(py) == FILLS, _fills(py)
