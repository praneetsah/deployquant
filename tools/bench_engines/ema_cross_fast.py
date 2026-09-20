from AlgorithmImports import *


class EmaCross(QCAlgorithm):
    """Same decisions as NautilusTrader's shipped EMACross example: always in the
    market, long 100 while fast EMA >= slow EMA, short 100 otherwise."""
    def initialize(self):
        self.set_start_date(2021, 1, 4)
        self.set_end_date(2026, 6, 9)
        self.set_cash(1_000_000)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.fast = self.ema(self.spy, 10, resolution=Resolution.MINUTE)
        self.slow = self.ema(self.spy, 20, resolution=Resolution.MINUTE)

    def on_data(self, data):
        if not (self.fast.is_ready and self.slow.is_ready):
            return
        qty = self.portfolio[self.spy].quantity
        want = 100 if self.fast.current.value >= self.slow.current.value else -100
        if qty != want:
            self.market_order(self.spy, want - qty)
