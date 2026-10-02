from dataclasses import dataclass
from enum import Enum


class Resolution(Enum):
    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAILY = "daily"
    # LEAN's python accepts both spellings (Resolution.Minute is the older,
    # still common one). Same value = an alias: Resolution.Minute IS
    # Resolution.MINUTE, so every comparison in the engine holds unchanged.
    Second = "second"
    Minute = "minute"
    Hour = "hour"
    Daily = "daily"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop"
    STOP_LIMIT = "stop_limit"
    MARKET_ON_OPEN = "market_on_open"
    MARKET_ON_CLOSE = "market_on_close"
    TRAILING_STOP = "trailing_stop"
    LIMIT_IF_TOUCHED = "limit_if_touched"
    Market = "market"
    Limit = "limit"
    StopMarket = "stop"
    StopLimit = "stop_limit"
    MarketOnOpen = "market_on_open"
    MarketOnClose = "market_on_close"
    TrailingStop = "trailing_stop"
    LimitIfTouched = "limit_if_touched"


class OrderStatus(Enum):
    NEW = "new"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELED = "canceled"
    INVALID = "invalid"
    UPDATE_SUBMITTED = "update_submitted"
    New = "new"
    Submitted = "submitted"
    Filled = "filled"
    Canceled = "canceled"
    Invalid = "invalid"
    UpdateSubmitted = "update_submitted"


class OrderDirection(Enum):
    BUY = "buy"
    SELL = "sell"
    Buy = "buy"
    Sell = "sell"


class _AnyName(type):
    """BrokerageName.WEBULL, .TD_AMERITRADE, .whatever — all valid, all just
    the name string. These are accepted-and-ignored settings in v1; failing
    on an unknown broker constant would break pasted code for no reason."""

    def __getattr__(cls, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return name


class BrokerageName(metaclass=_AnyName):
    pass


class AccountType(metaclass=_AnyName):
    pass


class DataNormalizationMode(metaclass=_AnyName):
    pass


class DayOfWeek(metaclass=_AnyName):
    pass


class MovingAverageType(metaclass=_AnyName):
    """Just the name: each indicator that takes one decides what it
    implements (`rsi` is Wilder's only) and refuses the rest by name."""


@dataclass
class UpdateOrderFields:
    quantity: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    tag: str | None = None
