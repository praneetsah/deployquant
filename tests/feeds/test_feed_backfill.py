"""The fallback a worker takes when the silence check fires: fetch today's
minutes over REST and write exactly the ones the stream never delivered."""
from conftest import et

from dqengine.feeds.backfill import backfill_day
from dqengine.feeds.base import MinuteBar, MinuteZipStore
from dqengine.runtime.core.data import DataStore

DAY = et(2026, 9, 18).date()
HALF = et(2026, 11, 27).date()


def ms(hour, minute):
    return (hour * 3600 + minute * 60) * 1000


def row(hour, minute, c=10.5):
    return [ms(hour, minute), 10.0, 11.0, 9.5, c, 1000.0]


class FakeBarFeed:
    """A BarFeed double: {symbol: {day: rows}}, or an exception to raise."""

    def __init__(self, days, fail=()):
        self.days = days
        self.fail = set(fail)
        self.calls = []

    def fetch_days(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            raise RuntimeError("data API 500")
        return self.days.get(symbol, {})


def test_only_the_minutes_the_stream_missed_are_written_and_reported(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    store.write([MinuteBar("TQQQ", DAY, ms(9, 31), 10.0, 11.0, 9.5, 10.5, 1000.0),
                 MinuteBar("TQQQ", DAY, ms(9, 32), 10.0, 11.0, 9.5, 10.5, 1000.0)])
    feed = FakeBarFeed({"TQQQ": {DAY: [row(9, 31), row(9, 32), row(9, 33),
                                       row(9, 34)]}})
    filled = backfill_day(feed, store, ["tqqq"], DAY, now_ms=ms(9, 40), log=lambda _: None)
    assert [b.start_ms for b in filled["TQQQ"]] == [ms(9, 33), ms(9, 34)]
    assert DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY).n == 4


def test_a_day_the_stream_never_touched_is_filled_whole(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    feed = FakeBarFeed({"TQQQ": {DAY: [row(9, 31), row(9, 32)]}})
    filled = backfill_day(feed, store, ["TQQQ"], DAY, now_ms=ms(10, 0), log=lambda _: None)
    assert len(filled["TQQQ"]) == 2
    assert feed.calls == [("TQQQ", DAY, DAY)]


def test_the_in_progress_minute_is_never_written(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    feed = FakeBarFeed({"TQQQ": {DAY: [row(9, 31), row(9, 32)]}})
    # 09:32:30 — the 09:32 bar has not closed yet
    filled = backfill_day(feed, store, ["TQQQ"], DAY,
                          now_ms=ms(9, 32) + 30_000, log=lambda _: None)
    assert [b.start_ms for b in filled["TQQQ"]] == [ms(9, 31)]


def test_an_after_hours_minute_on_a_half_day_is_refused(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    feed = FakeBarFeed({"TQQQ": {HALF: [row(12, 59), row(13, 5)]}})
    filled = backfill_day(feed, store, ["TQQQ"], HALF, now_ms=ms(15, 0),
                          log=lambda _: None)
    assert [b.start_ms for b in filled["TQQQ"]] == [ms(12, 59)]


def test_a_malformed_row_is_dropped_loudly(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    lines = []
    bad = [ms(9, 31), 539.0, 72.55, 72.65, 72.40, 73.0]     # low above high
    feed = FakeBarFeed({"TQQQ": {DAY: [bad, row(9, 32)]}})
    filled = backfill_day(feed, store, ["TQQQ"], DAY, now_ms=ms(10, 0),
                          log=lines.append)
    assert [b.start_ms for b in filled["TQQQ"]] == [ms(9, 32)]
    assert any("dropped malformed TQQQ bar" in x for x in lines)


def test_one_symbols_failure_never_stops_the_rest(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    lines = []
    feed = FakeBarFeed({"QQQ": {DAY: [row(9, 31)]}}, fail=["TQQQ"])
    filled = backfill_day(feed, store, ["TQQQ", "QQQ"], DAY, now_ms=ms(10, 0),
                          log=lines.append)
    assert set(filled) == {"QQQ"}
    assert any("backfill of TQQQ" in x for x in lines)


def test_a_store_failure_is_reported_not_raised(tmp_path):
    class Broken:
        def write(self, bars):
            raise OSError("disk full")

    lines = []
    feed = FakeBarFeed({"TQQQ": {DAY: [row(9, 31)]}})
    assert backfill_day(feed, Broken(), ["TQQQ"], DAY, now_ms=ms(10, 0),
                        log=lines.append) == {}
    assert any("backfill store of TQQQ" in x for x in lines)


def test_nothing_to_fill_reports_nothing(tmp_path):
    store = MinuteZipStore(str(tmp_path))
    store.write([MinuteBar("TQQQ", DAY, ms(9, 31), 10.0, 11.0, 9.5, 10.5, 1000.0)])
    feed = FakeBarFeed({"TQQQ": {DAY: [row(9, 31)]}})
    assert backfill_day(feed, store, ["TQQQ"], DAY, now_ms=ms(10, 0),
                        log=lambda _: None) == {}
