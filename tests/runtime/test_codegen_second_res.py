"""Second-resolution blocks on the python engine.

Real second bars exist locally only for SPY 2013, so this rides a synthetic
SecondStore — sparse clusters at the open, midday and the close tail, which
is exactly where the union walk and the before-close latch earn their keep.
That fixture came from the IR engine's own second-resolution suite and now
lives in conftest_helpers.py, the suite itself having gone with the engine
in Phase 3 Task 9.

The equivalence to prove: the IR engine's latch fires the before_close(1)
batch at `candidate + bar_ms` where candidate is the last ts <=
close - 60000 - bar_ms; the runtime's schedule fires
before_market_close(sym, 1) on the first bar ending at or after
close - 60000. With 1-second bars both land on the bar ENDING at
close - 60000, at the same price.

The session_open batch is the half that does NOT come out even at a
minute's granularity: the IR engine fires it at the first bar's END
(09:30:01 here), so the generated schedule has to name that bar rather
than 09:31 — see `open_offset` in dqengine.codegen.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "tests"))
from conftest_helpers import (SECOND_DAYS as DAYS,              # noqa: E402
                              SecondStore, build_second_spec as build_spec)
from parity import assert_golden                                # noqa: E402

OPEN_MS = 34_200_000            # 09:30:00 — the first bar STARTS here
FIRST_BAR_END_MS = OPEN_MS + 1000
BC_MS = 57_600_000 - 60_000     # 15:59:00


def second_ir():
    return {
        "ir_version": "0.2",
        "meta": {"name": "Second Blocks", "resolution": "second"},
        "params": {}, "universe": {"static": ["AAA", "BBB"]},
        "rules": [
            {"id": "open-alloc",
             "trigger": {"type": "session_open", "days": "all"},
             "action": {"type": "set_weights",
                        "weights": {"equal": [{"asset": "AAA"}]}}},
            {"id": "daily-rebalance",
             "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
             "action": {"type": "set_weights",
                        "weights": {"equal": [{"asset": "AAA"},
                                              {"asset": "BBB"}]}}},
        ],
    }


def test_second_resolution_blocks_match_their_golden():
    got = assert_golden(second_ir(), start=DAYS[0].isoformat(),
                        end=DAYS[-1].isoformat(), name="sec_alloc",
                        store=SecondStore(build_spec()), bar_ms=1000)
    stamps = {f["ms"] for f in got["fills"]}
    assert stamps, "neither allocation rule rebalanced"
    # both batches fire, and each on its own SECOND — not on a minute edge
    assert FIRST_BAR_END_MS in stamps, stamps
    assert BC_MS in stamps, stamps
    assert stamps <= {FIRST_BAR_END_MS, BC_MS}, stamps


def test_second_resolution_rules_match_their_golden():
    ir = second_ir()
    ir["rules"] = [
        {"id": "buy", "trigger": {"type": "session_open", "days": "all"},
         "when": {"not": {"pos": "invested", "symbol": "AAA"}},
         "action": {"type": "market_order", "side": "buy", "symbol": "AAA",
                    "size": {"pct_equity": 0.8}}},
        {"id": "flat",
         "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
         "action": {"type": "liquidate"}},
    ]
    got = assert_golden(ir, start=DAYS[0].isoformat(),
                        end=DAYS[-1].isoformat(), name="sec_rules",
                        store=SecondStore(build_spec()), bar_ms=1000)
    buys = [f for f in got["fills"] if f["qty"] > 0]
    sells = [f for f in got["fills"] if f["qty"] < 0]
    assert len(buys) == len(DAYS) and len(sells) == len(DAYS), got["fills"]
    assert {f["ms"] for f in buys} == {FIRST_BAR_END_MS}, buys
    assert {f["ms"] for f in sells} == {BC_MS}, sells


def test_the_generated_code_subscribes_at_second_resolution():
    from dqengine import codegen
    code = codegen.generate_python(second_ir())
    assert "Resolution.SECOND" in code


def test_an_at_time_rule_is_refused_at_second_resolution():
    """The one second-resolution trigger the two engines do NOT agree on.
    The IR engine fires at_time only on a bar whose END is exactly the wall
    time; the runtime fires on the first bar ending at or after it. At
    minute resolution that is the same bar; at second resolution a symbol
    that did not print in that one second leaves the oracle skipping the
    rule all session while the generated code fires it later, at a
    different price (measured against the synthetic store: 0 fills vs 4).
    Refuse, and keep the strategy on the engine that can run it."""
    import pytest

    from dqengine import codegen
    ir = second_ir()
    ir["rules"] = [{"id": "t",
                    "trigger": {"type": "at_time", "time": "12:00",
                                "days": "all"},
                    "action": {"type": "market_order", "side": "buy",
                               "symbol": "AAA", "size": {"pct_equity": 0.5}}}]
    with pytest.raises(codegen.CodegenUnsupported, match="at_time"):
        codegen.generate_python(ir)
    # ... and the same rule at minute resolution still compiles
    ir["meta"]["resolution"] = "minute"
    assert "_at_1200" in codegen.generate_python(ir)
