"""The bar cache's bookkeeping: coverage, checkpoints, the row shape, and
what a REST refresh is allowed to put in `bar_days`.

The HTTP is the client's and is tested with the client
(tests/feeds/test_bar_feed.py). What is tested here is what makes a second
run of the same range cost nothing, what an interrupted one resumes from,
and the hygiene the refresh applies to a day before it is stored.
"""
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dqengine.live import history, persistence
from dqengine.live.history import (cache_rows, coverage_key, ensure_history,
                                   load_coverage, merge_intervals,
                                   missing_ranges, save_coverage)

ET = ZoneInfo("America/New_York")
D = date(2024, 6, 17)


class FakeFeed:
    """A BarFeed that answers from a script and records what it was asked."""

    def __init__(self, days_by_range=None, raise_on=None):
        self.days_by_range = days_by_range or {}
        self.raise_on = raise_on or set()
        self.asks = []

    def fetch_days(self, symbol, start, end, progress=None):
        self.asks.append((symbol, start, end))
        if start in self.raise_on:
            raise RuntimeError("vendor down")
        days = self.days_by_range.get((start, end), {})
        if progress and days:
            progress(max(days))
        return days


def _rows(n=2, v=1000):
    return [[34200000 + i * 60000, 10.0, 10.5, 9.5, 10.2, v]
            for i in range(n)]


# ------------------------------------------------------------ pure intervals

def test_merge_intervals_merges_overlap_and_adjacent():
    ivs = [["2021-01-01", "2021-06-01"], ["2021-06-02", "2021-12-31"],
           ["2023-01-01", "2023-02-01"], ["2021-03-01", "2021-04-01"]]
    assert merge_intervals(ivs) == [["2021-01-01", "2021-12-31"],
                                    ["2023-01-01", "2023-02-01"]]


def test_missing_ranges_is_the_whole_span_when_nothing_is_known():
    assert missing_ranges(date(2021, 1, 4), date(2021, 2, 1), []) == [
        (date(2021, 1, 4), date(2021, 2, 1))]


def test_missing_ranges_subtracts_what_is_known():
    known = [["2021-01-10", "2021-01-20"]]
    assert missing_ranges(date(2021, 1, 4), date(2021, 2, 1), known) == [
        (date(2021, 1, 4), date(2021, 1, 9)),
        (date(2021, 1, 21), date(2021, 2, 1))]


def test_missing_ranges_is_empty_when_covered():
    known = [["2020-12-01", "2022-01-01"]]
    assert missing_ranges(date(2021, 1, 4), date(2021, 2, 1), known) == []


def test_missing_ranges_walks_several_known_islands():
    known = [["2021-01-06", "2021-01-08"], ["2021-01-15", "2021-01-18"]]
    assert missing_ranges(date(2021, 1, 4), date(2021, 1, 20), known) == [
        (date(2021, 1, 4), date(2021, 1, 5)),
        (date(2021, 1, 9), date(2021, 1, 14)),
        (date(2021, 1, 19), date(2021, 1, 20))]


# ---------------------------------------------------------------- row shape

def test_a_whole_volume_stays_whole_and_a_price_stays_a_float():
    """1000 and 1000.0 are the same number and a different row: this cache
    has always held whole share counts, and rewriting every stored day to
    float on the next refetch buys nothing."""
    row = cache_rows([[1, 10.0, 11.0, 9.0, 10.5, 1000.0]])[0]
    assert row == [1, 10.0, 11.0, 9.0, 10.5, 1000]
    assert [type(x) for x in row] == [int, float, float, float, float, int]
    frac = cache_rows([[1, 10.0, 11.0, 9.0, 10.5, 12.5]])[0]
    assert frac[5] == 12.5 and isinstance(frac[5], float), \
        "a fractional volume is left alone"


# ------------------------------------------------------------ the KV ledger

def test_coverage_round_trips_under_the_symbols_key(pg):
    assert coverage_key("tqqq") == "histcov:TQQQ"
    assert load_coverage("histcov:TQQQ") == []
    save_coverage("histcov:TQQQ", [["2024-01-01", "2024-01-31"]])
    assert load_coverage("histcov:TQQQ") == [["2024-01-01", "2024-01-31"]]
    save_coverage("histcov:TQQQ", [["2024-01-01", "2024-02-28"]])
    assert load_coverage("histcov:TQQQ") == [["2024-01-01", "2024-02-28"]]


