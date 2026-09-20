"""Composer-fidelity engine additions + symphony->IR converter tests.

The reference implementations (_rsi etc.) are copied verbatim from
qc/ComposerWM74_R1/main.py — the deployed-candidate QC port whose semantics
the engine additions must reproduce.
"""
import os
import random
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dqengine.runtime.core.data import DayBars
from dqengine.runtime.core.exprs import NOT_READY
from dqengine.runtime.core.indicators import IndicatorEngine
from tools.composer_import import convert_symphony


# ---------------------------------------------------------------- references
# verbatim from qc/ComposerWM74_R1/main.py

def _rsi_ref(p, n):
    n = int(n)
    if n < 1 or len(p) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = p[i] - p[i - 1]
        gain += d if d > 0 else 0.0
        loss += -d if d < 0 else 0.0
    ag, al = gain / n, loss / n
    for i in range(n + 1, len(p)):
        d = p[i] - p[i - 1]
        ag = (ag * (n - 1) + (d if d > 0 else 0.0)) / n
        al = (al * (n - 1) + (-d if d < 0 else 0.0)) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _maxdd_ref(p, n):
    n = int(n)
    w = p[-(n + 1):] if len(p) >= n + 1 else p
    if len(w) < 2:
        return None
    peak, mdd = w[0], 0.0
    for x in w:
        peak = max(peak, x)
        if peak > 0:
            mdd = max(mdd, (peak - x) / peak)
    return mdd * 100.0


def walk_series(n, seed=7, start=100.0):
    rng = random.Random(seed)
    p, out = start, []
    for _ in range(n):
        p *= 1.0 + rng.uniform(-0.03, 0.031)
        out.append(round(p, 4))
    return out


def make_ind(closes, last=None, sym="SPY"):
    ind = IndicatorEngine()
    for c in closes:
        ind.on_session_close(sym, c)
    ind.prior_close[sym] = closes[-1] if closes else None
    if last is not None:
        ind.last_price[sym] = last
    return ind


# ---------------------------------------------------------------- indicators

def test_wilder_rsi_matches_qc_reference():
    closes = walk_series(120)
    last = 101.5
    ind = make_ind(closes, last)
    node = {"ind": "rsi", "window": 10, "smoothing": "wilder",
            "include_today": True}
    got = ind.value(node, "SPY", {})
    want = _rsi_ref(closes + [last], 10)
    assert abs(got - want) < 1e-12


def test_wilder_rsi_not_ready_below_window_plus_one():
    closes = walk_series(9)          # 9 closes + today = 10 < 10+1
    ind = make_ind(closes, 100.0)
    node = {"ind": "rsi", "window": 10, "smoothing": "wilder",
            "include_today": True}
    assert ind.value(node, "SPY", {}) is NOT_READY


def test_default_rsi_unchanged_by_new_args():
    closes = walk_series(40)
    ind = make_ind(closes, 999.0)    # wild last price must NOT leak in
    plain = ind.value({"ind": "rsi", "window": 14}, "SPY", {})
    ind2 = make_ind(closes)          # no last price at all
    assert plain == ind2.value({"ind": "rsi", "window": 14}, "SPY", {})


def test_include_today_sma():
    closes = [10.0, 11.0, 12.0]
    ind = make_ind(closes, 13.0)
    got = ind.value({"ind": "sma", "window": 2, "include_today": True},
                    "SPY", {})
    assert abs(got - 12.5) < 1e-12   # (12 + 13) / 2


def test_include_today_without_last_price_falls_back():
    closes = [10.0, 11.0, 12.0]
    ind = make_ind(closes)           # no last_price
    got = ind.value({"ind": "sma", "window": 2, "include_today": True},
                    "SPY", {})
    assert abs(got - 11.5) < 1e-12   # (11 + 12) / 2


