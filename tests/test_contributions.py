"""Recurring deposits in backtests: the deposit must land in the sleeve (and
the equity curve), but must never be counted as investment RETURN — return
stats run on the time-weighted series when cash flows in mid-run.

Phase 3 Task 9 dropped this file's four end-to-end cases, which drove the
deleted IR engine's `run_backtest` with `cash_events`. Deposits run end to
end on the surviving engine in three committed places:
`tests/runtime/test_codegen.py::test_a_cash_deposit_replays_into_the_python_engine`
(golden `led_cash_deposit`, whose deposit date 2024-01-20 is a Saturday, so
it also covers the weekend roll-forward the dropped
`test_deposit_on_weekend_rolls_forward` asserted),
`tests/runtime/test_codegen_capsules.py::test_a_capsule_strategy_takes_a_deposit`,
and the golden `tpl_monthly_invest_20240102`. The flow-adjusted return
arithmetic below is shared library code (dqengine/runtime/core) and is tested
here directly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dqengine.runtime.core import flow_adjusted_returns                  # noqa: E402


# ---------------- pure math ----------------

def test_flow_adjusted_returns_strips_deposits():
    # flat market, one $100 deposit on day 1: equity jumps, return is zero
    eq = [1000.0, 1100.0, 1100.0]
    flows = [0.0, 100.0, 0.0]
    r = flow_adjusted_returns(eq, flows, 1000.0)
    assert r == [0.0, 0.0, 0.0]


def test_flow_adjusted_returns_without_flows_is_plain_returns():
    eq = [1010.0, 1020.1]
    r = flow_adjusted_returns(eq, [0.0, 0.0], 1000.0)
    assert abs(r[0] - 0.01) < 1e-12 and abs(r[1] - 0.01) < 1e-12


def test_flow_adjusted_returns_sees_real_gains_around_deposit():
    # day 1: $100 lands AND the pot gains 10% on the combined base
    eq = [1000.0, 1210.0]
    r = flow_adjusted_returns(eq, [0.0, 100.0], 1000.0)
    assert abs(r[1] - 0.10) < 1e-12
