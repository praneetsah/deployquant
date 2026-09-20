"""Calendar behaviour LEAN gets from its exchange-hours database and we get
from rules + data: early-close cut, week/month edges, holiday-aware next
session, sessions never run on projected days, store holes visible.

The holiday/early-close/projection rules at the bottom of this file arrived
in Phase 3 Task 9 from `tests/test_session_timing.py` and
`tests/test_calendar_projection.py`, which were deleted with the IR engine.
They never touched that engine: every one of them exercises
`dqengine/runtime/core/data.py`, which is shipped code on the live path --
`is_market_holiday` decides whether the worker ticks at all
(api/worker.py, api/live.py, api/broker_exec.py), `is_early_close` truncates
the bar export (api/pydata.py) and sets `close_time_ms`, and
`project_sessions` builds the live calendar horizon
(dqengine/runtime/backtester.py). Deleting them with their old files would have
dropped the only unit coverage those three have.
"""
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dqengine.runtime.core.data import (SessionCalendar, close_time_ms, day_bars_from_scaled,   # noqa: E402
                                  is_early_close, is_market_holiday,
                                  next_scheduled_session, project_sessions)
from dqengine.runtime.symbol import ExchangeHours                                        # noqa: E402

OPEN = (9 * 3600 + 30 * 60) * 1000


def _rows(day_ms_list):
    return [[ms, 1000000, 1000100, 999900, 1000000, 10.0] for ms in day_ms_list]


def test_early_close_day_is_cut_at_1300():
    thanksgiving_fri = date(2026, 11, 27)
    assert close_time_ms(thanksgiving_fri) == 13 * 3600 * 1000
    ms = [OPEN, 12 * 3600 * 1000 + 59 * 60000, 13 * 3600 * 1000, 15 * 3600 * 1000 + 59 * 60000]
    b = day_bars_from_scaled(thanksgiving_fri, _rows(ms))
    assert list(b.start_ms) == ms[:2], "13:00 onward is after-hours on a half day"
    normal = date(2026, 11, 30)
    assert day_bars_from_scaled(normal, _rows(ms)).n == 4


def test_first_of_week_at_the_stores_edge_asks_the_rules():
    wed = date(2026, 8, 19)                      # store starts on a Wednesday
    cal = SessionCalendar([wed, date(2026, 8, 20), date(2026, 8, 21)])
    assert cal.is_first_of_week(wed) is False
    assert cal.is_first_of_month(wed) is False
    mon = date(2026, 8, 17)
    assert SessionCalendar([mon, date(2026, 8, 18)]).is_first_of_week(mon) is True
    # Labor Day week: Tuesday IS first of week (Monday is a holiday)
    tue = date(2026, 9, 8)
    assert SessionCalendar([tue, date(2026, 9, 9)]).is_first_of_week(tue) is True
    # a month starting on a weekend: the first session is first_of_month
    assert SessionCalendar([date(2026, 8, 3), date(2026, 8, 4)]).is_first_of_month(date(2026, 8, 3))


def test_next_session_fallback_knows_holidays():
    assert next_scheduled_session(date(2026, 11, 25)) == date(2026, 11, 27)   # skips Thanksgiving
    assert next_scheduled_session(date(2026, 9, 4)) == date(2026, 9, 8)       # skips Labor Day
    eh = ExchangeHours(calendar=SessionCalendar([date(2026, 11, 24), date(2026, 11, 25)]))
    assert eh.get_next_trading_day(date(2026, 11, 25)).date() == date(2026, 11, 27)


def test_sessions_are_store_days_only_even_with_projection():
    """A projected day <= end with no bars must be a CALENDAR entry, never a
    session to run: running it fill-forwards a whole phantom day."""
    from conftest_helpers import SynthStore, synth_day
    from dqengine.runtime.warm import WarmPyEngine
    days = [date(2026, 8, 24), date(2026, 8, 25)]
    store = SynthStore({d: synth_day(d, [100, 101, 102]) for d in days})
    code = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 9, 4)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
    def on_data(self, data): pass
