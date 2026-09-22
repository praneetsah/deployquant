"""The silence fall-back asks the feed that went quiet.

A worker whose stream has stopped still owes its strategy today's bars, and
the vendor it asks is the one it was streaming: the same tape, the same
account, the same record of the session. That makes the REST refresh a
capability of the feed (`dqengine.feeds.BarRefresher`) rather than a second
composition, and the bar source ask the running feed for it.

What is pinned here: the bundled Alpaca feed's own REST refresh, including
the request it makes and the credentials and tape it makes it with; how the
bar source chooses between an explicit `refresh=` and a feed's capability;
what it says when the feed has none; and the `--fallback` wiring, which
builds a second feed object for REST alone and opens no socket for it.
"""
import json
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from dqengine.feeds.alpaca import AlpacaQuoteFeed
from dqengine.feeds.base import bar_refresher
from dqengine.live import bar_source, history, persistence, run
from dqengine.live.bar_source import SqlBarSource, default_bar_source
from dqengine.live.driver.ports import DriverNotConfigured

ET = ZoneInfo("America/New_York")
TODAY = datetime(2024, 6, 18, 12, 0, tzinfo=ET)          # Tuesday, mid-session
D = date(2024, 6, 18)


class Wire:
    """A scripted `urlopen`: one page per call, and every request recorded."""

    def __init__(self, pages):
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


def _at(monkeypatch, now):
    """Run the refresh at a fixed exchange time. `refresh_bars` takes no
    clock -- the vendor call it makes does, so the clock is pinned where
    the two meet."""
    real = history.refresh
    monkeypatch.setattr(history, "refresh",
                        lambda sym, **kw: real(sym, now=now, **kw))


def bar(t, o):
    return {"t": t, "o": o, "h": o + 1, "l": o - 1, "c": o, "v": 1000}


def feed_with(monkeypatch, pages, **kw):
    """An Alpaca quote feed whose REST calls go to a scripted wire, and
    whose socket would fail the test if anything opened one."""
    wire = Wire(pages)
    monkeypatch.setattr(urllib.request, "urlopen", wire)
    kw.setdefault("key_id", "k-not-a-real-credential")
    kw.setdefault("secret_key", "s-not-a-real-one")
    kw.setdefault("connect", lambda url: pytest.fail(
        f"the REST fall-back opened a socket to {url}"))
    return AlpacaQuoteFeed(**kw), wire


# ---- the capability itself --------------------------------------------------

def test_the_bundled_feed_carries_the_capability():
    assert bar_refresher(AlpacaQuoteFeed()) is not None


def test_a_feed_without_it_answers_none():
    class BarelyAFeed:
        def poll(self, timeout=1.0):
            return 0

    assert bar_refresher(BarelyAFeed()) is None
    assert bar_refresher(None) is None


def test_something_that_is_not_callable_is_not_the_capability():
    class Odd:
        refresh_bars = "soon"

    assert bar_refresher(Odd()) is None


# ---- the Alpaca feed's own REST bars ----------------------------------------

def test_the_feeds_refresh_stores_the_session_it_asked_for(pg, monkeypatch):
    """End to end over a scripted wire: the bars the vendor returns, the
    hygiene `history.refresh` applies, and the rows that reach `bar_days`."""
    feed, wire = feed_with(monkeypatch, [
        {"bars": [bar("2024-06-18T13:29:00Z", 8.0),     # 09:29, out of session
                  bar("2024-06-18T13:30:00Z", 9.0),     # 09:30
                  bar("2024-06-18T13:31:00Z", 10.0),    # 09:31
                  bar("2024-06-18T16:30:00Z", 11.0)]}])  # 12:30, not closed yet
    _at(monkeypatch, TODAY)
    assert feed.refresh_bars("tqqq") == 1
    with pg() as s:
        rec = s.get(persistence.BarDay, ("TQQQ", D))
    assert [r[0] for r in rec.rows] == [34200000, 34260000]
    url, params, _ = wire.requests[0]
    assert url == "https://data.alpaca.markets/v2/stocks/TQQQ/bars"
    assert params["timeframe"] == "1Min"
    assert params["adjustment"] == "raw", "raw is what printed"
    assert params["feed"] == "iex", "the free tape, as the stream's default is"


