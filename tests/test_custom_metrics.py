"""User-defined indicators: named expressions expanded before evaluation,
plus the primitives that make the standard set composable in user space —
std (price sigma), atr (needs session highs/lows), and series_ema/series_sma
(smoothing a DERIVED series, i.e. a MACD signal line).

Also the home, since Phase 3 Task 9, of the expression-evaluator and
pct_rank cases that lived in `tests/test_engine.py`. That file was named
for the IR engine but six of its eight tests only ever exercised
`dqengine/runtime/core/{exprs,indicators,data}.py`, which the python engine
evaluates every bar.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest                                                       # noqa: E402
from dqengine.runtime.core import collect_ir_symbols, expand_metrics     # noqa: E402
from dqengine.runtime.core.exprs import (EvalContext, NOT_READY, evaluate,       # noqa: E402
                                   evaluate_bool)
from dqengine.runtime.core.indicators import IndicatorEngine                    # noqa: E402


# ---------------- expansion ----------------

MACD_IR = {
    "version": "0.1", "universe": {"static": ["TQQQ"]}, "params": {},
    "metrics": {
        "macd": {"sub": [{"ind": "ema", "window": 12},
                         {"ind": "ema", "window": 26}]},
        "macd_signal": {"ind": "series_ema", "of": {"metric": "macd"},
                        "window": 9},
    },
    "rules": [
        {"id": "enter", "trigger": {"type": "session_open"},
         "when": {"gt": [{"metric": "macd"}, {"metric": "macd_signal"}]},
         "action": {"type": "market_order", "side": "buy",
                    "size": {"pct_equity": 0.9}}},
    ],
}


def test_metric_nodes_are_fully_substituted():
    out = expand_metrics(MACD_IR)
    blob = repr(out["rules"])
    assert "metric" not in blob
    assert out["rules"][0]["when"]["gt"][0] == {
        "sub": [{"ind": "ema", "window": 12}, {"ind": "ema", "window": 26}]}
    # nested: the signal's `of` got macd substituted too
    assert out["rules"][0]["when"]["gt"][1]["of"]["sub"][0]["ind"] == "ema"


def test_no_metrics_is_identity():
    ir = {"universe": {"static": ["SPY"]}, "rules": []}
    assert expand_metrics(ir) is ir


def test_unknown_metric_is_a_clear_error():
    ir = {**MACD_IR, "rules": [{"id": "x", "trigger": {"type": "session_open"},
                                "when": {"gt": [{"metric": "nope"}, 0]},
                                "action": {"type": "liquidate"}}]}
    with pytest.raises(ValueError, match='unknown indicator "nope"'):
        expand_metrics(ir)


def test_cycles_are_a_clear_error():
    ir = {"universe": {"static": ["SPY"]},
          "metrics": {"a": {"add": [{"metric": "b"}, 1]},
                      "b": {"add": [{"metric": "a"}, 1]}},
          "rules": [{"id": "x", "trigger": {"type": "session_open"},
                     "when": {"gt": [{"metric": "a"}, 0]},
                     "action": {"type": "liquidate"}}]}
    with pytest.raises(ValueError, match="loop"):
        expand_metrics(ir)


def test_symbol_override_retargets_leaves():
    ir = {"universe": {"static": ["SPY"]},
          "metrics": {"trend": {"sub": [{"ind": "sma", "window": 5},
                                        {"ind": "sma", "window": 20}]}},
          "rules": [{"id": "x", "trigger": {"type": "session_open"},
                     "when": {"gt": [{"metric": "trend", "symbol": "QQQ"}, 0]},
                     "action": {"type": "liquidate"}}]}
    out = expand_metrics(ir)
    legs = out["rules"][0]["when"]["gt"][0]["sub"]
    assert all(leg["symbol"] == "QQQ" for leg in legs)
    # collect_ir_symbols sees through the definition
    assert collect_ir_symbols(ir) == ["QQQ", "SPY"]


# ---------------- new primitives (synthetic series) ----------------

def _eng(closes, highs=None, lows=None):
    eng = IndicatorEngine()
    for i, c in enumerate(closes):
        eng.on_session_close("X", c,
                             highs[i] if highs else None,
                             lows[i] if lows else None)
    return eng


def test_std_is_price_pstdev():
    eng = _eng([1.0, 2.0, 3.0, 4.0])
    v = eng.value({"ind": "std", "window": 4}, "X", {})
    assert abs(v - math.sqrt(1.25)) < 1e-12   # pstdev of 1..4


def test_atr_uses_session_range_and_gaps():
    closes = [10.0, 11.0, 12.0]
    highs = [10.5, 11.5, 13.0]
    lows = [9.5, 10.5, 11.5]
    eng = _eng(closes, highs, lows)
    # TR day1 = max(1.0, |11.5-10|, |10.5-10|) = 1.5
    # TR day2 = max(1.5, |13-11|, |11.5-11|) = 2.0
    v = eng.value({"ind": "atr", "window": 2}, "X", {})
    assert abs(v - 1.75) < 1e-12


def test_atr_degrades_without_extremes():
    eng = _eng([10.0, 11.0, 12.0])       # close-only: TR = |close - prev|
    v = eng.value({"ind": "atr", "window": 2}, "X", {})
    assert abs(v - 1.0) < 1e-12


def test_series_sma_of_constant_expression():
    eng = _eng([float(i) for i in range(1, 31)])
    # smoothing (price - price) = 0 stays 0
    v = eng.value({"ind": "series_sma", "window": 5,
                   "of": {"sub": [{"ind": "price"}, {"ind": "price"}]}},
                  "X", {})
    assert v == 0.0
    # sma_of(price, 3) at day 30 = mean(28, 29, 30)
    v = eng.value({"ind": "series_sma", "window": 3,
                   "of": {"ind": "price"}}, "X", {})
    assert abs(v - 29.0) < 1e-12


def test_series_ema_not_ready_until_window():
    eng = _eng([1.0, 2.0])
    v = eng.value({"ind": "series_ema", "window": 9,
                   "of": {"ind": "price"}}, "X", {})
    assert v is NOT_READY


def test_series_of_rejects_position_state():
    eng = _eng([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="position or cash"):
        eng.value({"ind": "series_ema", "window": 2,
                   "of": {"pos": "days_held"}}, "X", {})


# ---------------- end to end ----------------
# `test_macd_strategy_backtests` ran MACD_IR through the IR engine's
# run_backtest. Phase 3 Task 9 deleted that engine; the same strategy runs
# end to end on the surviving engine as the committed golden
# `ind_metric_macd` (tests/runtime/test_codegen_indicators.py), which
# compares generated python against a frozen fill-by-fill fixture rather
# than against a ">= 1 fill" smoke assertion.


def test_price_close_is_last_completed_close():
    eng = _eng([10.0, 11.0, 12.0])
    eng.prior_close["X"] = 12.0        # what the session loop maintains
    eng.last_price["X"] = 12.6         # intraday tick
    assert eng.value({"ind": "price", "field": "last"}, "X", {}) == 12.6
    assert eng.value({"ind": "price", "field": "close"}, "X", {}) == 12.0


def test_price_intraday_extremes_error_clearly():
    eng = _eng([10.0])
    with pytest.raises(ValueError, match="highest"):
        eng.value({"ind": "price", "field": "high"}, "X", {})


# ---------------- audit: contract between formulas and the evaluator ----------------

def _eval_when(when, closes):
    """Run a one-rule strategy over a synthetic series via the evaluator."""
    from dqengine.runtime.core.exprs import EvalContext, evaluate_bool
    eng = _eng(closes)
    eng.last_price["X"] = closes[-1]
    eng.prior_close["X"] = closes[-1]
    ctx = EvalContext({}, "X",
                      lambda node, sym: eng.value(node, sym, {}),
                      lambda name, sym, node: NOT_READY,   # no position state
                      lambda name: 1000.0)
    return evaluate_bool(when, ctx)


def test_ne_is_false_on_not_ready_unlike_not_eq():
    # sma(50) with 3 days of data is NOT_READY: != must NOT fire the rule
    when_ne = {"ne": [{"ind": "sma", "window": 50}, 0]}
    when_not_eq = {"not": {"eq": [{"ind": "sma", "window": 50}, 0]}}
    assert _eval_when(when_ne, [1.0, 2.0, 3.0]) is False
    assert _eval_when(when_not_eq, [1.0, 2.0, 3.0]) is True   # the old trap
    # with enough data both agree
    closes = [float(i) for i in range(1, 61)]
    assert _eval_when(when_ne, closes) is True


def test_division_by_zero_is_not_ready_not_a_crash():
    when = {"gt": [{"div": [1, {"sub": [{"ind": "sma", "window": 2},
                                        {"ind": "sma", "window": 2}]}]}, 0]}
    assert _eval_when(when, [10.0, 10.0, 10.0]) is False


def test_nested_smoothing_is_refused_not_silently_wrong():
    eng = _eng([float(i) for i in range(1, 61)])
    with pytest.raises(ValueError, match="already-smoothed"):
        eng.value({"ind": "series_ema", "window": 5,
                   "of": {"ind": "series_ema", "window": 9,
                          "of": {"ind": "price"}}}, "X", {})
    with pytest.raises(ValueError, match="already-smoothed"):
        eng.value({"ind": "series_sma", "window": 5,
                   "of": {"ind": "pct_rank",
                          "of": {"ind": "realized_vol", "window": 20},
                          "lookback": 252}}, "X", {})


# ===================================================================
# Expression evaluator + pct_rank.
# From tests/test_engine.py, deleted with the IR engine in Phase 3 Task 9.
# Neither group ever built a Backtester: `evaluate`/`evaluate_bool` live in
# dqengine/runtime/core/exprs.py and IndicatorEngine in dqengine/runtime/core/
# indicators.py, both of which the surviving engine calls on every bar.
# ===================================================================

def _ctx(params=None, ind=None, pos=None, sleeve=None):
    return EvalContext(params or {}, "TQQQ",
                       ind or (lambda n, s: 1.0),
                       pos or (lambda n, s, node: 1.0),
                       sleeve or (lambda n: 1000.0))


def test_expr_arithmetic_and_cases():
    c = _ctx(params={"X": 2.0})
    assert evaluate({"mul": [{"param": "X"}, 3]}, c) == 6.0
    assert evaluate({"sub": [1, 0.2]}, c) == 0.8
    v = evaluate({"cases": [
        {"when": {"gt": [1, 2]}, "value": 10},
        {"when": {"gt": [2, 1]}, "value": 20}], "default": 30}, c)
    assert v == 20


def test_not_ready_comparisons_are_false():
    c = _ctx(ind=lambda n, s: NOT_READY)
    assert evaluate_bool({"gt": [{"ind": "sma", "window": 5}, 0]}, c) is False
    assert evaluate_bool({"not": {"gt": [{"ind": "sma", "window": 5}, 0]}}, c) is True


def test_pct_rank_floor_convention():
    ie = IndicatorEngine()
    # 100 sessions of alternating returns -> vol series exists
    px = 100.0
    for i in range(120):
        px *= 1.01 if i % 2 == 0 else 0.995
        ie.on_session_close("TQQQ", px)
    node = {"ind": "pct_rank", "of": {"ind": "realized_vol", "window": 14},
            "lookback": 252, "min_obs": 60}
    r = ie.value(node, "TQQQ", {})
    assert r is not NOT_READY and 0.0 <= r <= 1.0


def test_pct_rank_accepts_a_non_vol_inner():
    """Decision 3 of the one-engine cleanup: pct_rank is generic now, via
    the same _expr_history series_ema already used."""
    ind = IndicatorEngine()
    px = 100.0
    for i in range(200):
        px *= 1.002 if i % 3 else 0.997
        ind.on_session_close("SPY", px)
        ind.prior_close["SPY"] = px
        ind.last_price["SPY"] = px
    node = {"ind": "pct_rank", "lookback": 60, "min_obs": 30,
            "of": {"ind": "sma", "window": 10}}
    v = ind.value(node, "SPY", {})
    assert v is not NOT_READY and 0.0 <= v <= 1.0


def test_pct_rank_of_realized_vol_is_unchanged():
    """The tqqq_weekly gate's inner keeps its closed form: same numbers.

    Pinned on a series whose vol actually MOVES (the regime changes every
    50 sessions) so the rank lands strictly inside (0, 1) and reads the
    whole history: a flat-vol series ranks 1.0 under any arithmetic at all
    and would notice nothing. This locks the realized_vol path's exact
    floats; the tqqq_weekly acceptance gate (520 fills, $13,603.38) is what
    locks the strategy that trades on them.
    """
    import random
    ind = IndicatorEngine()
    rng = random.Random(7)
    px = 50.0
    for i in range(400):
        amp = 0.002 + 0.004 * ((i // 50) % 3)
        px *= 1.0 + amp * rng.uniform(-1.0, 1.0)
        ind.on_session_close("TQQQ", px)
    node = {"ind": "pct_rank", "lookback": 252, "min_obs": 60,
            "of": {"ind": "realized_vol", "window": 20}}
    assert ind.value(node, "TQQQ", {}) == 0.5338645418326693
