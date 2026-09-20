"""Equity-curve statistics, shared by every surface that shows a number.

Extracted from the Backtester so a live sleeve, an aggregated portfolio and
a backtest all report the same math. Callers today: dqengine.runtime.backtester
(:579), the api's /api/portfolio aggregation (main.py:867 `_managed_stats`),
and the tests.

flow_adjusted_returns is not an implementation detail of stats_from_equity
even though that is its only in-module caller: it is the time-weighted
series, and `tests/test_contributions.py` asserts it directly because "a
deposit is not a gain" is the property most easily broken by accident.
"""
from __future__ import annotations


def flow_adjusted_returns(equity: list, flows: list, start_cash: float) -> list:
    """Daily returns with external deposits stripped out — the time-weighted
    series. flows[i] is the cash that landed at day i's OPEN (0.0 mostly), so
    day i's denominator already includes it: a deposit is not a gain."""
    out = []
    prev = start_cash
    for e, f in zip(equity, flows):
        base = prev + f
        out.append(e / base - 1.0 if base > 0 else 0.0)
        prev = e
    return out


def stats_from_equity(equity_days: list, equity: list, flows: list,
                      start_cash: float) -> dict:
    """Stats over any equity series (dates, values, per-day external flows).
    Extracted from Backtester._compute_stats so live/aggregated curves reuse
    exactly the same math. Returns {} when the series can't say anything."""
    eq = equity
    if len(eq) < 2:
        return {}
    contributed = sum(flows)
    start_v, end_v = start_cash, eq[-1]
    days_cal = (equity_days[-1] - equity_days[0]).days
    years = max(days_cal / 365.25, 1e-9)

    if contributed:
        # external cash flowed in mid-run: end/start arithmetic would count
        # deposits as gains, so every return stat runs on the time-weighted
        # series instead; the deposits themselves are reported separately
        r = flow_adjusted_returns(eq, flows, start_v)
        growth = []
        g = 1.0
        for x in r:
            g *= 1.0 + x
            growth.append(g)
        end_g = growth[-1]
        cagr = end_g ** (1 / years) - 1 if end_g > 0 else -1.0
        peak, max_dd = growth[0], 0.0
        for v in growth:
            peak = max(peak, v)
            max_dd = min(max_dd, v / peak - 1.0)
        rets = r[1:]                       # day 0 excluded, as below
        net_profit_pct = (end_g - 1) * 100
    else:
        cagr = (end_v / start_v) ** (1 / years) - 1 if end_v > 0 else -1.0
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            max_dd = min(max_dd, v / peak - 1.0)
        rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
        net_profit_pct = (end_v / start_v - 1) * 100

    mean = sum(rets) / len(rets)
    var = sum((r_ - mean) ** 2 for r_ in rets) / max(len(rets) - 1, 1)
    sharpe = (mean / (var ** 0.5) * (252 ** 0.5)) if var > 0 else 0.0
    stats = {
        "start_equity": start_v, "end_equity": round(end_v, 2),
        "net_profit_pct": round(net_profit_pct, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(-max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "days": len(eq),
    }
    if contributed:
        stats["contributed"] = round(contributed, 2)
        stats["invested"] = round(start_v + contributed, 2)
    return stats
