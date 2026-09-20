from bisect import bisect_right
from datetime import date, datetime, timedelta

from .aliases import PascalMixin, alias_methods
from .models import NullFeeModel, NullSlippageModel


class Symbol(str):
    """A ticker that IS its string — hashable, comparable, and interchangeable
    with plain-str keys everywhere (LEAN code mixes both freely)."""

    def __new__(cls, value):
        return super().__new__(cls, str(value).upper())

    @property
    def value(self) -> str:
        return str(self)

    @property
    def id(self) -> str:
        return str(self)

    # PascalCase spellings seen in the wild
    Value = value
    ID = id


@alias_methods
class ExchangeHours(PascalMixin):
    def __init__(self, calendar=None):
        self.calendar = calendar  # SessionCalendar-like with sorted .days

    def get_next_trading_day(self, d) -> datetime:
        if isinstance(d, datetime):
            d = d.date()
        cal = self.calendar
        if cal is not None and getattr(cal, "days", None):
            i = bisect_right(cal.days, d)
            if i < len(cal.days):
                nd = cal.days[i]
                return datetime(nd.year, nd.month, nd.day)
        # past the calendar's data (and its projection): the holiday rules,
        # not a bare weekday walk -- the latter made Thanksgiving Thursday a
        # session and shifted day_before_last_of_week to Wednesday
        from dqengine.runtime.core.data import next_scheduled_session
        nd = next_scheduled_session(d)
        return datetime(nd.year, nd.month, nd.day)


class Exchange(PascalMixin):
    def __init__(self, hours: ExchangeHours):
        self.hours = hours


@alias_methods
class Security(PascalMixin):
    def __init__(self, symbol: Symbol, resolution, exchange: Exchange):
        self.symbol = symbol
        self.resolution = resolution
        self.exchange = exchange
        self.price = 0.0
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume = 0.0
        self.leverage = None  # None = never set; backtester defaults to 1.0
        self.slippage_model = NullSlippageModel()
        self.fee_model = NullFeeModel()
        self.invested = False  # kept current by the backtester

    def set_leverage(self, x: float):
        self.leverage = float(x)

    def set_slippage_model(self, model):
        self.slippage_model = model

    def set_fee_model(self, model):
        self.fee_model = model

    def set_data_normalization_mode(self, *a, **k):
        pass  # our bars are already the adjusted series the platform serves