def test_include_today_not_cached():
    closes = [10.0, 11.0, 12.0]
    ind = make_ind(closes, 13.0)
    node = {"ind": "sma", "window": 2, "include_today": True}
    assert abs(ind.value(node, "SPY", {}) - 12.5) < 1e-12
    ind.last_price["SPY"] = 14.0     # same day, later price
    assert abs(ind.value(node, "SPY", {}) - 13.0) < 1e-12


def test_max_drawdown_mapping_matches_qc_reference():
    """Converter maps max-drawdown(w) -> mul(drawdown(w+1, today), -100)."""
    closes = walk_series(60, seed=3)
    last = closes[-1] * 0.97
    ind = make_ind(closes, last)
    w = 5
    got = ind.value({"ind": "drawdown", "window": w + 1,
                     "include_today": True}, "SPY", {})
    want = _maxdd_ref(closes + [last], w)
    assert abs(got * -100.0 - want) < 1e-9


# ------------------------------------------------------------- inverse_vol

class FakeStore:
    def __init__(self, series):
        self.series = series

    def minute_days(self, sym):
        return sorted(self.series.get(sym, {}))

    def load_minute_day(self, sym, day):
        closes = self.series.get(sym, {}).get(day)
        if closes is None:
            return None
        n = len(closes)
        a = np.array(closes, dtype=np.float64)
        return DayBars(
            day=day,
            start_ms=np.array([34200000 + 60000 * i for i in range(n)],
                              dtype=np.int64),
            open=a.copy(), high=a.copy(), low=a.copy(), close=a.copy(),
            volume=np.full(n, 1000.0))


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def run_weights(weights, series, days, universe):
    """The same weight tree, run on the one engine there is.

    Until Phase 3 this built a Backtester directly. It now compiles the IR
    the way the platform does and runs the generated python against the same
    FakeStore -- run_python_backtest takes a `store` override for exactly
    this, which is how the golden parity harness drives its synthetic cases.
    The weight arithmetic under test is unchanged: WeightEngine is shared
    code that moved to dqengine/runtime/core, it did not belong to the IR engine.

    Returns the result dict. `res["fills"]` are dicts keyed `sym`/`qty`/
    `day`/`px`, not the IR engine's fill objects.
    """
    from dqengine import codegen
    from dqengine.runtime import run_python_backtest
    ir = {"ir_version": "0.2", "meta": {"name": "t"}, "params": {},
          "universe": {"static": universe},
          "rules": [{"id": "rb",
                     "trigger": {"type": "session_open", "days": "all"},
                     "action": {"type": "set_weights", "weights": weights}}]}
    code = codegen.generate_python(ir, margin_max=1.0)
    res = run_python_backtest(code, data_root=None, overrides={
        "start": days[0].isoformat(), "end": days[-1].isoformat(),
        "cash": 10_000.0, "store": FakeStore(series)})
    assert "error" not in res, res.get("error")
    return res


def test_inverse_vol_strategy_children_use_shadow_vol():
    """Group (non-asset) children must accumulate shadow returns so the
    inverse-vol split engages instead of falling back to equal forever."""
    days = weekdays(date(2024, 1, 1), 30)
    rng = random.Random(1)
    series = {"AAA": {}, "BBB": {}}
    pa = pb = 100.0
    for d in days:
        pa *= 1.0 + rng.uniform(-0.001, 0.001)    # calm
        pb *= 1.0 + rng.uniform(-0.04, 0.04)      # wild
        series["AAA"][d] = [pa] * 4
        series["BBB"][d] = [pb] * 4
    weights = {"inverse_vol": {"window": 10, "of": [
        {"equal": [{"asset": "AAA"}]},            # strategy-shaped children
        {"equal": [{"asset": "BBB"}]},
    ]}}
    res = run_weights(weights, series, days, ["AAA", "BBB"])
    # position value split must favour the calm child heavily by the end
    qty = {}
    for f in res["fills"]:
        qty[f["sym"]] = qty.get(f["sym"], 0) + f["qty"]
    va = qty.get("AAA", 0) * series["AAA"][days[-1]][-1]
    vb = qty.get("BBB", 0) * series["BBB"][days[-1]][-1]
    assert va > vb * 2, (va, vb)