# ------------------------------------------------------------- the backfill

def test_a_gap_is_fetched_once_and_never_again(pg):
    feed = FakeFeed({(D, D): {D: _rows()}})
    assert ensure_history("TQQQ", D, D, [], feed=feed) == 1
    assert load_coverage("histcov:TQQQ") == [["2024-06-17", "2024-06-17"]]
    assert ensure_history("TQQQ", D, D, [], feed=feed) == 0
    assert len(feed.asks) == 1, "the second ask is pure cache"
    with pg() as s:
        rec = s.get(persistence.HistBar, ("TQQQ", D))
    assert rec.rows == [[34200000, 10.0, 10.5, 9.5, 10.2, 1000],
                        [34260000, 10.0, 10.5, 9.5, 10.2, 1000]]
    assert all(isinstance(r[5], int) for r in rec.rows), \
        "the stored shape, not just the stored number"


def test_a_day_with_one_bar_is_not_stored(pg):
    feed = FakeFeed({(D, D): {D: _rows(1)}})
    assert ensure_history("TQQQ", D, D, [], feed=feed) == 0
    with pg() as s:
        assert s.get(persistence.HistBar, ("TQQQ", D)) is None


def test_an_empty_range_is_still_recorded_as_covered(pg):
    feed = FakeFeed({(D, D): {}})
    assert ensure_history("TQQQ", D, D, [], feed=feed) == 0
    assert load_coverage("histcov:TQQQ") == [["2024-06-17", "2024-06-17"]]


def test_a_refetch_replaces_the_day_and_stamps_it(pg):
    feed = FakeFeed({(D, D): {D: _rows()}})
    ensure_history("TQQQ", D, D, [], feed=feed)
    save_coverage("histcov:TQQQ", [])
    feed.days_by_range[(D, D)] = {D: [[34200000, 9.0, 9.0, 9.0, 9.0, 5],
                                      [34260000, 9.0, 9.0, 9.0, 9.0, 5]]}
    ensure_history("TQQQ", D, D, [], feed=feed)
    with pg() as s:
        rec = s.get(persistence.HistBar, ("TQQQ", D))
    assert [r[1] for r in rec.rows] == [9.0, 9.0]
    assert rec.fetched_at is not None


def test_only_the_uncovered_islands_are_asked_for(pg):
    save_coverage("histcov:TQQQ", [["2024-06-10", "2024-06-12"]])
    feed = FakeFeed({(date(2024, 6, 13), date(2024, 6, 14)): {}})
    ensure_history("TQQQ", date(2024, 6, 10), date(2024, 6, 14), [],
                   feed=feed)
    assert feed.asks == [("TQQQ", date(2024, 6, 13), date(2024, 6, 14))]
    assert load_coverage("histcov:TQQQ") == [["2024-06-10", "2024-06-14"]]


def test_curated_days_count_as_covered_but_are_not_written_to_the_ledger(pg):
    feed = FakeFeed()
    assert ensure_history("TQQQ", D, D + timedelta(days=1), [D,
                          D + timedelta(days=1)], feed=feed) == 0
    assert feed.asks == []
    assert load_coverage("histcov:TQQQ") == [], \
        "the ledger records what the vendor was asked for, nothing else"


def test_a_failed_gap_keeps_the_finished_gaps_checkpoint(pg):
    save_coverage("histcov:TQQQ", [["2024-06-12", "2024-06-12"]])
    first = (date(2024, 6, 10), date(2024, 6, 11))
    feed = FakeFeed({first: {date(2024, 6, 11): _rows()}},
                    raise_on={date(2024, 6, 13)})
    with pytest.raises(RuntimeError):
        ensure_history("TQQQ", date(2024, 6, 10), date(2024, 6, 14), [],
                       feed=feed)
    assert len(feed.asks) == 2
    assert load_coverage("histcov:TQQQ") == [["2024-06-10", "2024-06-12"]], \
        "an interrupted backfill resumes instead of refetching what it did"


def test_a_range_ending_in_the_future_is_clamped_to_yesterday(pg):
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date()
    feed = FakeFeed()
    assert ensure_history("TQQQ", today, today + timedelta(days=5), [],
                          feed=feed) == 0
    assert feed.asks == [], "today's session is not history yet"


