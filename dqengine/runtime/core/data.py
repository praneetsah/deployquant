"""Bar data access over the LEAN-format zips under the data root.

Formats (verified against the local rig):
  minute: data/equity/usa/minute/{sym}/{YYYYMMDD}_trade.zip
          -> csv rows: ms_since_midnight_ET,O,H,L,C,V   (prices x10000)
  daily:  data/equity/usa/daily/{sym}.zip
          -> csv rows: "YYYYMMDD 00:00,O,H,L,C,V"       (prices x10000)

Regular session = bars with start time in [09:30, 16:00) ET. The engine trades the
regular session only (matches the LEAN backtests we validate against, which run
without extended hours).
"""
from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import numpy as np

REG_OPEN_MS = (9 * 3600 + 30 * 60) * 1000     # 09:30 ET
REG_CLOSE_MS = 16 * 3600 * 1000               # 16:00 ET
EARLY_CLOSE_MS = 13 * 3600 * 1000             # 13:00 ET, US equity half-days
SCALE = 10000.0


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the n-th `weekday` (Mon=0..Sun=6) in `year`-`month`."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def is_early_close(day: date) -> bool:
    """US equity market half-days (13:00 ET close). Rule-derived, not a
    hard-coded per-year list:
      - the day after Thanksgiving (Thanksgiving = 4th Thursday of November)
      - Christmas Eve (Dec 24), when it falls on a weekday
      - July 3, when July 4th falls on a weekday
    """
    year = day.year
    thanksgiving = _nth_weekday(year, 11, 3, 4)   # Thursday = weekday 3
    if day == thanksgiving + timedelta(days=1):
        return True
    xmas_eve = date(year, 12, 24)
    if day == xmas_eve and xmas_eve.weekday() < 5:
        return True
    july4 = date(year, 7, 4)
    if day == date(year, 7, 3) and july4.weekday() < 5:
        return True
    return False


def close_time_ms(day: date) -> int:
    """The session's actual close, in ms-since-midnight ET: 16:00 normally,
    13:00 on a known early-close day. Calendar-derived — never inferred from
    how much bar data happens to be present for `day` (that conflation is
    exactly what let early closes and truncated/partial data get confused
    for each other; see session-timing-and-realtime-design.md)."""
    return EARLY_CLOSE_MS if is_early_close(day) else REG_CLOSE_MS