def test_inverse_vol_any_child_not_ready_equal_blend():
    """Composer semantics: any child without a readable vol -> equal blend."""
    days = weekdays(date(2024, 1, 1), 6)
    series = {"AAA": {d: [100.0] * 4 for d in days},
              "BBB": {d: [50.0] * 4 for d in days}}
    weights = {"inverse_vol": {"window": 10, "of": [
        {"asset": "AAA"}, {"asset": "BBB"},
    ]}}
    res = run_weights(weights, series, days, ["AAA", "BBB"])
    qty = {}
    for f in res["fills"]:
        qty[f["sym"]] = qty.get(f["sym"], 0) + f["qty"]
    va = qty.get("AAA", 0) * 100.0
    vb = qty.get("BBB", 0) * 50.0
    assert va > 0 and vb > 0
    assert abs(va - vb) / max(va, vb) < 0.05      # ~equal dollars


# --------------------------------------------------------------- converter

def _cond(**kw):
    base = {"step": "if-child", "is-else-condition?": False, "children": []}
    base.update(kw)
    return base


def test_convert_rsi_condition():
    c = _cond(**{"lhs-fn": "relative-strength-index", "lhs-val": "SPY",
                 "lhs-fn-params": {"window": 10}, "comparator": "gt",
                 "rhs-fixed-value?": True, "rhs-val": "79"})
    node = {"step": "if", "children": [
        dict(c, children=[{"step": "asset", "ticker": "SGOV",
                           "children": []}]),
        {"step": "if-child", "is-else-condition?": True,
         "children": [{"step": "asset", "ticker": "QQQ", "children": []}]},
    ]}
    ir = convert_symphony({"step": "root", "children": [node]})
    w = ir["rules"][0]["action"]["weights"]
    assert w["if"] == {"gt": [
        {"ind": "rsi", "symbol": "SPY", "window": 10,
         "smoothing": "wilder", "include_today": True}, 79.0]}
    assert w["then"] == [{"asset": "SGOV"}]
    assert w["else"] == [{"asset": "QQQ"}]


def test_convert_fixed_rhs_overrides_rhs_fn():
    """rhs-fixed-value? true means the numeric rhs-val wins even when an
    rhs-fn is present (QC _cond semantics)."""
    c = _cond(**{"lhs-fn": "relative-strength-index", "lhs-val": "SPY",
                 "lhs-window-days": "21", "comparator": "gt",
                 "rhs-fn": "moving-average-return", "rhs-window-days": "1",
                 "rhs-fixed-value?": True, "rhs-val": "30"})
    node = {"step": "if", "children": [dict(c, children=[])]}
    ir = convert_symphony({"step": "root", "children": [node]})
    w = ir["rules"][0]["action"]["weights"]
    assert w["if"]["gt"][1] == 30.0
    assert w["if"]["gt"][0]["window"] == 21


def test_convert_price_vs_sma_and_units():
    c = _cond(**{"lhs-fn": "current-price", "lhs-val": "SPY",
                 "comparator": "gt", "rhs-fn": "moving-average-price",
                 "rhs-val": "SPY", "rhs-fn-params": {"window": 200}})
    node = {"step": "if", "children": [dict(c, children=[])]}
    w = convert_symphony({"step": "root", "children": [node]}
                         )["rules"][0]["action"]["weights"]
    lhs, rhs = w["if"]["gt"]
    assert lhs == {"ind": "price", "field": "last", "symbol": "SPY"}
    assert rhs == {"ind": "sma", "symbol": "SPY", "window": 200,
                   "include_today": True}


def test_convert_max_drawdown_condition_units():
    c = _cond(**{"lhs-fn": "max-drawdown", "lhs-val": "QQQ",
                 "lhs-fn-params": {"window": 5}, "comparator": "gt",
                 "rhs-fixed-value?": True, "rhs-val": "6"})
    node = {"step": "if", "children": [dict(c, children=[])]}
    w = convert_symphony({"step": "root", "children": [node]}
                         )["rules"][0]["action"]["weights"]
    lhs, rhs = w["if"]["gt"]
    assert lhs == {"mul": [{"ind": "drawdown", "symbol": "QQQ", "window": 6,
                            "include_today": True}, -100]}
    assert rhs == 6.0


