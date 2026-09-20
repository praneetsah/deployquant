"""Bars pushed by the driver must land the engine exactly where reading
the zip lands it -- same arrays, same fills -- and a push that does not
reconcile with what the engine holds must kill it, never be absorbed.

The delta snapshot must merge back into the full snapshot byte-for-byte:
the driver builds the executor's payload from the merge.
"""
import os
import sys
from datetime import date

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from conftest_helpers import OPEN_MS, SynthStore, synth_day             # noqa: E402
from dqengine.runtime.core.data import SCALE, day_bars_from_scaled                  # noqa: E402
from dqengine.runtime.warm import WarmPyEngine                                # noqa: E402

CODE = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 21); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
        self.n = 0
        self.schedule.on(self.date_rules.every_day("TQQQ"),
                         self.time_rules.after_market_open("TQQQ", 3), self._go)
    def _go(self): self.set_holdings("TQQQ", 0.5)
    def on_data(self, data):
        self.n += 1
        self.log(f"bar {self.n}")
"""
D = date(2026, 8, 24)
P = date(2026, 8, 21)              # a stored prior day: today is NOT in the store
OV = {"start": "2026-08-21", "end": "2026-08-24", "cash": 1000.0}


def _scaled_rows(b, lo=0, hi=None):
    """Rows as the driver would push them: LEAN-scaled ints, like the zip."""
    hi = b.n if hi is None else hi
    return [[int(b.start_ms[i]), int(round(b.open[i] * SCALE)),
             int(round(b.high[i] * SCALE)), int(round(b.low[i] * SCALE)),
             int(round(b.close[i] * SCALE)), float(b.volume[i])]
            for i in range(lo, hi)]


def _push(b, upto, pushed_ms=-1):
    """The driver's push for a day of `upto` bars, given the engine already
    holds bars through start_ms `pushed_ms`."""
    rows = [r for r in _scaled_rows(b, 0, upto) if r[0] > pushed_ms]
    return {"TQQQ": {"first_ms": int(b.start_ms[0]), "n_total": upto, "rows": rows}}


def _fills(res):
    return [(f["day"], f["sym"], f["qty"], f["px"]) for f in res["fills"]]


def test_pushed_bars_match_the_store_path_exactly():
    full = synth_day(D, list(range(100, 112)))
    # store path: the zip holds the whole day, engine reads it per tick
    ref = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99]), D: full}),
                       overrides=dict(OV))
    ref.warm(through=date(2026, 8, 23))
    for k in (3, 6, 12):
        ref.advance(OPEN_MS + k * 60_000, D)
    ref.end_session(D)

    # push path: the store holds NOTHING for today; the session is seeded
    # from the push
    store = SynthStore({P: synth_day(P, [99, 99, 99])})
    eng = WarmPyEngine(CODE, store=store, overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    pushed = {}
    for k in (3, 6, 12):
        eng.advance(OPEN_MS + k * 60_000, D, bars=_push(full, k, pushed.get("TQQQ", -1)))
        pushed = eng.pushed_state()
    assert pushed == {"TQQQ": int(full.start_ms[11])}
    eng.end_session(D)

    a, b = ref.snapshot(), eng.snapshot()
    assert eng.dead is None
    assert _fills(a) == _fills(b)
    assert a["stats"] == b["stats"] and a["equity"] == b["equity"]
    assert a["logs"] == b["logs"]
    np.testing.assert_array_equal(ref._bt._day_bars["TQQQ"].close,
                                  eng._bt._day_bars["TQQQ"].close)


def test_pushed_bars_are_the_zip_numbers_not_the_raw_ones():
    """A push goes through the same scaled-int path the zip does, so a raw
    price that does not sit on a 1/SCALE grid is quantised identically."""
    b = day_bars_from_scaled(D, [[OPEN_MS, 1234567, 1234567, 1234567, 1234567, 1e7]])
    assert b.close[0] == 1234567 / SCALE


def test_a_push_that_does_not_reconcile_kills_the_engine():
    full = synth_day(D, list(range(100, 112)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99])}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3 * 60_000, D, bars=_push(full, 3))
    pushed = eng.pushed_state()["TQQQ"]
    # the driver pushes 3 more bars but claims the day holds 7: a bar the
    # engine will never see
    bad = _push(full, 6, pushed)
    bad["TQQQ"]["n_total"] = 7
    with pytest.raises(RuntimeError, match="out of sync"):
        eng.advance(OPEN_MS + 6 * 60_000, D, bars=bad)
    assert eng.dead


def test_a_push_with_a_changed_first_bar_kills_the_engine():
    full = synth_day(D, list(range(100, 112)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99])}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3 * 60_000, D, bars=_push(full, 3))
    bad = _push(full, 6, eng.pushed_state()["TQQQ"])
    bad["TQQQ"]["first_ms"] += 60_000
    with pytest.raises(RuntimeError, match="history changed"):
        eng.advance(OPEN_MS + 6 * 60_000, D, bars=bad)


def test_an_empty_push_steps_nothing_and_keeps_the_engine():
    full = synth_day(D, list(range(100, 112)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99])}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3 * 60_000, D, bars=_push(full, 3))
    n = eng.bars_stepped
    assert eng.advance(OPEN_MS + 3 * 60_000, D,
                       bars=_push(full, 3, eng.pushed_state()["TQQQ"])) is False
    assert eng.bars_stepped == n and eng.dead is None


def _merge(full, delta):
    """The driver's merge, duplicated here so the engine-side contract is
    pinned independently of api/live_python.py."""
    out = dict(delta)
    for k in ("fills", "equity_days", "equity", "flows"):
        out[k] = full[k] + delta[k]
    out["orders"] = None
    parts = {k: full["log_parts"][k] + delta["log_parts"][k]
             for k in ("algo", "refused", "missed")}
    out["log_parts"] = parts
    out["logs"] = parts["algo"] + parts["refused"] + parts["missed"]
    return out


def test_delta_snapshot_merges_back_to_the_full_one():
    full_day = synth_day(D, list(range(100, 112)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99]), D: full_day}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 4 * 60_000, D)
    base = eng.snapshot()
    since = {"fills": base["counts"]["fills"],
             "equity": base["counts"]["equity"], "logs": base["counts"]["logs"]}
    eng.advance(OPEN_MS + 12 * 60_000, D)
    eng.end_session(D)
    delta = eng.snapshot(since=since)
    assert delta["logs"] is None and len(delta["log_parts"]["algo"]) == 8
    assert len(delta["equity"]) == 1                     # the day just rolled
    assert delta["orders"] is None, "order statuses mutate; never delta'd"
    merged = _merge(base, delta)
    whole = eng.snapshot()
    whole["orders"] = None
    assert merged == whole


def test_a_since_past_the_end_is_refused():
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99])}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    with pytest.raises(ValueError, match="only"):
        eng.snapshot(since={"fills": 99, "equity": 0, "logs": [0, 0, 0]})


def test_pushed_session_never_consults_the_store():
    """The store holds a DIFFERENT day than the push; the pushed bars must
    be the session's data. (Before: _begin_session read the zip and a
    failed zip write left the engine healthy and never opening.)"""
    full = synth_day(D, list(range(100, 112)))
    other = synth_day(D, list(range(500, 512)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99]), D: other}),
                       overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    eng.advance(OPEN_MS + 3 * 60_000, D, bars=_push(full, 3))
    assert float(eng._bt._day_bars["TQQQ"].close[0]) == 100.0


def test_stats_are_cached_until_the_equity_series_changes():
    """Performance stats are a function of the equity series only; the
    series changes at end_session. Recomputing them per bar was 200 µs on
    a 5-year deployment."""
    from dqengine.runtime.backtester import stats_from_equity
    full_day = synth_day(D, list(range(100, 112)))
    eng = WarmPyEngine(CODE, store=SynthStore({P: synth_day(P, [99, 99, 99]), D: full_day}),
                       overrides=dict(OV))
    eng.warm(through=P)
    eng.advance(OPEN_MS + 4 * 60_000, D)
    a = eng.snapshot()
    bt = eng._bt
    fresh = stats_from_equity(bt._equity_days, bt._equity, bt._flows, bt._cash)
    assert {k: v for k, v in a["stats"].items() if k not in ("fills", "orders")} == fresh
    key_before = bt._stats_cache[0]
    eng.advance(OPEN_MS + 8 * 60_000, D)
    eng.snapshot()
    assert bt._stats_cache[0] == key_before, "mid-session: served from cache"
    eng.end_session(D)
    b = eng.snapshot()
    assert bt._stats_cache[0] != key_before, "the roll added an equity day"
    fresh = stats_from_equity(bt._equity_days, bt._equity, bt._flows, bt._cash)
    assert {k: v for k, v in b["stats"].items() if k not in ("fills", "orders")} == fresh