def test_the_rest_bars_come_off_the_tape_the_stream_runs_on(pg, monkeypatch):
    """A paid SIP stream must not fall back to IEX: splicing one exchange's
    minutes into the middle of a consolidated session is a different tape
    for the same day."""
    feed, wire = feed_with(monkeypatch, [{"bars": []}], feed="sip")
    _at(monkeypatch, TODAY)
    feed.refresh_bars("TQQQ")
    assert wire.requests[0][1]["feed"] == "sip"


def test_the_rest_bars_use_the_feeds_own_credentials(pg, monkeypatch):
    feed, wire = feed_with(monkeypatch, [{"bars": []}])
    _at(monkeypatch, TODAY)
    feed.refresh_bars("TQQQ")
    headers = wire.requests[0][2]
    assert headers["Apca-api-key-id"] == "k-not-a-real-credential"
    assert headers["Apca-api-secret-key"] == "s-not-a-real-one"


def test_one_rest_client_serves_every_symbol(pg, monkeypatch):
    """A universe of 58 symbols refreshes once a minute while a stream is
    down: a client per symbol is 58 clients a minute."""
    feed, _ = feed_with(monkeypatch, [{"bars": []}, {"bars": []}])
    _at(monkeypatch, TODAY)
    feed.refresh_bars("AAA")
    first = feed._rest_bars()
    feed.refresh_bars("BBB")
    assert feed._rest_bars() is first


