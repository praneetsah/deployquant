from AlgorithmImports import *


class Count(QCAlgorithm):
    """Does nothing but count calls: proves every bar reaches on_data."""
    def initialize(self):
        self.set_start_date(2021, 1, 4); self.set_end_date(2026, 6, 9)
        self.set_cash(1_000_000)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.n = 0; self.last = None

    def on_data(self, data):
        self.n += 1
        self.last = data[self.spy].close if self.spy in data else self.last

    def on_end_of_algorithm(self):
        self.log(f"ON_DATA_CALLS={self.n} last_close={self.last}")