"""
    eng = WarmPyEngine(code, store=store, overrides={"start": "2026-08-24", "end": "2026-09-04",
                                                    "cash": 1000.0, "project_calendar": True})
    eng.warm(through=date(2026, 9, 4))
    assert eng._bt._sessions == days
    assert eng._bt._cal.days[-1] > days[-1], "the calendar IS projected"
    assert eng.snapshot()["calendar_holes"] == []


def test_a_store_hole_is_reported():
    from conftest_helpers import SynthStore, synth_day
    from dqengine.runtime.warm import WarmPyEngine
    days = [date(2026, 8, 24), date(2026, 8, 26)]          # Tuesday 8/25 missing
    store = SynthStore({d: synth_day(d, [100, 101, 102]) for d in days})
    code = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 26)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
    def on_data(self, data): pass
"""
    eng = WarmPyEngine(code, store=store, overrides={"start": "2026-08-24", "end": "2026-08-26",
                                                    "cash": 1000.0})
    eng.warm(through=date(2026, 8, 26))
    assert eng.snapshot()["calendar_holes"] == ["2026-08-25"]


# ===================================================================
# Holiday and early-close rules.
# From tests/test_session_timing.py (the CRITICAL 1 half-day regression)
# and tests/test_calendar_projection.py (the 2026-08-19 phantom-exit
# incident). Neither group ever constructed a Backtester.
# ===================================================================

def test_calendar_week_semantics():
    days = [date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6),
            date(2021, 1, 7), date(2021, 1, 8),          # full week
            date(2021, 1, 12), date(2021, 1, 13)]        # Mon 1/11 "holiday"
    cal = SessionCalendar(days)
    assert cal.is_first_of_week(date(2021, 1, 4))
    assert cal.is_last_of_week(date(2021, 1, 8))
    assert cal.is_day_before_last_of_week(date(2021, 1, 7))
    assert cal.is_first_of_week(date(2021, 1, 12))       # Tuesday after holiday


def test_is_early_close_black_friday():
    assert is_early_close(date(2024, 11, 29))     # day after Thanksgiving
    assert is_early_close(date(2025, 11, 28))


def test_is_early_close_christmas_eve_only_on_weekday():
    assert is_early_close(date(2024, 12, 24))      # Tuesday
    assert not is_early_close(date(2022, 12, 24))   # Saturday -> market closed anyway


def test_is_early_close_july_third_only_when_july_fourth_is_a_weekday():
    assert is_early_close(date(2024, 7, 3))         # July 4 2024 = Thursday
    assert not is_early_close(date(2021, 7, 3))     # July 4 2021 = Sunday


def test_is_early_close_false_on_an_ordinary_day():
    assert not is_early_close(date(2024, 6, 17))
    assert not is_early_close(date(2024, 11, 27))   # Wed before Thanksgiving: full day


def test_close_time_ms_matches_early_close_flag():
    assert close_time_ms(date(2024, 11, 29)) == 13 * 3600 * 1000
    assert close_time_ms(date(2024, 6, 17)) == 16 * 3600 * 1000


def test_fixed_date_holidays_observed():
    assert is_market_holiday(date(2026, 1, 1))       # New Year's (Thursday)
    assert is_market_holiday(date(2026, 7, 3))       # July 4 2026 = Saturday -> Friday
    assert not is_market_holiday(date(2026, 7, 4))   # the Saturday itself: weekend
    assert is_market_holiday(date(2026, 12, 25))     # Christmas (Friday)
    assert is_market_holiday(date(2027, 12, 24))     # Christmas 2027 = Saturday -> Friday
    assert is_market_holiday(date(2022, 6, 20))      # Juneteenth 2022 = Sunday -> Monday
    assert not is_market_holiday(date(2021, 6, 18))  # pre-2022: Juneteenth not observed


