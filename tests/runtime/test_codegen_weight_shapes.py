"""Allocation coverage across weight-tree SHAPES, not just rotation.json's.

rotation.json is one shape: `best` over plain assets, ranked by cum_return,
top-1. The tree also has equal / weighted / inverse_vol / if / nested best,
and `if` conditions reach the whole indicator surface — including ATR, whose
highs and lows are exactly where the warm-up feed can differ.

Two windows each, neither starting on the placeholder date codegen emits,
because a window equal to the placeholder is what hid the warm-up bugs the
first time round.

Each case was proved against the IR engine and is frozen as `wt_<shape>_
<start>`. The frozen digest is STRICTER than the cross-engine comparison it
replaces in two ways -- that one carried (day, symbol, qty, price) and only
the final equity, this one carries the fill MINUTE as well and every daily
equity value -- and weaker in exactly one: it cannot pin the order in which
a rebalance submits two symbols taking the same integer delta, because that
order is Python's per-process hash order and differs between processes.
`inverse_vol` really does reorder across seeds; nothing else about it moves.
See parity._canonical, and test_the_rebalance_delta_tie_break_is_still_hash
_ordered, which is what now pins the construct itself.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA, assert_golden                           # noqa: E402

SYMS = ["SPY", "QQQ", "IWM", "TLT"]

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

SHAPES = {
    "best_cum_return": {"best": {
        "of": [{"asset": s} for s in SYMS],
        "by": {"metric": "cum_return", "window": 60},
        "order": "top", "n": 1}},
    "equal": {"equal": [{"asset": s} for s in SYMS]},
    "weighted": {"weighted": [{"w": 0.6, "of": {"asset": "SPY"}},
                              {"w": 0.4, "of": {"asset": "TLT"}}]},
    "inverse_vol": {"inverse_vol": {"of": [{"asset": s} for s in SYMS],
                                    "window": 20}},
    "nested_best": {"best": {
        "of": [{"best": {"of": [{"asset": "SPY"}, {"asset": "QQQ"}],
                         "by": {"metric": "cum_return", "window": 30},
                         "order": "top", "n": 1}},
               {"asset": "TLT"}],
        "by": {"metric": "cum_return", "window": 60},
        "order": "top", "n": 1}},
    # `if` reaches the indicator surface. ATR specifically: highs and lows
    # equal the close over the IR engine's warm-up, and feeding REAL daily
    # highs/lows instead would change it for 260 sessions.
    "if_rsi": {"if": {"gt": [{"ind": "rsi", "window": 14, "symbol": "SPY"}, 50]},
               "then": [{"asset": "QQQ"}], "else": [{"asset": "TLT"}]},
    "if_atr": {"if": {"gt": [{"ind": "atr", "window": 14, "symbol": "SPY"}, 1.0]},
               "then": [{"asset": "TLT"}], "else": [{"asset": "SPY"}]},
}

WINDOWS = [("2024-03-01", "2024-06-28"), ("2025-01-02", "2025-06-30")]

CASES = [(name, start, end)
         for name in SHAPES for start, end in WINDOWS]


def _ir_for(node):
    return {"ir_version": "0.2", "meta": {"name": "shape"}, "params": {},
            "universe": {"static": SYMS},
            "rules": [{"id": "rot",
                       "trigger": {"type": "before_close", "minutes": 1,
                                   "days": "all"},
                       "action": {"type": "set_weights", "weights": node}}]}


def golden_name(shape, start):
    return f"wt_{shape}_{start.replace('-', '')}"


@needs_data
@pytest.mark.parametrize("shape,start,end", CASES,
                         ids=[f"{n}-{s}" for n, s, _ in CASES])
def test_generated_allocation_matches_its_golden(shape, start, end):
    got = assert_golden(_ir_for(SHAPES[shape]), start=start, end=end,
                        name=golden_name(shape, start), cash=10000.0,
                        margin=1.0, canonical_fill_order=True)
    # a shape that never rebalanced would match its golden trivially
    assert got["fills"], (shape, start, "window too quiet to prove anything")
    assert abs(got["stats"]["end_equity"] - got["equity"][-1]) < 0.01, \
        (shape, start, got["stats"]["end_equity"], got["equity"][-1])