def test_progress_counts_days_against_every_gap(pg):
    save_coverage("histcov:TQQQ", [["2024-06-12", "2024-06-12"]])
    feed = FakeFeed({(date(2024, 6, 10), date(2024, 6, 11)):
                     {date(2024, 6, 11): _rows()},
                     (date(2024, 6, 13), date(2024, 6, 14)):
                     {date(2024, 6, 14): _rows()}})
    seen = []
    ensure_history("TQQQ", date(2024, 6, 10), date(2024, 6, 14), [],
                   lambda done, total: seen.append((done, total)), feed=feed)
    assert seen == [(2, 4), (4, 4)], \
        "day-granular, against the total of every gap, not of one"


# ------------------------------------------------------------- credentials

def test_a_covered_range_never_asks_for_a_credential(pg, monkeypatch):
    save_coverage("histcov:TQQQ", [["2024-06-01", "2024-06-30"]])
    monkeypatch.setattr(history, "history_feed",
                        lambda creds=None: pytest.fail(
                            "built a client for a range already covered"))
    assert ensure_history("TQQQ", D, D, []) == 0


def test_the_host_supplies_the_credentials(pg, monkeypatch):
    asked = []

    def creds():
        asked.append(True)
        return "host-key", "host-secret"

    made = {}
    monkeypatch.setattr(history, "AlpacaBarFeed",
                        lambda k, s, feed=None, adjustment=None:
                        made.update(key=k, secret=s, feed=feed,
                                    adjustment=adjustment) or FakeFeed())
    ensure_history("TQQQ", D, D, [], creds=creds)
    assert asked and made == {"key": "host-key", "secret": "host-secret",
                              "feed": "sip", "adjustment": "all"}, \
        "the consolidated tape, total-return adjusted"


def test_without_credentials_the_message_says_which_two(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        history.env_creds()
    assert "APCA_API_KEY_ID" in str(e.value)
    assert "APCA_API_SECRET_KEY" in str(e.value)


def test_the_environment_is_the_default(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "env-key-not-a-real-credential")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "env-secret-not-a-real-one")
    feed = history.history_feed()
    assert feed.key_id == "env-key-not-a-real-credential"
    assert feed.feed == "sip" and feed.adjustment == "all"


# ---------------------------------------------------------- the live refresh

TODAY = datetime(2024, 6, 18, 11, 15, tzinfo=ET)          # a Tuesday, EDT
YDAY = date(2024, 6, 17)
EARLY = date(2024, 7, 3)          # July 4 falls on a Thursday: a 13:00 close


def _min(ms, px=10.0, v=1000.0):
    """One well-formed minute: low <= open, close <= high."""
    return [ms, px, px + 1, px - 1, px, v]


class RangeFeed:
    """A BarFeed that answers any range with a fixed day map and records
    what it was asked for."""

    def __init__(self, days):
        self.days = days
        self.asks = []

    def fetch_days(self, symbol, start, end, progress=None):
        self.asks.append((symbol, start, end))
        return self.days


class DeadFeed:
    """The vendor is down. `fetch_days` is the only call refresh makes."""

    def fetch_days(self, symbol, start, end, progress=None):
        raise RuntimeError("Alpaca data API 503: service unavailable")


def _stored(pg, day, sym="TQQQ"):
    with pg() as s:
        return s.get(persistence.BarDay, (sym, day))


def test_refresh_writes_todays_closed_minutes(pg):
    feed = RangeFeed({TODAY.date(): [_min(34200000), _min(34260000),
                                     _min(34320000)]})
    assert history.refresh("tqqq", feed=feed, now=TODAY) == 1
    assert feed.asks == [("TQQQ", date(2024, 6, 14), date(2024, 6, 18))], \
        "five calendar days back by default, symbol upper-cased"
    rec = _stored(pg, TODAY.date())
    assert [r[0] for r in rec.rows] == [34200000, 34260000, 34320000]
    assert rec.source == "rest"
    assert [type(x) for x in rec.rows[0]] == [int, float, float, float,
                                              float, int], \
        "the stored shape: a whole volume stays whole, prices stay floats"


