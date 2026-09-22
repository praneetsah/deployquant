"""Schwab's REST minutes: the call, the hygiene, and the write rule.

The vendor is a scripted `urlopen` and the database is a fake session that
holds `BarDay` objects in a dict, so nothing here opens a socket, reads a
credential or needs Postgres. What is pinned is what prod has always done
with these bars, because a live sleeve is valued off them:

  * the request, including the explicit endDate that is the whole reason
    today's completed minutes arrive at all;
  * the regular session only, 13:00 on an early close, and a structurally
    impossible candle dropped rather than stored;
  * one row per minute when the vendor sends a minute twice;
  * the write rule: a stored day that already holds at least as many rows
    is left alone, `fetched_at` included, and a thinner one is replaced
    whole rather than merged;
  * one refresh of a symbol at a time, so two deployments sharing a symbol
    do not both insert the same row.
"""
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from dqengine.live.persistence import BarDay
from dqengine_schwab import bars as mod
from dqengine_schwab.feed import SchwabQuoteFeed

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 18)                 # a normal Friday session
HALF = date(2026, 11, 27)               # 13:00 close

TOKEN = (lambda: "t-not-a-real-token")


def candle(day: date, hh: int, mm: int, o=10.0, h=10.5, l=9.5, c=10.2,
           v=1000):
    """One CHART-shaped price-history candle, stamped in epoch ms."""
    at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
    return {"datetime": int(at.timestamp() * 1000), "open": o, "high": h,
            "low": l, "close": c, "volume": v}


