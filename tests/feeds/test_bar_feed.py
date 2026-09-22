"""The history client: what a vendor bar becomes, and how a range is paged.

One client serves every caller of historical minute bars in this
distribution -- the CLI's data fetch, the driver's backfill, the live
cache's gap filling -- so its conversion and its pagination are pinned
here. Nothing reaches the network.
"""
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pytest

from dqengine.feed import AlpacaBarFeed, bars_to_days, drop_in_progress


def _bar(t, o=1.0, h=2.0, low=0.5, c=1.5, v=100):
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v}


# ------------------------------------------------------------- conversion

def test_bars_to_days_converts_utc_to_the_exchange_session():
    # 2023-08-03: EDT (UTC-4). 13:30Z = 09:30 ET (in), 13:29Z = 09:29 (out),
    # 19:59Z = 15:59 (in, last bar), 20:00Z = 16:00 (out)
    bars = [_bar("2023-08-03T13:29:00Z"),
            _bar("2023-08-03T13:30:00Z", o=10.0),
            _bar("2023-08-03T19:59:00Z", c=11.0),
            _bar("2023-08-03T20:00:00Z")]
    days = bars_to_days(bars)
    assert list(days) == [date(2023, 8, 3)]
    rows = days[date(2023, 8, 3)]
    assert len(rows) == 2
    assert rows[0][0] == (9 * 3600 + 30 * 60) * 1000 and rows[0][1] == 10.0
    assert rows[-1][0] == (15 * 3600 + 59 * 60) * 1000 and rows[-1][4] == 11.0


def test_bars_to_days_handles_the_winter_offset_and_sorts():
    # 2024-01-05: EST (UTC-5). 14:30Z = 09:30 ET. Out-of-order input sorts.
    bars = [_bar("2024-01-05T15:00:00Z"), _bar("2024-01-05T14:30:00Z")]
    rows = bars_to_days(bars)[date(2024, 1, 5)]
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert rows[0][0] == (9 * 3600 + 30 * 60) * 1000


def test_bars_to_days_stops_at_an_early_close():
    """13:00 on a half day. A 13:30 bar kept as a regular one fills resting
    orders on an after-hours print."""
    bars = [_bar("2024-11-29T17:58:00Z"), _bar("2024-11-29T18:30:00Z")]
    assert list(bars_to_days(bars)[date(2024, 11, 29)][0])[0] == 46680000
    assert len(bars_to_days(bars)[date(2024, 11, 29)]) == 1


def test_every_field_comes_back_as_a_float():
    rows = bars_to_days([_bar("2023-08-03T13:30:00Z", o=10, v=1000)])
    assert rows[date(2023, 8, 3)][0][1:] == [10.0, 2.0, 0.5, 1.5, 1000.0]


def test_the_in_progress_minute_never_leaves_the_client():
    rows = [[0, 1, 1, 1, 1, 1], [60_000, 1, 1, 1, 1, 1]]
    assert drop_in_progress(rows, now_ms=120_000) == rows
    assert drop_in_progress(rows, now_ms=119_999) == rows[:1]


# -------------------------------------------------------------- pagination

class Wire:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        url, _, qs = req.full_url.partition("?")
        self.requests.append((url, dict(urllib.parse.parse_qsl(qs)),
                              dict(req.headers), timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item

        class R:
            def read(self_, *a):
                return json.dumps(item).encode()
        return R()


@pytest.fixture()
def wire(monkeypatch):
    def install(script):
        w = Wire(script)
        monkeypatch.setattr(urllib.request, "urlopen", w)
        return w
    return install


def test_pages_are_followed_and_merged_into_one_day_map(wire):
    w = wire([{"bars": [_bar("2024-06-17T13:31:00Z", o=2.0)],
               "next_page_token": "t1"},
              {"bars": [_bar("2024-06-17T13:30:00Z", o=1.0)]}])
    feed = AlpacaBarFeed("k", "s", feed="sip")
    days = feed.fetch_days("tqqq", date(2024, 6, 17), date(2024, 6, 17))
    assert [r[1] for r in days[date(2024, 6, 17)]] == [1.0, 2.0], \
        "pages merge and the day sorts, whatever order they arrived in"
    assert [u for u, _, _, _ in w.requests] == [
        "https://data.alpaca.markets/v2/stocks/TQQQ/bars"] * 2
    assert w.requests[0][1] == {
        "timeframe": "1Min", "adjustment": "all", "feed": "sip",
        "limit": "10000", "start": "2024-06-17T00:00:00Z",
        "end": "2024-06-17T23:59:59Z"}
    assert w.requests[1][1]["page_token"] == "t1"
    assert w.requests[0][2]["Apca-api-key-id"] == "k"
    assert w.requests[0][3] == 60.0


def test_progress_is_reported_as_each_page_lands(wire):
    wire([{"bars": [_bar("2024-06-17T13:30:00Z")], "next_page_token": "t1"},
          {"bars": [], "next_page_token": "t2"},
          {"bars": [_bar("2024-06-18T13:30:00Z")]}])
    seen = []
    AlpacaBarFeed("k", "s").fetch_days("X", date(2024, 6, 17),
                                       date(2024, 6, 18), progress=seen.append)
    assert seen == [date(2024, 6, 17), date(2024, 6, 17), date(2024, 6, 18)], \
        "the latest day seen so far, once per page; an empty page repeats it"


def test_a_page_with_no_days_yet_reports_no_progress(wire):
    wire([{"bars": []}])
    seen = []
    AlpacaBarFeed("k", "s").fetch_days("X", date(2024, 6, 17),
                                       date(2024, 6, 17), progress=seen.append)
    assert seen == []


def test_a_rate_limit_backs_off_and_retries_the_same_request(wire):
    err = urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO(b""))
    w = wire([err, err, {"bars": []}])
    slept = []
    AlpacaBarFeed("k", "s", sleep=slept.append).fetch_days(
        "X", date(2024, 6, 17), date(2024, 6, 17))
    assert slept == [15, 30], "15s, then 30s"
    assert w.requests[0][1] == w.requests[2][1]


def test_a_rate_limit_that_never_clears_gives_up_loudly(wire):
    err = urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO(b"x"))
    wire([err] * 5)
    with pytest.raises(RuntimeError) as e:
        AlpacaBarFeed("k", "s", sleep=lambda s: None).fetch_days(
            "X", date(2024, 6, 17), date(2024, 6, 17))
    assert "429" in str(e.value)


def test_any_other_status_raises_at_once_with_the_body(wire):
    w = wire([urllib.error.HTTPError("u", 403, "no", {},
                                     io.BytesIO(b"forbidden"))])
    with pytest.raises(RuntimeError) as e:
        AlpacaBarFeed("k", "s", sleep=lambda s: None).fetch_days(
            "X", date(2024, 6, 17), date(2024, 6, 17))
    assert "403" in str(e.value) and "forbidden" in str(e.value)
    assert len(w.requests) == 1


def test_the_feed_and_adjustment_are_the_callers(wire):
    w = wire([{"bars": []}])
    AlpacaBarFeed("k", "s", feed="iex", adjustment="raw").fetch_days(
        "X", date(2024, 6, 17), date(2024, 6, 17))
    assert w.requests[0][1]["feed"] == "iex"
    assert w.requests[0][1]["adjustment"] == "raw"
