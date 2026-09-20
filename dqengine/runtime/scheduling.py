from dataclasses import dataclass, field
from datetime import date

from .aliases import PascalMixin, alias_methods
from .errors import unsupported

_DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
              "friday": 4, "saturday": 5, "sunday": 6}


def _day_index(d) -> int:
    if isinstance(d, int):
        return d
    return _DAY_NAMES[str(d).lower()]


@dataclass
class DateRule:
    kind: str
    days: tuple = ()

    def matches(self, day: date, cal) -> bool:
        if self.kind == "every_day":
            return True
        if self.kind == "every":
            return day.weekday() in self.days
        if self.kind == "week_start":
            return cal.is_first_of_week(day)
        if self.kind == "week_end":
            return cal.is_last_of_week(day)
        if self.kind == "month_start":
            return cal.is_first_of_month(day)
        if self.kind == "month_end":
            return cal.is_last_of_month(day)
        raise ValueError(self.kind)


def _no_offset(days_offset):
    if days_offset:
        unsupported("date_rules with days_offset")


@alias_methods
class DateRules(PascalMixin):
    # every method takes an optional leading symbol arg (ignored — one US
    # equity calendar) to match LEAN call shapes like every_day(self.symbol).
    def every_day(self, symbol=None) -> DateRule:
        return DateRule("every_day")

    def every(self, *days) -> DateRule:
        return DateRule("every", tuple(_day_index(d) for d in days))

    def week_start(self, symbol=None, days_offset=0) -> DateRule:
        _no_offset(days_offset)
        return DateRule("week_start")

    def week_end(self, symbol=None, days_offset=0) -> DateRule:
        _no_offset(days_offset)
        return DateRule("week_end")

    def month_start(self, symbol=None, days_offset=0) -> DateRule:
        _no_offset(days_offset)
        return DateRule("month_start")

    def month_end(self, symbol=None, days_offset=0) -> DateRule:
        _no_offset(days_offset)
        return DateRule("month_end")


@dataclass
class TimeRule:
    kind: str
    minutes: float = 0
    at_ms: int | None = None

    def fire_ms(self, open_ms: int, close_ms: int) -> int:
        if self.kind == "at":
            return self.at_ms
        if self.kind == "after_open":
            return int(open_ms + self.minutes * 60_000)
        if self.kind == "before_close":
            return int(close_ms - self.minutes * 60_000)
        raise ValueError(self.kind)


@alias_methods
class TimeRules(PascalMixin):
    def at(self, hour, minute=0, second=0) -> TimeRule:
        return TimeRule("at", at_ms=(hour * 3600 + minute * 60 + second) * 1000)

    def after_market_open(self, symbol=None, minutes=0, extended_market_open=False) -> TimeRule:
        if isinstance(symbol, (int, float)):  # after_market_open(30)
            minutes = symbol
        return TimeRule("after_open", minutes=minutes)

    def before_market_close(self, symbol=None, minutes=0, extended_market_close=False) -> TimeRule:
        if isinstance(symbol, (int, float)):  # before_market_close(5)
            minutes = symbol
        return TimeRule("before_close", minutes=minutes)


@dataclass
class ScheduledEvent:
    date_rule: DateRule
    time_rule: TimeRule
    callback: object
    name: str = ""


@alias_methods
class Schedule(PascalMixin):
    def __init__(self):
        self.events: list[ScheduledEvent] = []

    def on(self, date_rule: DateRule, time_rule: TimeRule, callback, name=None) -> ScheduledEvent:
        ev = ScheduledEvent(date_rule, time_rule, callback,
                            name or getattr(callback, "__name__", ""))
        self.events.append(ev)
        return ev
