"""stats_from_equity: the Backtester's stats math extracted so live and
aggregated curves reuse exactly the same formulas."""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dqengine.runtime.core import stats_from_equity            # noqa: E402


def days(n, start=date(2026, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_too_short_series_returns_empty():
    assert stats_from_equity([date(2026, 1, 1)], [100.0], [0.0], 100.0) == {}


def test_flow_free_doubling_over_a_year():
    d = [date(2025, 1, 1), date(2026, 1, 1)]
    s = stats_from_equity(d, [100.0, 200.0], [0.0, 0.0], 100.0)
    assert s["net_profit_pct"] == 100.0
    assert 99.0 < s["cagr_pct"] < 101.0
    assert s["max_drawdown_pct"] == 0.0
    assert s["days"] == 2
    assert "contributed" not in s


def test_deposit_is_not_a_gain():
    # flat performance; $100 lands at day 1's open
    s = stats_from_equity(days(3), [100.0, 200.0, 200.0],
                          [0.0, 100.0, 0.0], 100.0)
    assert s["net_profit_pct"] == 0.0
    assert s["contributed"] == 100.0
    assert s["invested"] == 200.0


def test_zero_start_cash_all_flows():
    # portfolio aggregation case: start_cash 0, capital arrives as day-0 flow
    s = stats_from_equity(days(3), [1000.0, 1100.0, 1210.0],
                          [1000.0, 0.0, 0.0], 0.0)
    assert round(s["net_profit_pct"], 1) == 21.0


def test_max_drawdown():
    s = stats_from_equity(days(3), [100.0, 50.0, 75.0], [0.0] * 3, 100.0)
    assert s["max_drawdown_pct"] == 50.0