def test_the_in_progress_minute_is_never_written(pg):
    """11:15 ET is 40500000 ms, and at 11:15 that minute has not closed. A
    partial row read as a closed bar lets a stop trigger on a low the real
    minute never printed."""
    feed = RangeFeed({TODAY.date(): [_min(40380000), _min(40440000),
                                     _min(40500000)]})
    history.refresh("TQQQ", feed=feed, now=TODAY)
    assert [r[0] for r in _stored(pg, TODAY.date()).rows] == [40380000,
                                                              40440000]


def test_the_minute_that_just_closed_is_written(pg):
    """The boundary the other way: at 11:16 the 11:15 bar has closed and is
    a bar like any other."""
    feed = RangeFeed({TODAY.date(): [_min(40440000), _min(40500000)]})
    history.refresh("TQQQ", feed=feed, now=TODAY.replace(minute=16))
    assert [r[0] for r in _stored(pg, TODAY.date()).rows] == [40440000,
                                                              40500000]


def test_the_cut_is_todays_business_only(pg):
    feed = RangeFeed({YDAY: [_min(57480000), _min(57540000)]})   # 15:58, 15:59
    history.refresh("TQQQ", feed=feed, now=TODAY)
    assert [r[0] for r in _stored(pg, YDAY).rows] == [57480000, 57540000]


def test_a_bar_outside_the_regular_session_is_never_stored(pg):
    """[09:30, 16:00). The engine trades the regular session, and a
    pre-market or after-hours print stored as a regular bar fills resting
    orders at a price the session never saw. The bundled client already
    drops these; the rule is applied here as well, so a host that passes
    its own feed writes the same table."""
    feed = RangeFeed({YDAY: [_min(28800000),      # 08:00, pre-market
                             _min(34140000),      # 09:29
                             _min(34200000),      # 09:30, the open
                             _min(57540000),      # 15:59, the last bar
                             _min(57600000),      # 16:00
                             _min(72000000)]})    # 20:00
    history.refresh("TQQQ", feed=feed, now=TODAY)
    assert [r[0] for r in _stored(pg, YDAY).rows] == [34200000, 57540000]


def test_an_early_close_day_ends_at_one_oclock(pg):
    """A half-day closes at 13:00 and feeds keep printing to 16:00."""
    feed = RangeFeed({EARLY: [_min(46680000),     # 12:58
                              _min(46740000),     # 12:59
                              _min(46800000),     # 13:00
                              _min(48600000)]})   # 13:30
    history.refresh("TQQQ", feed=feed,
                    now=datetime(2024, 7, 5, 11, 15, tzinfo=ET))
    assert [r[0] for r in _stored(pg, EARLY).rows] == [46680000, 46740000]


def test_a_malformed_candle_is_dropped(pg):
    """low above high, an open eight times the price: stored, it would mark
    every position off nonsense."""
    feed = RangeFeed({YDAY: [_min(34200000), _min(34260000),
                             [34320000, 539.0, 72.55, 72.65, 72.40, 73]]})
    history.refresh("TQQQ", feed=feed, now=TODAY)
    assert [r[0] for r in _stored(pg, YDAY).rows] == [34200000, 34260000]


def test_a_duplicated_minute_is_stored_once(pg):
    """A vendor resends a corrected candle for a minute it already sent."""
    feed = RangeFeed({YDAY: [_min(34200000, px=10.0), _min(34260000),
                             _min(34200000, px=11.0)]})
    history.refresh("TQQQ", feed=feed, now=TODAY)
    rec = _stored(pg, YDAY)
    assert [r[0] for r in rec.rows] == [34200000, 34260000]
    assert rec.rows[0][1] == 11.0, "the correction wins"


def test_a_day_with_fewer_than_two_usable_bars_is_not_stored(pg):
    """What every reader of this table already requires of a day: the
    exports, the live push and the replay all skip one with a single row."""
    feed = RangeFeed({YDAY: [_min(34200000)]})
    assert history.refresh("TQQQ", feed=feed, now=TODAY) == 0
    assert _stored(pg, YDAY) is None