def test_convert_root_selector_and_window_override():
    tree = {"step": "root", "children": [{
        "step": "filter", "select-fn": "top", "select-n": "1",
        "sort-by-fn": "max-drawdown", "sort-by-fn-params": {"window": 5},
        "children": [
            {"step": "group", "children": [{"step": "asset", "ticker": "AAA",
                                            "children": []}]},
            {"step": "group", "children": [{"step": "asset", "ticker": "BBB",
                                            "children": []}]},
        ]}]}
    w = convert_symphony(tree, selector_window=9
                         )["rules"][0]["action"]["weights"]
    best = w["best"]
    assert best["by"] == {"metric": "drawdown", "window": 9}
    # QC max_dd pick = largest drawdown; engine drawdown is negative -> bottom
    assert best["order"] == "bottom"
    assert best["n"] == 1
    assert best["of"] == [{"equal": [{"asset": "AAA"}]},
                          {"equal": [{"asset": "BBB"}]}]


def test_convert_asset_rank_filter():
    tree = {"step": "root", "children": [{
        "step": "filter", "select-fn": "top", "select-n": "5",
        "sort-by-fn": "moving-average-return",
        "sort-by-fn-params": {"window": 5},
        "children": [{"step": "asset", "ticker": t, "children": []}
                     for t in ("TBF", "UST", "CTA")]}]}
    w = convert_symphony(tree)["rules"][0]["action"]["weights"]
    assert w["best"]["by"] == {"metric": "mean_return", "window": 5}
    assert w["best"]["order"] == "top"
    assert w["best"]["n"] == 5


def test_convert_specified_weights_and_inverse_vol():
    tree = {"step": "root", "children": [{
        "step": "wt-cash-specified", "children": [
            {"step": "asset", "ticker": "AAA", "children": [],
             "weight": {"num": "0", "den": 100}},
            {"step": "wt-inverse-vol", "window-days": "20",
             "weight": {"num": "100", "den": 100}, "children": [
                 {"step": "asset", "ticker": "BBB", "children": []}]},
        ]}]}
    w = convert_symphony(tree)["rules"][0]["action"]["weights"]
    items = w["weighted"]
    assert len(items) == 1                        # zero-weight child dropped
    assert items[0]["w"] == 1.0
    assert items[0]["of"] == {"inverse_vol": {"window": 20,
                                              "of": [{"asset": "BBB"}]}}


def test_convert_universe_is_asset_tickers_only():
    tree = {"step": "root", "children": [{
        "step": "if", "children": [
            _cond(**{"lhs-fn": "relative-strength-index", "lhs-val": "TQQQ",
                     "lhs-fn-params": {"window": 10}, "comparator": "gt",
                     "rhs-fixed-value?": True, "rhs-val": "79",
                     "children": [{"step": "asset", "ticker": "SGOV",
                                   "children": []}]}),
        ]}]}
    ir = convert_symphony(tree)
    assert ir["universe"]["static"] == ["SGOV"]   # TQQQ is data-only


def test_labels_carried_through_and_ignored_by_engine():
    tree = {"step": "root", "children": [{
        "step": "group", "name": "KMLM switcher",
        "children": [{"step": "asset", "ticker": "AAA", "children": []}]}]}
    w = convert_symphony(tree)["rules"][0]["action"]["weights"]
    assert w == {"equal": [{"asset": "AAA"}], "label": "KMLM switcher"}

    # the engine must run a labeled tree exactly like an unlabeled one
    days = weekdays(date(2024, 1, 1), 4)
    series = {"AAA": {d: [100.0] * 4 for d in days}}
    res = run_weights(w, series, days, ["AAA"])
    assert sum(f["qty"] for f in res["fills"]) > 0