def test_floating_holidays():
    assert is_market_holiday(date(2026, 1, 19))      # MLK: 3rd Monday Jan
    assert is_market_holiday(date(2026, 2, 16))      # Washington's: 3rd Monday Feb
    assert is_market_holiday(date(2026, 5, 25))      # Memorial: last Monday May
    assert is_market_holiday(date(2026, 9, 7))       # Labor: 1st Monday Sep
    assert is_market_holiday(date(2026, 11, 26))     # Thanksgiving: 4th Thursday Nov
    assert is_market_holiday(date(2026, 4, 3))       # Good Friday 2026
    assert is_market_holiday(date(2024, 3, 29))      # Good Friday 2024
    assert not is_market_holiday(date(2026, 8, 20))  # ordinary Thursday


def test_new_years_on_saturday_is_not_observed():
    # NYSE rule: Jan 1 2022 fell on Saturday; Dec 31 2021 was a normal session
    assert not is_market_holiday(date(2021, 12, 31))
    assert not is_market_holiday(date(2022, 1, 1))


def test_project_sessions_skips_weekends_and_holidays():
    # from Wed 2026-08-19: Thu 20, Fri 21, Mon 24, ...
    out = project_sessions(date(2026, 8, 19), date(2026, 8, 25))
    assert out == [date(2026, 8, 20), date(2026, 8, 21),
                   date(2026, 8, 24), date(2026, 8, 25)]
    # across Labor Day 2026 (Mon Sep 7)
    out = project_sessions(date(2026, 9, 3), date(2026, 9, 9))
    assert out == [date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]


def test_projection_matches_real_juneteenth_week():
    # data really has 2024-06-17,18,20,21 (no 6/19); projection from Monday
    # must produce the same shape
    out = project_sessions(date(2024, 6, 17), date(2024, 6, 21))
    assert out == [date(2024, 6, 18), date(2024, 6, 20), date(2024, 6, 21)]


# ------------------------------------------- calendar semantics at horizon
# The 2026-08-19 incident: the live calendar's day list always ends at
# "now", so without projection `is_last_of_week` reads the data horizon as
# the week's end and fires day_before_last_of_week slots on the wrong day.

def _week_aug_2026(through_day: int) -> list:
    prev = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12),
            date(2026, 8, 13), date(2026, 8, 14)]
    cur = [date(2026, 8, d) for d in (17, 18, 19, 20, 21) if d <= through_day]
    return prev + cur


def _projected_cal(days: list) -> SessionCalendar:
    return SessionCalendar(
        days + project_sessions(days[-1], days[-1] + timedelta(days=14)))


def test_projected_calendar_kills_the_phantom_tuesday_thursday_exit():
    """The 2026-08-19 incident: data horizon = Wednesday must NOT make
    Tuesday read as day_before_last_of_week, nor Wednesday as last_of_week."""
    cal = _projected_cal(_week_aug_2026(19))
    assert not cal.is_day_before_last_of_week(date(2026, 8, 18))
    assert not cal.is_last_of_week(date(2026, 8, 19))
    # the real slots, visible through the projection:
    assert cal.is_day_before_last_of_week(date(2026, 8, 20))
    assert cal.is_last_of_week(date(2026, 8, 21))


def test_projected_calendar_fires_thursday_on_thursday():
    """Data horizon = Thursday: the Thursday 14:00 slot must be live TODAY
    (unfixed, it only fired retroactively on Friday morning)."""
    cal = _projected_cal(_week_aug_2026(20))
    assert cal.is_day_before_last_of_week(date(2026, 8, 20))
    assert not cal.is_last_of_week(date(2026, 8, 20))


def test_projected_calendar_keeps_friday_as_last_of_week():
    cal = _projected_cal(_week_aug_2026(21))
    assert cal.is_last_of_week(date(2026, 8, 21))
    assert cal.is_day_before_last_of_week(date(2026, 8, 20))
    assert not cal.is_day_before_last_of_week(date(2026, 8, 21))


def test_projected_calendar_holiday_friday_week():
    """Good Friday week 2026 (Apr 3 = holiday): Thursday Apr 2 is the true
    last session; Wednesday Apr 1 is day-before-last. Horizon = Wednesday."""
    days = [date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1)]
    cal = _projected_cal(days)
    assert cal.is_day_before_last_of_week(date(2026, 4, 1))
    assert not cal.is_last_of_week(date(2026, 4, 1))
    assert cal.is_last_of_week(date(2026, 4, 2))
