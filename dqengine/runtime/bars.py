from dataclasses import dataclass
from datetime import datetime

from .aliases import PascalMixin, alias_methods


@dataclass
class TradeBar(PascalMixin):
    symbol: str
    time: datetime       # bar START
    end_time: datetime   # bar END — what LEAN hands on_data at
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def price(self) -> float:
        return self.close


@alias_methods
class Bars(dict, PascalMixin):
    """dict keyed by upper-case ticker strings; lookups accept Symbol or str."""

    def __init__(self, items=None):
        super().__init__()
        for k, v in (items or {}).items():
            dict.__setitem__(self, str(k).upper(), v)

    def __getitem__(self, key):
        return dict.__getitem__(self, str(key).upper())

    def __contains__(self, key):
        return dict.__contains__(self, str(key).upper())

    def get(self, key, default=None):
        return dict.get(self, str(key).upper(), default)

    def contains_key(self, key) -> bool:
        return key in self


@alias_methods
class Slice(PascalMixin):
    def __init__(self, bars: Bars, time: datetime):
        self.bars = bars
        self.time = time

    def __getitem__(self, key):
        return self.bars[key]

    def __contains__(self, key):
        return key in self.bars

    def contains_key(self, key) -> bool:
        return key in self.bars

    def get(self, key, default=None):
        return self.bars.get(key, default)

    def keys(self):
        return self.bars.keys()
