"""One definition of "the daily bar of this session".

A daily-resolution strategy reads a daily zip. The rows in that zip for a
recent day are derived from the minute bars the platform stored, so the
derivation has to be the same function everywhere and it has to survive a
round trip through the file: a daily bar that changes when it is written
and read back is a strategy that does not reproduce itself.
"""
import os
import zipfile
from datetime import date

import numpy as np
import pytest

from dqengine.runtime.core.data import (SCALE, DataStore, DayBars,
                                        derive_daily_row)


def _day(closes, volumes=None, day=date(2026, 8, 24)):
    c = np.array(closes, float)
    n = len(c)
    return DayBars(day=day,
                   start_ms=np.array([34_200_000 + i * 60_000 for i in range(n)]),
                   open=c - 0.5, high=c + 1.0, low=c - 1.0, close=c,
                   volume=np.array(volumes if volumes is not None
                                   else [1.0] * n, float))


def test_the_row_is_first_open_last_close_and_the_session_extremes():
    o, h, l, c, v = derive_daily_row(_day([100.0, 103.0, 101.0]))
    assert o == 99.5                      # first bar's open
    assert c == 101.0                     # last bar's close
    assert h == 104.0                     # max of close + 1
    assert l == 99.0                      # min of close - 1
    assert v == 3.0


def test_a_high_that_is_not_on_the_last_bar_still_wins():
    """The extremes are the session's, not the last bar's."""
    _, h, l, _, _ = derive_daily_row(_day([100.0, 150.0, 101.0]))
    assert h == 151.0 and l == 99.0


def test_volume_is_the_value_the_zip_can_store():
    """The zip writes volume with %g, six significant digits. Summing the
    minute volumes and keeping the exact sum would store one number and
    read back another."""
    _, _, _, _, v = derive_daily_row(_day([100.0, 101.0],
                                          volumes=[1_234_567.0, 1.0]))
    assert v == 1_234_570.0               # float("1.23457e+06")
    assert v != 1_234_568.0               # the unformatted sum


def test_the_row_round_trips_through_a_daily_zip(tmp_path):
    """Write the derived row the way the exporter writes it, read it back
    the way a daily backtest reads it, and get the same five numbers."""
    bars = _day([100.25, 101.5, 100.75], volumes=[9_876_543.0, 2.0, 3.0])
    o, h, l, c, v = derive_daily_row(bars)
    d = bars.day
    path = tmp_path / "equity" / "usa" / "daily" / "spy.zip"
    os.makedirs(path.parent, exist_ok=True)
    line = (f"{d.strftime('%Y%m%d')} 00:00,"
            f"{int(round(o * SCALE))},{int(round(h * SCALE))},"
            f"{int(round(l * SCALE))},{int(round(c * SCALE))},{v:g}")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("spy.csv", line)

    back = DataStore(str(tmp_path)).load_daily("SPY")
    assert back[d] == (o, h, l, c, v)


def test_a_session_with_no_bars_has_no_daily_row():
    """Zeros would put a price of 0 in front of the strategy."""
    empty = DayBars(day=date(2026, 8, 24), start_ms=np.array([]),
                    open=np.array([]), high=np.array([]), low=np.array([]),
                    close=np.array([]), volume=np.array([]))
    with pytest.raises(ValueError):
        derive_daily_row(empty)
    with pytest.raises(ValueError):
        derive_daily_row(None)
