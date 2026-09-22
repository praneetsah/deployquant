"""Bar hygiene: what reaches the zip, in what encoding, and what does not.

The same rules the hosted platform's bar store earned the hard way — a
whole frame at a time, duplicates silent, after-hours candles on a
half-day never stored as regular bars, volumes written as whole numbers.
"""
import zipfile

from conftest import et

from dqengine.feeds.base import MinuteBar, MinuteZipStore
from dqengine.runtime.core.data import DataStore
from dqengine.store import minute_zip_path

DAY = et(2026, 9, 18).date()            # a normal Friday session
HALF = et(2026, 11, 27).date()          # the day after Thanksgiving: 13:00 close


def bar(symbol="TQQQ", day=DAY, start_ms=(9 * 3600 + 31 * 60) * 1000,
        o=10.0, h=11.0, l=9.5, c=10.5, v=1000.0):
    return MinuteBar(symbol, day, start_ms, o, h, l, c, v)


def test_a_written_bar_reads_back_through_the_engines_own_store(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    written = store.write([bar(), bar(start_ms=(9 * 3600 + 32 * 60) * 1000)])
    assert [b.start_ms for b in written] == [34260000, 34320000]
    day_bars = DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY)
    assert day_bars.n == 2
    assert list(day_bars.close) == [10.5, 10.5]


def test_an_identical_resend_writes_nothing_and_announces_nothing(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    assert len(store.write([bar()])) == 1
    path = minute_zip_path(str(tmp_path), "TQQQ", DAY)
    before = open(path, "rb").read()
    assert store.write([bar()]) == []
    assert open(path, "rb").read() == before


def test_a_corrected_minute_replaces_the_stored_one_and_is_announced(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    store.write([bar()])
    written = store.write([bar(c=10.9)])
    assert [b.close for b in written] == [10.9]
    assert list(DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY).close) == [10.9]


def test_a_whole_volume_is_written_as_an_integer(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    store.write([bar(v=1270160.0)])
    path = minute_zip_path(str(tmp_path), "TQQQ", DAY)
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode()
    assert text.strip().endswith(",1270160")
    assert "e+" not in text


def test_an_after_hours_bar_on_an_early_close_day_is_not_stored(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    written = store.write([bar(day=HALF, start_ms=(12 * 3600 + 59 * 60) * 1000),
                           bar(day=HALF, start_ms=(13 * 3600 + 5 * 60) * 1000)])
    assert [b.start_ms for b in written] == [46740000]
    assert DataStore(str(tmp_path)).load_minute_day("TQQQ", HALF).n == 1


def test_a_pre_open_bar_is_not_stored(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    assert store.write([bar(start_ms=9 * 3600 * 1000)]) == []
    assert DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY) is None


def test_a_frame_spanning_two_symbols_writes_both_days(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    written = store.write([bar("TQQQ"), bar("QQQ", c=400.0, o=399.0, h=401.0, l=398.0)])
    assert {b.symbol for b in written} == {"TQQQ", "QQQ"}
    assert DataStore(str(tmp_path)).load_minute_day("QQQ", DAY).n == 1


def test_an_existing_zip_is_read_before_it_is_extended(tmp_path):
    """A restarted feed must not truncate the day it rejoins."""
    MinuteZipStore(str(tmp_path)).write([bar()])
    fresh = MinuteZipStore(str(tmp_path))
    fresh.write([bar(start_ms=(9 * 3600 + 32 * 60) * 1000)])
    assert DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY).n == 2
    # and the bar it never saw is still recognised as a duplicate
    assert fresh.write([bar()]) == []