def test_a_refresh_merges_with_what_a_feed_already_stored(pg):
    """A feed that stored minutes this call did not fetch keeps them, and
    the day keeps the tag of whoever wrote it first."""
    with pg() as s:
        s.add(persistence.BarDay(symbol="TQQQ", day=YDAY, source="stream",
                                 rows=[_min(34200000, px=1.0),
                                       _min(34500000, px=2.0)]))
        s.commit()
    feed = RangeFeed({YDAY: [_min(34260000, px=3.0), _min(34320000, px=4.0)]})
    assert history.refresh("TQQQ", feed=feed, now=TODAY) == 1
    rec = _stored(pg, YDAY)
    assert [r[0] for r in rec.rows] == [34200000, 34260000, 34320000,
                                        34500000]
    assert [r[1] for r in rec.rows] == [1.0, 3.0, 4.0, 2.0]
    assert rec.source == "stream"


def test_a_fetched_minute_wins_over_the_stored_one(pg):
    with pg() as s:
        s.add(persistence.BarDay(symbol="TQQQ", day=YDAY, source="stream",
                                 rows=[_min(34200000, px=1.0),
                                       _min(34260000, px=2.0)]))
        s.commit()
    feed = RangeFeed({YDAY: [_min(34200000, px=9.0), _min(34260000, px=2.0)]})
    assert history.refresh("TQQQ", feed=feed, now=TODAY) == 1
    assert [r[1] for r in _stored(pg, YDAY).rows] == [9.0, 2.0]


def test_an_unchanged_day_leaves_the_export_stamp_alone(pg):
    """Readers use `fetched_at` to decide whether to re-export the day, so
    a refresh that changed nothing must not tell them it did."""
    feed = RangeFeed({YDAY: [_min(34200000), _min(34260000)]})
    history.refresh("TQQQ", feed=feed, now=TODAY)
    before = _stored(pg, YDAY).fetched_at
    assert history.refresh("TQQQ", feed=feed, now=TODAY) == 0
    assert _stored(pg, YDAY).fetched_at == before


def test_the_days_changed_are_counted_not_the_rows(pg):
    feed = RangeFeed({YDAY: [_min(34200000), _min(34260000)],
                      TODAY.date(): [_min(34200000), _min(34260000),
                                     _min(34320000)]})
    assert history.refresh("TQQQ", feed=feed, now=TODAY) == 2


def test_the_window_is_the_callers(pg):
    feed = RangeFeed({})
    history.refresh("TQQQ", days=1, feed=feed, now=TODAY)
    assert feed.asks == [("TQQQ", TODAY.date(), TODAY.date())]


def test_a_window_of_no_days_still_asks_for_today(pg):
    feed = RangeFeed({})
    history.refresh("TQQQ", days=0, feed=feed, now=TODAY)
    assert feed.asks == [("TQQQ", TODAY.date(), TODAY.date())]


def test_a_clock_in_another_zone_is_read_in_exchange_time(pg):
    """15:15Z is 11:15 ET on this date. The window and the in-progress cut
    are both the exchange's, whatever the caller's clock is set to."""
    utc = datetime(2024, 6, 18, 15, 15, tzinfo=timezone.utc)
    feed = RangeFeed({TODAY.date(): [_min(40380000), _min(40440000),
                                     _min(40500000)]})
    history.refresh("TQQQ", feed=feed, now=utc)
    assert feed.asks == [("TQQQ", date(2024, 6, 14), date(2024, 6, 18))]
    assert [r[0] for r in _stored(pg, TODAY.date()).rows] == [40380000,
                                                              40440000]


def test_one_refresh_of_a_symbol_at_a_time(pg):
    """Two threads refreshing the same day would each merge its own fetch
    onto the rows it read, and the second to commit would write the first's
    minutes away. The lock is held across the fetch, and it is per symbol,
    so a refresh of another symbol does not queue behind this one."""
    seen = {}

    class CheckingFeed(RangeFeed):
        def fetch_days(self, symbol, start, end, progress=None):
            seen["own"] = history._refresh_lock(symbol).locked()
            seen["other"] = history._refresh_lock("SPY").locked()
            return super().fetch_days(symbol, start, end, progress)

    history.refresh("TQQQ", feed=CheckingFeed({}), now=TODAY)
    assert seen == {"own": True, "other": False}
    assert not history._refresh_lock("TQQQ").locked(), "released on the way out"


def test_a_vendor_error_releases_the_symbol(pg):
    with pytest.raises(RuntimeError):
        history.refresh("TQQQ", feed=DeadFeed(), now=TODAY)
    assert not history._refresh_lock("TQQQ").locked()


