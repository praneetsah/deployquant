"""Reading an IR document: which symbols it mentions, which it can hold.

`collect_ir_symbols` and `collect_tradeable_symbols` are shared library code
(dqengine/runtime/core), not engine code. The distinction between them is
load-bearing on the money path: `collect_ir_symbols` is what the codegen
subscribes (an RSI gate on CCC needs CCC's bars), while
`collect_tradeable_symbols` is what execution attribution treats as a
position this strategy could legitimately hold — widening it would let a
manual position be attributed to a deployment (api/tests/
test_execution_attribution.py names both).

These five arrived here in Phase 3 Task 9 from `tests/test_multi.py`, whose
other eight tests drove the IR Backtester and were deleted with it. These
never touched that engine. `tests/runtime/test_core_package.py` pins that
the two helpers are importable from their new home and answer the trivial
one-symbol case; the semantics below are what that smoke test does not
cover.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dqengine.runtime.core import collect_ir_symbols, collect_tradeable_symbols  # noqa: E402


def make_ir(rules, universe, params=None):
    return {"ir_version": "0.2", "meta": {"name": "t"}, "params": params or {},
            "universe": {"static": universe}, "rules": rules}


def test_collect_ir_symbols():
    ir = make_ir([{
        "id": "r", "trigger": {"type": "session_open", "days": "all"},
        "when": {"gt": [{"ind": "price", "symbol": "CCC"}, 5]},
        "action": {"type": "set_weights", "weights": {
            "equal": [{"asset": "AAA"}, {"asset": "DDD"}]}},
    }], ["AAA", "BBB"])
    assert collect_ir_symbols(ir) == ["AAA", "BBB", "CCC", "DDD"]


def test_collect_tradeable_symbols_excludes_expression_only_signals():
    """An RSI gate reading CCC does not make CCC something this IR can
    hold -- unlike collect_ir_symbols, CCC must NOT appear."""
    ir = make_ir([{
        "id": "r", "trigger": {"type": "session_open", "days": "all"},
        "when": {"gt": [{"ind": "rsi", "symbol": "CCC"}, 5]},
        "action": {"type": "set_weights", "weights": {
            "equal": [{"asset": "AAA"}, {"asset": "DDD"}]}},
    }], ["AAA", "BBB"])
    assert collect_tradeable_symbols(ir) == ["AAA", "BBB", "DDD"]


def test_collect_tradeable_symbols_includes_universe_static():
    ir = make_ir([], ["AAA", "BBB"])
    assert collect_tradeable_symbols(ir) == ["AAA", "BBB"]


def test_collect_tradeable_symbols_includes_weight_tree_assets():
    ir = make_ir([{
        "id": "r", "trigger": {"type": "session_open", "days": "all"},
        "action": {"type": "set_weights", "weights": {
            "equal": [{"asset": "AAA"}, {"asset": "ZZZ"}]}},
    }], ["AAA"])
    assert collect_tradeable_symbols(ir) == ["AAA", "ZZZ"]


def test_collect_tradeable_symbols_includes_action_level_symbol_override():
    ir = make_ir([{
        "id": "r", "trigger": {"type": "session_open", "days": "all"},
        "action": {"type": "buy", "symbol": "QQQQ", "qty": 1},
    }], ["AAA"])
    assert collect_tradeable_symbols(ir) == ["AAA", "QQQQ"]
