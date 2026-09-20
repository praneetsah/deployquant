from datetime import date

from dqengine.runtime.scheduling import DateRules, TimeRules, Schedule


class FakeCal:
    def is_first_of_week(self, d):
        return d.weekday() == 0

    def is_last_of_week(self, d):
        return d.weekday() == 4

    def is_first_of_month(self, d):
        return d.day == 1

    def is_last_of_month(self, d):
        return d.day == 31


def test_date_rules():
    cal = FakeCal()
    assert DateRules().every_day("TQQQ").matches(date(2026, 8, 26), cal)
    assert DateRules().every("Monday", "Friday").matches(date(2026, 8, 28), cal)  # Fri
    assert not DateRules().every("Monday").matches(date(2026, 8, 28), cal)
    assert DateRules().week_start().matches(date(2026, 8, 24), cal)               # Mon
    assert DateRules().month_end().matches(date(2026, 8, 31), cal)


def test_time_rules_fire_ms():
    OPEN, CLOSE = 9 * 3600_000 + 30 * 60_000, 16 * 3600_000
    assert TimeRules().at(10, 30).fire_ms(OPEN, CLOSE) == 10 * 3600_000 + 30 * 60_000
    assert TimeRules().after_market_open("TQQQ", 30).fire_ms(OPEN, CLOSE) == OPEN + 30 * 60_000
    assert TimeRules().before_market_close("TQQQ", 1).fire_ms(OPEN, CLOSE) == CLOSE - 60_000
    assert TimeRules().before_market_close(minutes=5).fire_ms(OPEN, CLOSE) == CLOSE - 300_000
    # someone wrote before_market_close(5): the int is minutes, not a symbol
    assert TimeRules().before_market_close(5).fire_ms(OPEN, CLOSE) == CLOSE - 300_000


def test_schedule_on_registers():
    sched = Schedule()
    fired = []
    ev = sched.on(DateRules().every_day(), TimeRules().at(15, 59), lambda: fired.append(1))
    assert sched.events == [ev]
    ev.callback()
    assert fired == [1]