def test_a_vendor_error_is_loud_and_writes_nothing(pg):
    """The fetch happens before a session is opened, so a failed request
    cannot leave half a day behind."""
    with pg() as s:
        s.add(persistence.BarDay(symbol="TQQQ", day=YDAY, source="stream",
                                 rows=[_min(34200000), _min(34260000)]))
        s.commit()
    with pytest.raises(RuntimeError) as e:
        history.refresh("TQQQ", feed=DeadFeed(), now=TODAY)
    assert "503" in str(e.value)
    assert _stored(pg, TODAY.date()) is None
    assert [r[0] for r in _stored(pg, YDAY).rows] == [34200000, 34260000]


# ---------------------------------------------------------- the two clients

def test_todays_minutes_are_fetched_raw_off_the_free_tape(monkeypatch):
    made = {}
    monkeypatch.setattr(history, "AlpacaBarFeed",
                        lambda k, s, feed=None, adjustment=None:
                        made.update(key=k, feed=feed, adjustment=adjustment))
    history.refresh_feed(creds=lambda: ("k-not-a-real-credential", "s"))
    assert made == {"key": "k-not-a-real-credential",
                    "feed": "iex", "adjustment": "raw"}, \
        "bar_days is what printed; an adjusted today would step the series"


def test_the_backfill_and_the_refresh_are_different_asks(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "env-key-not-a-real-credential")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "env-secret-not-a-real-one")
    hist, live = history.history_feed(), history.refresh_feed()
    assert (hist.feed, hist.adjustment) == ("sip", "all")
    assert (live.feed, live.adjustment) == ("iex", "raw")
    assert hist.key_id == live.key_id == "env-key-not-a-real-credential"


class _Wire:
    """urlopen, scripted. Nothing here reaches the network."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, req, timeout=None):
        url, _, qs = req.full_url.partition("?")
        self.requests.append((url, dict(urllib.parse.parse_qsl(qs))))
        page = self.pages.pop(0)

        class R:
            def read(self_, *a):
                return json.dumps(page).encode()
        return R()


def test_refresh_over_the_real_client_stores_the_session_it_asked_for(
        pg, monkeypatch):
    """End to end through the bundled client: the request it makes, and the
    rows that survive the trip into `bar_days`."""
    def bar(t, o):
        return {"t": t, "o": o, "h": o + 1, "l": o - 1, "c": o, "v": 1000}

    w = _Wire([{"bars": [bar("2024-06-18T13:29:00Z", 8.0),    # 09:29, out
                         bar("2024-06-18T13:30:00Z", 9.0),    # 09:30
                         bar("2024-06-18T13:31:00Z", 10.0),   # 09:31
                         bar("2024-06-18T15:15:00Z", 11.0)]}])  # 11:15, open
    monkeypatch.setattr(urllib.request, "urlopen", w)
    assert history.refresh("tqqq", now=TODAY,
                           creds=lambda: ("k-not-a-real-credential",
                                          "s-not-a-real-one")) == 1
    assert [r[0] for r in _stored(pg, TODAY.date()).rows] == [34200000,
                                                              34260000]
    assert w.requests[0][0] == \
        "https://data.alpaca.markets/v2/stocks/TQQQ/bars"
    assert w.requests[0][1] == {
        "timeframe": "1Min", "adjustment": "raw", "feed": "iex",
        "limit": "10000", "start": "2024-06-14T00:00:00Z",
        "end": "2024-06-18T23:59:59Z"}


# ------------------------------------------------ the single-install default

def test_the_default_bar_source_fills_in_both_calls(monkeypatch):
    """What a host with no market-data account of its own gets: one Alpaca
    account behind both of the port's vendor calls."""
    from dqengine.live.bar_source import default_bar_source
    seen = []
    monkeypatch.setattr(history, "ensure_history",
                        lambda *a, **kw: seen.append(("history", a, kw)) or 0)
    monkeypatch.setattr(history, "refresh",
                        lambda *a, **kw: seen.append(("refresh", a, kw)) or 0)
    creds = object()
    src = default_bar_source(creds=creds)
    src._history("TQQQ", D, D, [])
    src.refresh("TQQQ")
    assert seen == [("history", ("TQQQ", D, D, []), {"creds": creds}),
                    ("refresh", ("TQQQ",), {"creds": creds})]
