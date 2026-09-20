"""The indicator surface, end to end on both engines.

There is no second implementation of any of these on the codegen side any
more — `self._bc.indicator(node, sym)` hands the node to the IR engine's
own IndicatorEngine. These tests exist to prove that claim symbol by
symbol, and to pin the two things the shared engine does NOT give for
free: atr's per-session highs and lows, and the last_of_month selector
(a calendar question, not an indicator).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA, assert_golden                          # noqa: E402
from dqengine import codegen                                               # noqa: E402

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

WINDOW = ("2024-03-01", "2024-06-28")


def _gated(when, *, metrics=None, universe=("SPY",)):
    ir = {
        "ir_version": "0.2", "meta": {"name": "Indicator Gate"},
        "params": {"W": 20},
        "universe": {"static": list(universe)},
        "rules": [
            {"id": "enter", "trigger": {"type": "session_open", "days": "all"},
             "when": {"all": [{"not": {"pos": "invested"}}, when]},
             "action": {"type": "market_order", "side": "buy",
                        "size": {"pct_equity": 0.5}}},
            {"id": "exit",
             "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
             "when": {"not": when},
             "action": {"type": "liquidate"}},
        ],
    }
    if metrics:
        ir["metrics"] = metrics
    return ir


GATES = {
    # a custom metric: MACD's histogram, composed from the same vocabulary
    "ind_metric_macd": (
        _gated({"gt": [{"metric": "macd_hist"}, 0]}, metrics={
            "macd": {"sub": [{"ind": "ema", "window": 12},
                             {"ind": "ema", "window": 26}]},
            "macd_hist": {"sub": [{"metric": "macd"},
                                  {"ind": "series_ema", "window": 9,
                                   "of": {"metric": "macd"}}]},
        })),
    # a metric retargeted to ANOTHER ticker: every bare leaf inside the
    # definition binds to QQQ (expand_metrics' `retarget`)
    "ind_metric_retargeted": (
        _gated({"gt": [{"metric": "trend", "symbol": "QQQ"}, 0]},
               metrics={"trend": {"sub": [{"ind": "sma", "window": 10},
                                          {"ind": "sma", "window": 50}]}},
               universe=("SPY",))),
    # cross-symbol leaf
    "ind_cross_symbol": (
        _gated({"gt": [{"ind": "rsi", "window": 14, "symbol": "TLT"}, 50]},
               universe=("SPY",))),
    # atr: the only indicator that reads session highs and lows. Threshold
    # 6.0 (not the naive 3.0) deliberately: at 3.0 the gate crosses up once
    # in this data and never back, which only proves the entry-day read --
    # see test_atr_matches_its_golden_across_a_reversal below, which
    # runs this same gate over a window long enough for SPY's 20-day ATR
    # to cross back both ways.
    "ind_atr": (
        _gated({"gt": [{"ind": "atr", "window": {"param": "W"}}, 6.0]})),
    # price(close): the last COMPLETED daily close, not the live price.
    # Proved below (test_price_close_matches_its_golden_across_a_
    # reversal) over a window long enough to reach a real crossing back --
    # the base WINDOW here never sells, same reasoning as atr above.
    "ind_price_close": (
        _gated({"gt": [{"ind": "price", "field": "close"},
                       {"ind": "sma", "window": 50}]})),
    # derived series
    "ind_series_sma": (
        _gated({"gt": [{"ind": "series_sma", "window": 5,
                        "of": {"ind": "rsi", "window": 14}}, 50]})),
    # wilder rsi (the Composer convention) alongside the IR default
    "ind_rsi_wilder": (
        _gated({"gt": [{"ind": "rsi", "window": 14, "smoothing": "wilder"},
                       50]})),
    # include_today: the Composer-truth snapshot variant
    "ind_include_today": (
        _gated({"gt": [{"ind": "rsi", "window": 10, "include_today": True},
                       50]})),
    # pct_rank over something other than realized_vol (cleanup Decision 3).
    # Proved below rather than in the parametrized sweep, for the same
    # reason atr and price(close) are: a gate that only ever opens proves
    # the entry-day read and nothing else.
    "ind_pct_rank_generic": (
        _gated({"gt": [{"ind": "pct_rank", "lookback": 60, "min_obs": 30,
                        "of": {"ind": "rsi", "window": 14}}, 0.8]})),
}


# These are proved separately below, each over a window that reaches a real
# reversal -- see the three _across_a_reversal tests.
_REVERSAL_GATES = ("ind_atr", "ind_price_close", "ind_pct_rank_generic")


@needs_data
@pytest.mark.parametrize("name", sorted(k for k in GATES
                                        if k not in _REVERSAL_GATES))
def test_indicator_gate_matches_its_golden(name):
    assert_golden(GATES[name], start=WINDOW[0], end=WINDOW[1], name=name)


@needs_data
def test_last_of_month_matches_its_golden():
    ir = _gated({"not": {"pos": "invested"}})
    ir["rules"][0]["trigger"] = {"type": "session_open",
                                 "days": "first_of_month"}
    ir["rules"][1]["trigger"] = {"type": "before_close", "minutes": 1,
                                 "days": "last_of_month"}
    ir["rules"][1]["when"] = {"gt": [{"pos": "qty"}, 0]}
    got = assert_golden(ir, start="2024-01-02", end="2024-06-28",
                        name="ind_last_of_month")
    assert len(got["fills"]) >= 8, "the window must contain several months"


@needs_data
def test_atr_matches_its_golden_across_a_reversal():
    """A window that only crosses the gate once proves the entry-day read
    and nothing else: once invested, the entry rule's `not invested` guard
    short-circuits before atr is evaluated again, and the exit rule's
    result only reaches a fill or equity if it actually crosses back --
    otherwise two engines computing materially different atr values every
    session would still match on "fills one-for-one, equity to the cent"
    trivially, because neither ever acts on the difference. SPY's 20-day
    atr (cold-started -- this IR is single-symbol, so warm=False, exactly
    like ind_atr's base-window sibling) crosses 6.0 going up on 2024-04-26,
    back down on 2024-05-13, and back up again on 2024-07-31 -- a real
    up/down/up, not a coincidence of this window's edges."""
    got = assert_golden(GATES["ind_atr"], start=WINDOW[0], end="2024-07-31",
                        name="ind_atr")
    sells = [f for f in got["fills"] if f["qty"] < 0]
    assert sells, "the atr gate must cross back down at least once"


@needs_data
def test_price_close_matches_its_golden_across_a_reversal():
    """Same structural gap as atr above, for price(close) vs sma(50): the
    base WINDOW's single buy never reverses, so a divergence in either
    indicator's value that never flips the comparison would pass unnoticed.
    SPY's close first closes below its own 50-day sma on 2024-07-25, which
    the exit rule reads at the before-close handler of the NEXT session
    (2024-07-26) and sells; it closes back above on 2024-07-26, which the
    entry rule reads at the open of 2024-07-29 and re-buys -- a real
    pullback and recovery, not a hair's-width tie: both crossings clear
    their threshold by roughly half a percent."""
    got = assert_golden(GATES["ind_price_close"], start=WINDOW[0],
                        end="2024-07-29", name="ind_price_close")
    sells = [f for f in got["fills"] if f["qty"] < 0]
    assert sells, "the price(close) gate must cross back down at least once"


@needs_data
def test_pct_rank_generic_matches_its_golden_across_a_reversal():
    """Cleanup Decision 3: pct_rank's inner is any expression now, computed
    by _expr_history — the same as-of machinery series_ema already used —
    so the two engines get it from one place. rsi(14)'s own 60-session
    percentile crosses 0.8 going up on 2024-05-07, back down on 2024-05-30
    and up again on 2024-06-14 inside the base window, so this proves the
    ranked series, not just the day the gate first opened."""
    got = assert_golden(GATES["ind_pct_rank_generic"], start=WINDOW[0],
                        end=WINDOW[1], name="ind_pct_rank_generic")
    sells = [f for f in got["fills"] if f["qty"] < 0]
    buys = [f for f in got["fills"] if f["qty"] > 0]
    assert len(buys) >= 2 and sells, got["fills"]


def test_atr_turns_on_the_ohlc_tracker_and_nothing_else_does():
    """on_data costs the quiet-bar fast path for the whole run, so it is
    emitted only for the one indicator that needs session extremes."""
    with_atr = codegen.generate_python(GATES["ind_atr"])
    assert "def on_data" in with_atr and "self._bc.on_bar(data)" in with_atr
    assert "track_ohlc=True" in with_atr
    without = codegen.generate_python(GATES["ind_cross_symbol"])
    assert "def on_data" not in without
    assert "track_ohlc=False" in without


@needs_data
def test_a_non_atr_strategy_keeps_the_quiet_bar_fast_path():
    """The fast path is what makes a 5-year run 2 seconds. Losing it
    silently for every strategy would be the real cost of this phase."""
    from parity import generate, run_py
    got = run_py(generate(GATES["ind_cross_symbol"]), WINDOW[0], WINDOW[1],
                 10000.0, DATA, None, None, None)
    fp = got["fast_path"]
    assert fp["eligible"] and fp["sessions"] == fp["of"], fp


def test_an_unknown_metric_is_named_not_swallowed():
    ir = _gated({"gt": [{"metric": "nope"}, 0]}, metrics={"yes": 1})
    with pytest.raises(Exception, match="nope"):
        codegen.generate_python(ir)


def test_a_symbol_named_only_inside_a_metric_is_still_subscribed():
    code = codegen.generate_python(GATES["ind_metric_retargeted"])
    assert "'QQQ'" in code.split("class ")[0], "QQQ must be in SYMS"