class Wire:
    """A scripted `urlopen`: one page per call, every request recorded."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, req, timeout=None):
        url, _, qs = req.full_url.partition("?")
        self.requests.append((url, dict(urllib.parse.parse_qsl(qs)),
                              dict(req.headers)))
        page = self.pages.pop(0)

        class R:
            def read(self_, *a):
                return json.dumps(page).encode()
        return R()


class FakeSession:
    """Just enough session for the upsert: get, add, commit."""

    def __init__(self, rows):
        self.rows = rows            # {(SYM, day): BarDay}
        self.commits = 0

    def get(self, model, key):
        assert model is BarDay
        return self.rows.get(key)

    def add(self, row):
        self.rows[(row.symbol, row.day)] = row

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def db(monkeypatch):
    """The bar table, in memory, as the factory the refresh opens."""
    from dqengine.live import persistence
    rows = {}
    monkeypatch.setattr(persistence, "SessionLocal", lambda: FakeSession(rows))
    return rows


@pytest.fixture()
def wire(monkeypatch):
    def install(*pages):
        w = Wire(*pages)
        monkeypatch.setattr(urllib.request, "urlopen", w)
        return w
    return install


def stored(db, sym="TQQQ", day=DAY):
    return db.get((sym, day))


# ---- the call ---------------------------------------------------------------

def test_the_request_asks_for_minutes_up_to_now(wire):
    w = wire({"candles": []})
    mod.fetch_recent_minute_days("tqqq", token=TOKEN)
    url, q, headers = w.requests[0]
    assert url == "https://api.schwabapi.com/marketdata/v1/pricehistory"
    assert q["symbol"] == "TQQQ"
    assert q["periodType"] == "day" and q["period"] == "10"
    assert q["frequencyType"] == "minute" and q["frequency"] == "1"
    assert q["needExtendedHoursData"] == "false"
    assert int(q["endDate"]) > 0, \
        "without an explicit endDate Schwab stops at the previous close"
    assert headers["Authorization"] == "Bearer t-not-a-real-token"


def test_the_token_is_minted_per_call(wire):
    """A Schwab access token lasts half an hour. A refresh running all day
    asks the callable each time rather than holding the first one."""
    asked = []
    wire({"candles": []}, {"candles": []})
    token = lambda: (asked.append(1), f"t-{len(asked)}")[1]   # noqa: E731
    mod.fetch_recent_minute_days("TQQQ", token=token)
    mod.fetch_recent_minute_days("TQQQ", token=token)
    assert len(asked) == 2


def test_the_period_is_the_callers(wire):
    w = wire({"candles": []})
    mod.fetch_recent_minute_days("TQQQ", period_days=2, token=TOKEN)
    assert w.requests[0][1]["period"] == "2"


# ---- the hygiene ------------------------------------------------------------

def test_only_the_regular_session_survives(wire):
    wire({"candles": [candle(DAY, 9, 29), candle(DAY, 9, 30),
                      candle(DAY, 15, 59), candle(DAY, 16, 0),
                      candle(DAY, 18, 0)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert [r[0] for r in out[DAY]] == [34200000, 57540000]


def test_an_early_close_ends_at_one(wire):
    """Schwab keeps sending candles until 16:00 on a half day. Stored as
    regular bars they fill resting orders on after-hours prints."""
    wire({"candles": [candle(HALF, 12, 59), candle(HALF, 13, 0),
                      candle(HALF, 15, 0)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert [r[0] for r in out[HALF]] == [(12 * 3600 + 59 * 60) * 1000]


def test_a_candle_that_is_not_a_bar_is_dropped(wire):
    """The observed malformed bar: low above high, an open eight times the
    price. Stored, it is what a sleeve gets valued at."""
    wire({"candles": [candle(DAY, 9, 30),
                      candle(DAY, 9, 31, o=539.0, h=72.55, l=72.65, c=72.40),
                      candle(DAY, 9, 32)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert [r[0] for r in out[DAY]] == [34200000, 34320000]


def test_a_minute_sent_twice_is_stored_once_and_the_last_wins(wire):
    wire({"candles": [candle(DAY, 9, 30, c=10.0), candle(DAY, 9, 31),
                      candle(DAY, 9, 30, c=10.4)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert [r[0] for r in out[DAY]] == [34200000, 34260000]
    assert out[DAY][0][4] == 10.4


def test_the_rows_are_the_shape_every_reader_expects(wire):
    wire({"candles": [candle(DAY, 9, 30, o=1.0, h=2.0, l=0.5, c=1.5, v=7)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert out[DAY] == [[34200000, 1.0, 2.0, 0.5, 1.5, 7]]


def test_each_session_is_its_own_day(wire):
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31),
                      candle(date(2026, 9, 17), 9, 30)]})
    out = mod.fetch_recent_minute_days("TQQQ", token=TOKEN)
    assert sorted(out) == [date(2026, 9, 17), DAY]


# ---- the write rule ---------------------------------------------------------

def test_a_fresh_day_is_written(db, wire):
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31)]})
    assert mod.refresh_bar_cache("tqqq", token=TOKEN) == 1
    rec = stored(db)
    assert rec.symbol == "TQQQ" and rec.day == DAY
    assert [r[0] for r in rec.rows] == [34200000, 34260000]


def test_a_day_with_one_usable_bar_is_not_stored(db, wire):
    wire({"candles": [candle(DAY, 9, 30)]})
    assert mod.refresh_bar_cache("TQQQ", token=TOKEN) == 0
    assert stored(db) is None


def test_a_day_already_as_complete_is_left_completely_alone(db, wire):
    """The stored day is the stream's record and may hold minutes this call
    did not fetch. Row count is the test, and `fetched_at` is untouched --
    readers use that stamp to decide whether to re-export the day."""
    was = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
    db[("TQQQ", DAY)] = BarDay(symbol="TQQQ", day=DAY, fetched_at=was,
                               rows=[[34200000, 1, 1, 1, 1, 1],
                                     [34260000, 2, 2, 2, 2, 2],
                                     [34320000, 3, 3, 3, 3, 3]])
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31)]})
    assert mod.refresh_bar_cache("TQQQ", token=TOKEN) == 0
    rec = stored(db)
    assert len(rec.rows) == 3 and rec.rows[0][1] == 1
    assert rec.fetched_at == was


def test_a_thinner_day_is_replaced_whole_and_restamped(db, wire):
    """This vendor's price history returns the whole session, so the longer
    record is the better one and the fetched rows stand on their own. That
    is not what `dqengine.live.history.refresh` does with Alpaca's bars,
    which merges -- the difference is per vendor and deliberate."""
    db[("TQQQ", DAY)] = BarDay(symbol="TQQQ", day=DAY, fetched_at=None,
                               rows=[[34200000, 1, 1, 1, 1, 1],
                                     [34500000, 9, 9, 9, 9, 9]])
    wire({"candles": [candle(DAY, 9, 30, c=10.4), candle(DAY, 9, 31),
                      candle(DAY, 9, 32)]})
    assert mod.refresh_bar_cache("TQQQ", token=TOKEN) == 1
    rec = stored(db)
    assert [r[0] for r in rec.rows] == [34200000, 34260000, 34320000]
    assert rec.rows[0][4] == 10.4, "the fetched minute, not the stored one"
    assert 34500000 not in [r[0] for r in rec.rows], \
        "replaced whole: a stored minute outside the fetch does not survive"
    assert rec.fetched_at is not None


def test_the_symbol_is_upper_cased_everywhere(db, wire):
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31)]})
    mod.refresh_bar_cache("tqqq", token=TOKEN)
    assert ("TQQQ", DAY) in db


def test_the_count_is_days_not_rows(db, wire):
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31),
                      candle(date(2026, 9, 17), 9, 30),
                      candle(date(2026, 9, 17), 9, 31)]})
    assert mod.refresh_bar_cache("TQQQ", token=TOKEN) == 2


def test_a_vendor_error_writes_nothing(db, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("schwab said no")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        mod.refresh_bar_cache("TQQQ", token=TOKEN)
    assert db == {}


# ---- the lock ---------------------------------------------------------------

def test_the_refresh_is_guarded_per_symbol():
    """Narrowing the tick lock to one lock per deployment exposed a race
    the global one was hiding: two deployments refreshing one symbol both
    see `existing is None` and both insert the same BarDay row."""
    assert mod._refresh_lock("TQQQ") is mod._refresh_lock("tqqq".upper())
    assert mod._refresh_lock("TQQQ") is not mod._refresh_lock("SPY")


def test_the_lock_is_held_across_the_whole_upsert(db, wire, monkeypatch):
    wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31)]})
    held = []
    monkeypatch.setattr(mod, "_refresh_bar_cache",
                        lambda sym, token: held.append(
                            mod._refresh_lock("TQQQ").locked()))
    mod.refresh_bar_cache("tqqq", token=TOKEN)
    assert held == [True]


def test_the_lock_is_released_when_the_vendor_fails(monkeypatch):
    monkeypatch.setattr(mod, "_refresh_bar_cache",
                        lambda sym, token: (_ for _ in ()).throw(
                            OSError("schwab said no")))
    with pytest.raises(OSError):
        mod.refresh_bar_cache("ZZZ", token=TOKEN)
    assert not mod._refresh_lock("ZZZ").locked()


# ---- the feed's capability --------------------------------------------------

def test_the_feed_refreshes_on_its_own_token(db, wire):
    """`dqengine.feeds.BarRefresher` on the feed: a worker whose stream has
    gone quiet asks the feed, and the feed asks this vendor with the same
    token the socket logs in with."""
    from dqengine.feeds.base import bar_refresher
    w = wire({"candles": [candle(DAY, 9, 30), candle(DAY, 9, 31)]})
    feed = SchwabQuoteFeed(token=lambda: "t-from-the-feed",
                           connect=lambda url: pytest.fail(
                               "the REST refresh opened a socket"))
    assert bar_refresher(feed) is not None
    assert feed.refresh_bars("tqqq") == 1
    assert w.requests[0][2]["Authorization"] == "Bearer t-from-the-feed"
    assert stored(db) is not None


def test_the_feed_asks_the_module_at_call_time(monkeypatch):
    """Bound at import, a host that replaced the call would be ignored."""
    seen = []
    monkeypatch.setattr(mod, "refresh_bar_cache",
                        lambda sym, token: seen.append((sym, token())) or 7)
    feed = SchwabQuoteFeed(token=lambda: "t-injected")
    assert feed.refresh_bars("AAA") == 7
    assert seen == [("AAA", "t-injected")]


def test_nothing_here_can_be_called_without_a_token():
    """The token is keyword-only and has no default: an unauthenticated
    call is a TypeError, never a request that quietly 401s."""
    with pytest.raises(TypeError):
        mod.refresh_bar_cache("TQQQ")
    with pytest.raises(TypeError):
        mod.fetch_recent_minute_days("TQQQ")
