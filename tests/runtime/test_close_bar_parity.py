"""A strategy that trades across the session close, pinned against LEAN.

`tools/bench_engines/ema_cross_fast.py` is always in the market and flips
22,796 times in 5.4 years of SPY minute bars, 75 of them on a session's last
bar. LEAN turns a market order placed after the close into market-on-open and
fills it at the next session's first open, before the algorithm sees that bar.

LEAN's result on these bars with fees set to zero (run 2026-09-20): 22,796
orders, End Equity 991119. This engine: 22,796 fills, 991113. 28 fills differ
and none of them is about the close: 26 fall on 2023-08-03, where LEAN's
prices are 3.8% above the bar file all day, and 2 on 2023-06-05, where LEAN
fills four minutes that have no trades with copies of the previous bar and an
average crosses three minutes sooner. Before the close rule this engine ended
at 993457, with 103 fills different.
"""
import os

from dqengine.config import data_root
from dqengine.runtime import run_python_backtest

from conftest_helpers import reference_bars

HERE = os.path.dirname(os.path.abspath(__file__))
ALGO = os.path.join(HERE, "..", "..", "tools", "bench_engines", "ema_cross_fast.py")
DATA = data_root()

needs_data = reference_bars(DATA, "spy")


@needs_data
def test_orders_placed_on_the_last_bar_fill_at_the_next_open_as_on_lean():
    res = run_python_backtest(open(ALGO).read(), DATA)
    assert "error" not in res, res.get("error")
    assert len(res["fills"]) == 22796
    assert res["stats"]["end_equity"] == 991113.0          # LEAN: 991119
    # no fill is stamped at the close any more: the 75 that were now open the next session
    assert not [f for f in res["fills"] if f["ms"] >= 57_600_000]
    assert len([f for f in res["fills"] if f["ms"] == 34_260_000]) >= 75