def _easter(year: int) -> date:
    """Easter Sunday (Anonymous Gregorian computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


# Announced one-off full closures (mourning days etc.). Not rule-derivable:
# when the NYSE announces one, it must be added here (and redeployed) BEFORE
# the closure week starts, or week-boundary selectors will treat the closed
# day as a session and fire the Thursday-slot exits a day late.
ONE_OFF_CLOSURES = {
    date(2025, 1, 9),    # National Day of Mourning, President Carter
}


def is_market_holiday(day: date) -> bool:
    """US equity full-closure holidays: rule-derived like is_early_close,
    plus the announced ONE_OFF_CLOSURES above. Weekend-observance: Sat ->
    preceding Friday, Sun -> following Monday, except New Year's, which NYSE
    does not observe when Jan 1 is a Saturday (e.g. 2021-12-31 was a normal
    session)."""
    if day.weekday() >= 5:
        return False
    if day in ONE_OFF_CLOSURES:
        return True

    def observed(d: date):
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    y = day.year
    fixed = [observed(date(y, 7, 4)), observed(date(y, 12, 25))]
    if y >= 2022:
        fixed.append(observed(date(y, 6, 19)))          # Juneteenth
    ny = date(y, 1, 1)
    if ny.weekday() != 5:                               # Sat: not observed
        fixed.append(observed(ny))
    if day in fixed:
        return True
    floating = [
        _nth_weekday(y, 1, 0, 3),                       # MLK: 3rd Mon Jan
        _nth_weekday(y, 2, 0, 3),                       # Washington: 3rd Mon Feb
        _easter(y) - timedelta(days=2),                 # Good Friday
        date(y, 9, 1) + timedelta((0 - date(y, 9, 1).weekday()) % 7),  # Labor Day
        _nth_weekday(y, 11, 3, 4),                      # Thanksgiving
    ]
    mem = date(y, 5, 31)                                # Memorial: last Mon May
    floating.append(mem - timedelta(days=(mem.weekday() - 0) % 7))
    return day in floating


def _scheduled_session_earlier_in(d: date, unit: str) -> bool:
    """Is there a weekday-non-holiday session before `d` in the same ISO
    week / calendar month? Rule-derived, so it needs no data."""
    x = d - timedelta(days=1)
    while True:
        same = ((x.isocalendar()[:2] == d.isocalendar()[:2]) if unit == "week"
                else (x.year, x.month) == (d.year, d.month))
        if not same:
            return False
        if x.weekday() < 5 and not is_market_holiday(x):
            return True
        x -= timedelta(days=1)


def next_scheduled_session(d: date) -> date:
    """The first weekday-non-holiday after `d` -- the rule-derived fallback
    for calendars that run out of data (a bare weekday walk treated
    Thanksgiving Thursday as a session)."""
    x = d + timedelta(days=1)
    while x.weekday() >= 5 or is_market_holiday(x):
        x += timedelta(days=1)
    return x


def project_sessions(after: date, through: date) -> list[date]:
    """Scheduled trading sessions in (after, through]: weekdays that are not
    rule-derived holidays. Used to extend a data-derived SessionCalendar past
    the live data horizon so week-boundary selectors (last_of_week,
    day_before_last_of_week) see the week's real shape instead of treating
    "today" as the end of the week."""
    out = []
    d = after + timedelta(days=1)
    while d <= through:
        if d.weekday() < 5 and not is_market_holiday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def day_bars_from_scaled(day: date, rows) -> Optional["DayBars"]:
    """A DayBars from LEAN-scaled rows [ms, o, h, l, c, v] (prices in
    1/SCALE units, exactly what the zip holds). The ONE path from rows to
    arrays: the zip loader and the live driver's pushed bars both come
    through here, so a bar pushed over RPC is numerically the bar the
    replay reads from the zip."""
    rows = np.asarray(rows, dtype=np.float64)
    if rows.size == 0:
        return None
    rows = rows.reshape(-1, 6)
    ms = rows[:, 0]
    # the SESSION's close, not 16:00: on an early-close day (13:00 ET) both
    # consolidated-tape and broker chart feeds keep emitting after-hours candles
    # until 16:00, and treating them as regular bars fills resting orders
    # on after-hours prints and marks equity on a 15:59 after-hours close.
    # LEAN's exchange-hours database ends the day at 13:00; so do we.
    rows = rows[(ms >= REG_OPEN_MS) & (ms < close_time_ms(day))]
    if len(rows) == 0:
        return None
    return DayBars(
        day=day,
        start_ms=rows[:, 0].astype(np.int64),
        open=rows[:, 1] / SCALE,
        high=rows[:, 2] / SCALE,
        low=rows[:, 3] / SCALE,
        close=rows[:, 4] / SCALE,
        volume=rows[:, 5],
    )


@dataclass
class DayBars:
    """One symbol-day of regular-session minute bars (numpy columns)."""
    day: date
    # per-bar arrays, aligned; times are bar START in ms since midnight ET
    start_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    @property
    def n(self) -> int:
        return len(self.start_ms)

    @property
    def session_open(self) -> float:
        return float(self.open[0])

    @property
    def session_close(self) -> float:
        return float(self.close[-1])


def derive_daily_row(bars: DayBars) -> tuple[float, float, float, float, float]:
    """The session's daily bar from its minute bars: (o, h, l, c, v).

    One definition, used by everything that turns minute bars into a daily
    row, so a live day and a later backtest of the same day read the same
    numbers. Open is the first bar's open, close the last bar's close, high
    and low the session extremes, volume the sum.

    Volume goes through `%g` because that is how the daily zip writes it
    (six significant digits). A caller that summed the volume itself and
    wrote it unformatted would store one number and read back another, and
    a daily bar that changes when it is round-tripped is a strategy that
    does not reproduce itself.

    Raises on an empty session: there is no daily bar for a day with no
    bars, and returning zeros would put a price of 0 in front of a
    strategy."""
    if bars is None or not bars.n:
        raise ValueError("no bars to derive a daily row from")
    return (float(bars.open[0]), float(bars.high.max()), float(bars.low.min()),
            float(bars.close[-1]), float(f"{float(bars.volume.sum()):g}"))


class DataStore:
    def __init__(self, data_root: str):
        self.root = data_root

    # ---------- minute ----------

    def minute_days(self, symbol: str) -> list[date]:
        d = os.path.join(self.root, "equity", "usa", "minute", symbol.lower())
        if not os.path.isdir(d):
            return []
        out = []
        for f in sorted(os.listdir(d)):
            if f.endswith("_trade.zip"):
                out.append(datetime.strptime(f[:8], "%Y%m%d").date())
        return out

    def load_minute_day(self, symbol: str, day: date) -> Optional[DayBars]:
        path = os.path.join(self.root, "equity", "usa", "minute", symbol.lower(),
                            f"{day.strftime('%Y%m%d')}_trade.zip")
        if not os.path.exists(path):
            return None
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0])
        rows = np.loadtxt(io.BytesIO(raw), delimiter=",",
                          dtype=np.float64, ndmin=2)
        return day_bars_from_scaled(day, rows)

    # ---------- daily ----------

    def load_daily(self, symbol: str) -> dict[date, tuple[float, float, float, float, float]]:
        """{day: (o, h, l, c, v)} from the daily zip."""
        path = os.path.join(self.root, "equity", "usa", "daily", f"{symbol.lower()}.zip")
        out: dict[date, tuple[float, float, float, float, float]] = {}
        if not os.path.exists(path):
            return out
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0]).decode()
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split(",")
            d = datetime.strptime(parts[0].split(" ")[0], "%Y%m%d").date()
            out[d] = (float(parts[1]) / SCALE, float(parts[2]) / SCALE,
                      float(parts[3]) / SCALE, float(parts[4]) / SCALE, float(parts[5]))
        return out


class SessionCalendar:
    """Trading-day calendar derived from the union of available data days.

    Week semantics use ISO calendar weeks over *trading* days, matching the
    the reference strategy's isocalendar logic (a Monday holiday makes Tuesday first_of_week).
    """

    def __init__(self, days: list[date]):
        self.days = sorted(set(days))
        self._index = {d: i for i, d in enumerate(self.days)}

    @staticmethod
    def _week_key(d: date) -> int:
        iso = d.isocalendar()
        return iso[0] * 100 + iso[1]

    def is_first_of_week(self, d: date) -> bool:
        i = self._index[d]
        if i == 0:
            # the store's first day: no neighbour to compare, so ask the
            # RULES -- is there a scheduled session earlier in this ISO
            # week? (LEAN's WeekStart never fires on a Wednesday.)
            return not _scheduled_session_earlier_in(d, "week")
        return self._week_key(self.days[i - 1]) != self._week_key(d)

    def is_last_of_week(self, d: date) -> bool:
        i = self._index[d]
        if i == len(self.days) - 1:
            return True
        return self._week_key(self.days[i + 1]) != self._week_key(d)

    def is_day_before_last_of_week(self, d: date) -> bool:
        i = self._index[d]
        if i >= len(self.days) - 1:
            return False
        nxt = self.days[i + 1]
        if self._week_key(nxt) != self._week_key(d):
            return False          # d itself is last of week
        return self.is_last_of_week(nxt)

    @staticmethod
    def is_early_close(d: date) -> bool:
        return is_early_close(d)

    @staticmethod
    def close_time_ms(d: date) -> int:
        return close_time_ms(d)

    def is_first_of_month(self, d: date) -> bool:
        i = self._index[d]
        if i == 0:
            return not _scheduled_session_earlier_in(d, "month")
        return self.days[i - 1].month != d.month

    def is_last_of_month(self, d: date) -> bool:
        i = self._index[d]
        return i == len(self.days) - 1 or self.days[i + 1].month != d.month

    def matches(self, d: date, days_sel: str) -> bool:
        if days_sel == "all":
            return True
        if days_sel == "first_of_week":
            return self.is_first_of_week(d)
        if days_sel == "last_of_week":
            return self.is_last_of_week(d)
        if days_sel == "day_before_last_of_week":
            return self.is_day_before_last_of_week(d)
        if days_sel == "first_of_month":
            return self.is_first_of_month(d)
        if days_sel == "last_of_month":
            return self.is_last_of_month(d)
        raise ValueError(f"unknown days selector: {days_sel}")
