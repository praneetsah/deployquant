"""tools/bench_vs_lean.py is a GATE, named in the cleanup plan's constraints
and run after every task. A gate that cannot fail is not a gate.

Before this file, `--skip-lean` compared the IR engine against dqengine.runtime.
Phase 3 deletes the IR engine, which would have left one number being
compared with itself -- `len({x}) > 1` is False, so the script would have
printed "all engines agree to the penny" and exited 0 on any result at all,
including a dqengine.runtime that had started trading differently.
"""
import os
import sys

TOOLS = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                     "..", "..", "tools"))
sys.path.insert(0, TOOLS)
import bench_vs_lean as B                                       # noqa: E402


def test_the_lean_reference_is_committed():
    assert B.LEAN_REFERENCE["end_equity"] == 13603.38
    assert B.LEAN_REFERENCE["fills"] == 520


def test_one_number_is_never_agreement(capsys):
    """The whole point. A single engine cannot agree with anything."""
    assert B.agreement({"DQengine / dqengine.runtime": 13603.38}) == 1
    assert "DIVERGENCE" in capsys.readouterr().out


def test_two_matching_numbers_agree(capsys):
    assert B.agreement({"DQengine / dqengine.runtime": 13603.38,
                        "LEAN (reference)": 13603.38}) == 0
    assert "all engines agree to the penny" in capsys.readouterr().out


def test_a_cent_of_disagreement_fails(capsys):
    assert B.agreement({"DQengine / dqengine.runtime": 13603.39,
                        "LEAN (reference)": 13603.38}) == 1
    assert "DIVERGENCE" in capsys.readouterr().out


def test_the_ir_engine_is_not_a_participant():
    assert not hasattr(B, "run_dq_ir")
    src = open(os.path.join(TOOLS, "bench_vs_lean.py")).read()
    assert "ir_engine" not in src