def test_a_feed_built_with_no_credentials_says_which_ones_to_set(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        AlpacaQuoteFeed().refresh_bars("TQQQ")
    assert "APCA_API_KEY_ID" in str(e.value)


# ---- what the bar source does with it ---------------------------------------

class Refresher:
    """A feed with the capability and nothing else on it."""

    def __init__(self, wrote=1):
        self.calls = []
        self.wrote = wrote

    def refresh_bars(self, symbol):
        self.calls.append(symbol)
        return self.wrote


class Streamer:
    """A feed without the capability."""


def test_the_bar_source_refreshes_through_the_feed_it_was_given():
    feed = Refresher(wrote=2)
    src = SqlBarSource(feed=feed)
    assert src.refresh("TQQQ") == 2
    assert feed.calls == ["TQQQ"]


def test_an_explicit_refresh_wins_over_the_feeds_own():
    """A host may stream on one account and refresh on another; saying so
    explicitly is how."""
    feed, seen = Refresher(), []
    src = SqlBarSource(refresh=lambda sym: seen.append(sym) or 3, feed=feed)
    assert src.refresh("TQQQ") == 3
    assert seen == ["TQQQ"] and feed.calls == []


def test_a_feed_with_no_rest_bars_refuses_and_names_it():
    src = SqlBarSource(feed=Streamer())
    with pytest.raises(DriverNotConfigured) as e:
        src.refresh("TQQQ")
    assert "Streamer" in str(e.value) and "refresh" in str(e.value)


def test_the_refresh_source_is_what_the_fallback_would_ask():
    assert SqlBarSource().refresh_source() is None
    assert SqlBarSource(feed=Streamer()).refresh_source() is None
    assert SqlBarSource(refresh=lambda sym: 0).refresh_source() == "REST"
    assert SqlBarSource(feed=Refresher()).refresh_source() == "Refresher REST"


def test_the_default_bar_source_refreshes_through_a_feed_when_given_one(
        monkeypatch):
    """`--fallback` reaches the port here: the history stays the Alpaca
    account this install backfills from, and today's minutes come from the
    feed."""
    feed, asked = Refresher(), []
    monkeypatch.setattr(history, "ensure_history",
                        lambda *a, **kw: asked.append(a) or 0)
    monkeypatch.setattr(history, "refresh", lambda *a, **kw: pytest.fail(
        "the feed's own REST bars were bypassed"))
    src = default_bar_source(feed=feed)
    src.refresh("TQQQ")
    src._history("TQQQ", D, D, [])
    assert feed.calls == ["TQQQ"] and asked == [("TQQQ", D, D, [])]


def test_the_default_bar_source_without_a_feed_is_unchanged(monkeypatch):
    seen = []
    monkeypatch.setattr(history, "refresh",
                        lambda *a, **kw: seen.append((a, kw)) or 0)
    creds = object()
    default_bar_source(creds=creds).refresh("TQQQ")
    assert seen == [(("TQQQ",), {"creds": creds})]


# ---- the wiring: which feed the fall-back is built from ---------------------

def test_a_rest_feed_is_built_without_a_store_or_a_socket(monkeypatch):
    """`rest_feed` exists to be asked for bars and nothing else. Opening a
    socket for it would cost the account's one stream slot -- Alpaca allows
    one connection, and the process that is streaming holds it."""
    from dqengine.feeds import alpaca as alpaca_mod
    monkeypatch.setattr(alpaca_mod, "_connect_websocket", lambda url:
                        pytest.fail(f"a socket was opened to {url}"))
    feed = run.rest_feed("alpaca")
    assert isinstance(feed, AlpacaQuoteFeed)
    assert feed.store is None and feed.on_bar is None
    assert feed.symbols == []
    assert bar_refresher(feed) is not None


def test_the_fallback_may_name_another_vendor(monkeypatch):
    """`--feed schwab --fallback alpaca`: the stream is one vendor's and the
    REST bars are another's, deliberately, and the Alpaca side never opens a
    socket to be asked for them."""
    from dqengine.feeds import alpaca as alpaca_mod
    monkeypatch.setattr(alpaca_mod, "_connect_websocket", lambda url:
                        pytest.fail(f"a socket was opened to {url}"))
    installed = {}
    monkeypatch.setattr("dqengine.live.driver.ports.configure",
                        lambda **kw: installed.update(kw))
    monkeypatch.setattr(bar_source, "default_bar_source",
                        lambda creds=None, feed=None: ("built", feed))
    run.install_ports(fallback="alpaca")
    assert isinstance(installed["bars"][1], AlpacaQuoteFeed)


def test_no_fallback_named_is_the_single_account_composition(monkeypatch):
    installed = {}
    monkeypatch.setattr("dqengine.live.driver.ports.configure",
                        lambda **kw: installed.update(kw))
    monkeypatch.setattr(bar_source, "default_bar_source",
                        lambda creds=None, feed=None: ("built", feed))
    run.install_ports()
    assert installed["bars"] == ("built", None)


def test_a_session_falls_back_to_its_own_feed_by_default():
    s = run.LiveSession("dep", "conn", feed_name="alpaca")
    assert s.fallback_name == "alpaca"
    s2 = run.LiveSession("dep", "conn", feed_name="schwab",
                         fallback_name="alpaca")
    assert s2.fallback_name == "alpaca"


def test_the_session_installs_the_fallback_it_was_given(monkeypatch):
    seen = {}
    monkeypatch.setattr(run, "install_ports",
                        lambda bars=None, fallback=None:
                        seen.update(bars=bars, fallback=fallback))
    monkeypatch.setattr("dqengine.live.bus.bus_from_env", lambda: None)
    s = run.LiveSession("dep", "conn", feed_name="schwab",
                        fallback_name="alpaca")
    with pytest.raises(RuntimeError):
        s.start(consumers=False, feed=False)         # no bus: stops there
    assert seen == {"bars": None, "fallback": "alpaca"}
