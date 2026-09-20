"""Buy the dip, with a stop: enter QQQ when its 14-day RSI closes under 30,
exit when it recovers above 55 or the position is down 8%.

    dqengine data fetch QQQ --from 2019-06-01
    dqengine example rsi_dip
    dqengine backtest rsi_dip.py
"""
from AlgorithmImports import *


class RsiDip(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2020, 1, 2)
        self.set_end_date(2025, 12, 31)
        self.set_cash(10_000)
        self.qqq = self.add_equity("QQQ", Resolution.MINUTE).symbol
        self.rsi14 = self.rsi(self.qqq, 14, resolution=Resolution.DAILY)
        self.set_warm_up(30, Resolution.DAILY)
        self.schedule.on(self.date_rules.every_day(self.qqq),
                         self.time_rules.before_market_close(self.qqq, 10),
                         self.decide)

    def decide(self):
        if self.is_warming_up or not self.rsi14.is_ready:
            return
        held = self.portfolio[self.qqq]
        value = self.rsi14.current.value
        if not held.invested and value < 30:
            self.set_holdings(self.qqq, 0.95)
        elif held.invested and (value > 55 or held.unrealized_profit_percent < -0.08):
            self.liquidate(self.qqq)
