"""dqengine/runtime/core: the shared library, under the package that owns it.

fills, ledger, portfolio, data, exprs, indicators and weights are the python
engine's own code. They lived in `ir_engine` because that is where they were
written, not because the IR engine owned them -- dqengine.runtime imported all
seven, and the api imported four. This pins the new home, the five helpers
that came with them, and the one structural rule that keeps the sandbox
image working: core must not reach back up into dqengine.runtime.
"""
import os


def test_the_seven_modules_import_from_core():
    from dqengine.runtime.core import (data, exprs, fills, indicators, ledger,
                                 portfolio, weights)
    assert data.SCALE == 10000
    assert fills.round_price(1.23456) > 0
    assert exprs.NOT_READY is not None
    for mod in (indicators, ledger, portfolio, weights):
        assert mod.__name__.startswith("dqengine.runtime.core.")


def test_the_five_helpers_import_from_core():
    from dqengine.runtime.core import (collect_ir_symbols, collect_tradeable_symbols,
                                 expand_metrics, flow_adjusted_returns,
                                 stats_from_equity)
    ir = {"universe": {"static": ["SPY"]}, "rules": []}
    assert collect_ir_symbols(ir) == ["SPY"]
    assert collect_tradeable_symbols(ir) == ["SPY"]
    assert expand_metrics(ir)["rules"] == []
    assert flow_adjusted_returns([110.0], [0.0], 100.0) == [0.10000000000000009]
    assert stats_from_equity([], [], [], 100.0) == {}


def test_core_does_not_import_its_parent():
    """dqengine/runtime/__init__ imports .backtester, which imports numpy. The
    sandbox driver (dqengine.sandbox.pyrunner) loads engine_rpc.py by file
    path so its process never pays for the whole engine -- once the service
    had no numpy at all. A core module that reached back up would
    reintroduce that dependency through the back door."""
    root = os.path.join(os.path.dirname(__file__), "..", "..",
                        "dqengine", "runtime", "core")
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".py"):
            continue
        src = open(os.path.join(root, fname)).read()
        assert "from dqengine.runtime" not in src, fname
        assert "import dqengine.runtime" not in src, fname
        assert "ir_engine" not in src, fname
