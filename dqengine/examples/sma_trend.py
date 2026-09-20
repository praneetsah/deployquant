"""Trend following on one ETF: hold SPY while its 50-day average is above
its 200-day average, otherwise sit in cash. Checked once a day, a few
minutes after the open.

    dqengine data fetch SPY --from 2019-01-01
    dqengine example sma_trend
    dqengine backtest sma_trend.py
"""
from AlgorithmImports import *


class SmaTrend(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2020, 1, 2)
        self.set_end_date(2025, 12, 31)
        self.set_cash(10_000)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.fast = self.sma(self.spy, 50, Resolution.DAILY)
        self.slow = self.sma(self.spy, 200, Resolution.DAILY)
        self.set_warm_up(200, Resolution.DAILY)
        self.schedule.on(self.date_rules.every_day(self.spy),
                         self.time_rules.after_market_open(self.spy, 5),
                         self.rebalance)

    def rebalance(self):
        if self.is_warming_up or not self.slow.is_ready:
            return
        want_long = self.fast.current.value > self.slow.current.value
        if want_long and not self.portfolio[self.spy].invested:
            self.set_holdings(self.spy, 1.0)
        elif not want_long and self.portfolio[self.spy].invested:
            self.liquidate(self.spy)
