"""The Schwab streamer, driven by a scripted socket.

Frames are written in the shapes the real stream sends: a login response
under `response`, minute candles under `data[].content` for
CHART_EQUITY, L1 updates for LEVELONE_EQUITIES, and a `notify`
heartbeat. No test opens a connection, and no test reads a credential
file: the token is a callable and so is the streamer-info lookup.
"""
import inspect
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from dqengine.adapters.base import BrokerAuthExpired
from dqengine.feeds.base import MinuteZipStore, QuoteBoard, bar_is_sane
from dqengine.runtime.core.data import DataStore
from dqengine_schwab import feed as mod
from dqengine_schwab.feed import SchwabQuoteFeed

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DAY = datetime(2026, 9, 18, tzinfo=ET)          # a normal Friday session
HALF = datetime(2026, 11, 27, tzinfo=ET)        # 13:00 close

INFO = {"streamerSocketUrl": "wss://streamer.example/ws",
        "schwabClientCustomerId": "CUST", "schwabClientCorrelId": "CORR",
        "schwabClientChannel": "CH", "schwabClientFunctionId": "FN"}

LOGIN_OK = json.dumps({"response": [{"service": "ADMIN", "command": "LOGIN",
                                     "requestid": "1",
                                     "content": {"code": 0, "msg": "ok"}}]})


def login_denied(code=3, msg="token expired"):
    return json.dumps({"response": [{"service": "ADMIN", "command": "LOGIN",
                                     "requestid": "1",
                                     "content": {"code": code, "msg": msg}}]})


def candle(symbol, dt_et, o=10.0, h=11.0, l=9.5, c=10.5, v=1000, seq=341):
    """The SHIFTED layout production sends: field 1 is a sequence number."""
    ts = int(dt_et.astimezone(UTC).timestamp() * 1000)
    return {"key": symbol, "1": seq, "2": o, "3": h, "4": l, "5": c,
            "6": v, "7": ts, "8": 20260918}


def chart_frame(*candles):
    return json.dumps({"data": [{"service": "CHART_EQUITY", "command": "SUBS",
                                 "content": list(candles)}]})


def quote_frame(*quotes):
    return json.dumps({"data": [{"service": "LEVELONE_EQUITIES",
                                 "command": "SUBS", "content": list(quotes)}]})


HEARTBEAT = json.dumps({"notify": [{"heartbeat": "1787503260000"}]})


class FakeSocket:
    """A scripted websocket. Items are returned from `recv` in order: a
    str is the frame, an exception instance is raised, and an exhausted
    script is a read timeout."""

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.script:
            raise TimeoutError("no more scripted frames")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    def close(self):
        self.closed = True


class Connector:
    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.sockets = []
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if not self.scripts:
            raise AssertionError("the feed opened more sockets than scripted")
        sock = FakeSocket(self.scripts.pop(0))
        self.sockets.append(sock)
        return sock


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class RecordingStore:
    """A BarStore that records the order of writes; `changed` decides how
    much of each frame it reports as new."""

    def __init__(self, events, changed=None):
        self.events = events
        self._changed = changed

    def write(self, bars):
        self.events.append(("store", [b.symbol for b in bars]))
        return list(bars) if self._changed is None else self._changed(bars)


def make_feed(*scripts, symbols=("TQQQ",), quote_symbols=(), store=None,
              on_bar=None, on_quote=None, log=None, token=None, **kw):
    conn = Connector(*scripts)
    clock = Clock()
    lines = [] if log is None else log
    feed = SchwabQuoteFeed(
        {"app_key": "k", "app_secret": "s", "refresh_token": "r"},
        token=token or (lambda: "ACCESS"), streamer_info=lambda t: dict(INFO),
        store=store, on_bar=on_bar, on_quote=on_quote, symbols=symbols,
        quote_symbols=quote_symbols, connect=conn,
        sleep=lambda s: lines.append(("sleep", s)), clock=clock,
        log=lines.append, **kw)
    return feed, conn, clock, lines


# ---- login and subscription -------------------------------------------------

def test_the_login_request_is_exactly_this():
    feed, conn, _, _ = make_feed([LOGIN_OK])
    feed.poll()
    assert conn.urls == ["wss://streamer.example/ws"]
    assert conn.sockets[0].sent[0] == {"requests": [{
        "requestid": "1", "service": "ADMIN", "command": "LOGIN",
        "SchwabClientCustomerId": "CUST", "SchwabClientCorrelId": "CORR",
        "parameters": {"Authorization": "ACCESS", "SchwabClientChannel": "CH",
                       "SchwabClientFunctionId": "FN"}}]}
    assert feed.state.connected is True and feed.state.error is None


def test_both_services_are_subscribed_with_their_own_keys_and_fields():
    """Bars only for what the engine trades; quotes also for the extras a
    market strip watches."""
    feed, conn, clock, _ = make_feed([LOGIN_OK], symbols=("TQQQ", "qqq"),
                                     quote_symbols=("SPY",))
    feed.poll()
    chart, quotes = conn.sockets[0].sent[1], conn.sockets[0].sent[2]
    assert chart["requests"][0] == {
        "requestid": str(int(clock.t)), "service": "CHART_EQUITY",
        "command": "SUBS", "SchwabClientCustomerId": "CUST",
        "SchwabClientCorrelId": "CORR",
        "parameters": {"keys": "QQQ,TQQQ", "fields": "0,1,2,3,4,5,6,7,8"}}
    assert quotes["requests"][0]["service"] == "LEVELONE_EQUITIES"
    assert quotes["requests"][0]["parameters"] == {
        "keys": "QQQ,SPY,TQQQ",
        "fields": "0,1,2,3,8,10,11,12,17,32"}
    assert feed.state.symbols == ("QQQ", "TQQQ")


def test_adding_and_removing_symbols_while_connected_sends_the_difference():
    feed, conn, _, _ = make_feed([LOGIN_OK], symbols=("TQQQ",))
    feed.poll()
    feed.subscribe(["qqq", "TQQQ"])
    feed.subscribe_quotes(["IWM"])
    feed.unsubscribe(["TQQQ"])
    sent = [r["requests"][0] for r in conn.sockets[0].sent[3:]]
    assert [(s["service"], s["command"], s["parameters"]["keys"]) for s in sent] == [
        ("CHART_EQUITY", "ADD", "QQQ"),
        ("LEVELONE_EQUITIES", "ADD", "QQQ"),
        ("LEVELONE_EQUITIES", "ADD", "IWM"),
        ("CHART_EQUITY", "UNSUBS", "TQQQ"),
        ("LEVELONE_EQUITIES", "UNSUBS", "TQQQ")]
    assert feed.symbols == ["QQQ"] and feed.quote_symbols == ["IWM", "QQQ"]
    assert feed.state.symbols == ("QQQ",)


def test_subscribing_while_disconnected_sends_nothing_and_waits_for_the_socket():
    feed, conn, _, _ = make_feed([LOGIN_OK])
    feed.subscribe(["SPY"])
    assert conn.sockets == []
    feed.poll()
    assert conn.sockets[0].sent[1]["requests"][0]["parameters"]["keys"] == "SPY,TQQQ"


def test_a_denied_login_is_recorded_and_retried_not_raised():
    feed, conn, clock, _ = make_feed([login_denied()], [LOGIN_OK])
    assert feed.poll() == 0
    assert feed.state.connected is False
    assert "login denied (3)" in feed.state.error
    assert "re-auth" in feed.state.error and "token expired" in feed.state.error
    # a person has to re-authorize, so this does not hot-loop the venue
    clock.advance(mod.AUTH_BACKOFF_S - 1)
    assert feed.poll() == 0 and len(conn.sockets) == 1
    clock.advance(2)
    feed.poll()
    assert len(conn.sockets) == 2 and feed.state.connected is True


def test_an_expired_refresh_token_says_so_in_one_line():
    """Schwab expires a refresh token 7 days after issuing it. The feed
    that stops receiving must say which person-shaped action fixes it, and
    must not hammer the token endpoint while it waits for one."""
    calls = []

    def dead_token():
        calls.append(1)
        raise BrokerAuthExpired("Schwab re-authorization needed: "
                                "unsupported_token_type")

    feed, conn, clock, _ = make_feed([LOGIN_OK], token=dead_token)
    assert feed.poll() == 0
    assert feed.state.connected is False
    assert "authorization expired" in feed.state.error
    assert "7 days" in feed.state.error and "authorize the app again" in feed.state.error
    assert conn.sockets == []                    # no socket was ever opened
    clock.advance(mod.AUTH_BACKOFF_S - 1)
    assert feed.poll() == 0
    assert calls == [1], "a token only a person can renew is not retried per second"
    clock.advance(2)
    feed.poll()
    assert len(calls) == 2


def test_an_unreadable_login_response_is_a_failed_connect_not_a_crash():
    feed, _, _, _ = make_feed(["<html>gateway timeout</html>"])
    assert feed.poll() == 0
    assert "unreadable streamer login response" in feed.state.error


# ---- chart field layouts (ported from the hosted platform's suite) ----------

def test_shifted_wire_layout_is_parsed():
    """Real prod content, 2026-08-19 12:41 ET: field 1 is a sequence
    counter, 2..6 are OHLCV — a layout that VIOLATES Schwab's own docs but
    is what production streams since ~2026-08-18. Under the old hard-coded
    documented mapping every such bar was dropped as malformed."""
    bar = {"key": "TQQQ", "1": 341, "2": 72.715, "3": 72.78,
           "4": 72.68, "5": 72.7799, "6": 184210, "7": 1787503260000}
    assert mod.parse_chart_fields(bar) == (72.715, 72.78, 72.68, 72.7799, 184210)


def test_documented_layout_still_parses():
    """Schwab's officially documented layout (and what the stream sent
    until ~2026-08-18): 1..5 = OHLCV, 6 = sequence. Kept because the wire
    contradicts the docs today — a Schwab-side revert must not break us."""
    bar = {"key": "TQQQ", "1": 72.44, "2": 72.55, "3": 72.42,
           "4": 72.54, "5": 192759, "6": 538, "7": 1787425140000}
    assert mod.parse_chart_fields(bar) == (72.44, 72.55, 72.42, 72.54, 192759)


def test_layouts_cannot_be_mistaken_for_each_other():
    shifted = {"1": 341, "2": 72.715, "3": 72.78, "4": 72.68,
               "5": 72.7799, "6": 184210}
    assert mod.parse_chart_fields(shifted)[:1] + mod.parse_chart_fields(shifted)[4:] \
        == (72.715, 184210)
    documented = {"1": 72.44, "2": 72.55, "3": 72.42, "4": 72.54,
                  "5": 192759, "6": 538}
    o, h, l, c, v = mod.parse_chart_fields(documented)
    assert (o, v) == (72.44, 192759)


def test_garbage_content_returns_none():
    assert mod.parse_chart_fields({"key": "TQQQ", "7": 1787503260000}) is None
    assert mod.parse_chart_fields({"1": -1, "2": 0, "3": "x", "4": None}) is None


def test_the_sanity_check_is_the_engines_own_not_a_third_copy():
    assert mod.bar_is_sane is bar_is_sane


def test_a_candle_in_the_documented_layout_still_becomes_a_bar():
    """Both layouts travel the whole path, not just the parser."""
    ts = int(DAY.replace(hour=9, minute=31).astimezone(UTC).timestamp() * 1000)
    documented = {"key": "TQQQ", "1": 72.44, "2": 72.55, "3": 72.42,
                  "4": 72.54, "5": 192759, "6": 538, "7": ts}
    seen = []
    feed, _, _, _ = make_feed([LOGIN_OK, chart_frame(documented)],
                              on_bar=seen.append)
    feed.poll()
    assert [(b.symbol, b.start_ms, b.open, b.volume) for b in seen] == \
        [("TQQQ", 34260000, 72.44, 192759.0)]


# ---- bars -------------------------------------------------------------------

def test_a_bar_is_in_the_store_before_the_callback_is_told_about_it(tmp_path):
    seen = []

    def on_bar(bar):
        loaded = DataStore(str(tmp_path)).load_minute_day(bar.symbol, bar.day)
        seen.append((bar.symbol, bar.start_ms, None if loaded is None else loaded.n))

    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=31)))],
        store=MinuteZipStore(str(tmp_path)), on_bar=on_bar)
    feed.poll()
    assert seen == [("TQQQ", 34260000, 1)]


def test_a_whole_frame_is_stored_before_any_of_it_is_announced():
    events = []
    minute = DAY.replace(hour=9, minute=31)
    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(candle("TQQQ", minute),
                               candle("QQQ", minute, o=399.0, h=401.0,
                                      l=398.0, c=400.0))],
        store=RecordingStore(events),
        on_bar=lambda b: events.append(("bar", b.symbol)))
    feed.poll()
    assert events == [("store", ["TQQQ", "QQQ"]), ("bar", "TQQQ"), ("bar", "QQQ")]


def test_a_candle_the_store_already_holds_is_not_announced():
    events = []
    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=31)))],
        store=RecordingStore(events, changed=lambda bars: []),
        on_bar=lambda b: events.append(("bar", b.symbol)))
    feed.poll()
    assert events == [("store", ["TQQQ"])]
    assert feed.state.last_bar_at is None


def test_an_after_hours_candle_on_an_early_close_day_never_becomes_a_bar():
    """Schwab keeps streaming candles until 16:00 on a half day. Stored as
    regular bars they fill resting orders on after-hours prints."""
    events = []
    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(candle("TQQQ", HALF.replace(hour=12, minute=59)),
                               candle("TQQQ", HALF.replace(hour=13, minute=5)),
                               candle("TQQQ", HALF.replace(hour=15, minute=59)))],
        store=RecordingStore(events),
        on_bar=lambda b: events.append(("bar", b.start_ms)))
    feed.poll()
    assert events == [("store", ["TQQQ"]), ("bar", 46740000)]


def test_a_premarket_candle_never_becomes_a_bar():
    events = []
    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(candle("TQQQ", DAY.replace(hour=8, minute=15)))],
        store=RecordingStore(events), on_bar=lambda b: events.append(("bar", b)))
    feed.poll()
    assert events == []


def test_a_malformed_candle_is_dropped_loudly_and_the_frame_survives():
    """The observed shape, TQQQ 2026-08-18 15:59: low above high, an open
    eight times the price, 73 shares in a minute that traded 200k+. It is
    not a bar under either field layout, so no layout rescues it."""
    events, lines = [], []
    minute = DAY.replace(hour=15, minute=59)
    ts = int(minute.astimezone(UTC).timestamp() * 1000)
    bad = {"key": "TQQQ", "1": 539.0, "2": 72.55, "3": 72.65, "4": 72.40,
           "5": 73, "6": 0, "7": ts}
    feed, _, _, _ = make_feed(
        [LOGIN_OK, chart_frame(bad, candle("QQQ", minute))],
        store=RecordingStore(events),
        on_bar=lambda b: events.append(("bar", b.symbol)), log=lines)
    feed.poll()
    assert events == [("store", ["QQQ"]), ("bar", "QQQ")]
    assert any("dropped malformed TQQQ chart content" in str(x) for x in lines)


def test_a_candle_with_an_unreadable_time_is_dropped_loudly():
    events, lines = [], []
    bad = candle("TQQQ", DAY.replace(hour=9, minute=31))
    bad["7"] = "not-a-time"
    feed, _, _, _ = make_feed([LOGIN_OK, chart_frame(bad)],
                              store=RecordingStore(events),
                              on_bar=lambda b: events.append(("bar", b)),
                              log=lines)
    feed.poll()
    assert events == []
    assert any("unreadable time" in str(x) for x in lines)


def test_a_frame_that_is_not_json_is_dropped_loudly_and_the_feed_keeps_going():
    events, lines = [], []
    feed, _, _, _ = make_feed(
        [LOGIN_OK, "<html>bad gateway</html>",
         chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=31)))],
        store=RecordingStore(events),
        on_bar=lambda b: events.append(("bar", b.symbol)), log=lines)
    feed.poll()
    feed.poll()
    assert events == [("store", ["TQQQ"]), ("bar", "TQQQ")]
    assert any("dropped malformed frame" in str(x) for x in lines)
    assert feed.state.connected is True


def test_an_unknown_service_is_dropped_loudly():
    frame = json.dumps({"data": [{"service": "NASDAQ_BOOK", "content": []}]})
    feed, _, _, lines = make_feed([LOGIN_OK, frame])
    # the login response is read inside the connect, so this poll already
    # carries the first data frame
    assert feed.poll() == 1
    assert any("unknown service" in str(x) for x in lines)


def test_a_bar_callback_that_raises_does_not_take_the_feed_down():
    def boom(_bar):
        raise RuntimeError("strategy blew up")

    feed, _, _, lines = make_feed(
        [LOGIN_OK,
         chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=31))),
         chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=32)))],
        on_bar=boom)
    feed.poll()
    feed.poll()
    assert feed.state.connected is True
    assert sum("bar callback failed" in str(x) for x in lines) == 2


# ---- quotes -----------------------------------------------------------------

def test_a_quote_reaches_the_board_and_the_callback_with_the_fields_mapped():
    board = QuoteBoard()
    feed, _, clock, _ = make_feed(
        [LOGIN_OK, quote_frame({"key": "TQQQ", "1": 72.4, "2": 72.6, "3": 72.5,
                                "8": 1_000_000, "10": 73.0, "11": 71.0,
                                "12": 72.0, "17": 72.1, "32": "Normal"})],
        on_quote=board.update)
    feed.poll()
    q = board.get("TQQQ")
    assert (q["bid"], q["ask"], q["last"]) == (72.4, 72.6, 72.5)
    assert board.snapshot() == {"TQQQ": {"last": 72.5,
                                         "at_ms": int(clock.t * 1000)}}
    # the full board keeps every subscribed field, not just the three
    assert feed.quotes["TQQQ"] == {"bid": 72.4, "ask": 72.6, "last": 72.5,
                                   "volume": 1_000_000, "high": 73.0,
                                   "low": 71.0, "prev_close": 72.0,
                                   "open": 72.1, "status": "Normal",
                                   "at": feed.quotes["TQQQ"]["at"],
                                   "last_at": feed.quotes["TQQQ"]["at"]}


def test_a_tick_carries_the_trade_time_only_when_it_carried_a_trade():
    """The frame set has no per-field timestamp, so arrival is the tick's
    time. A frame with no last price leaves the trade time alone, which is
    what lets a consumer measure a price's staleness rather than a quote's."""
    assert mod.quote_tick("REW", {"1": 10.27}, 5_000).last_at_ms is None
    tick = mod.quote_tick("REW", {"1": 10.2, "2": 10.3, "3": 11.85}, 5_000)
    assert (tick.bid, tick.ask, tick.last) == (10.2, 10.3, 11.85)
    assert tick.at_ms == tick.last_at_ms == 5_000


def test_a_bid_only_tick_moves_the_frame_time_and_not_the_trade_time():
    """A thin ETF whose last trade was 15:40 but whose bid ticked at
    15:58:59 must report 19 minutes of staleness, not one second."""
    board = QuoteBoard()
    feed, _, clock, _ = make_feed(
        [LOGIN_OK, quote_frame({"key": "REW", "3": 11.85}),
         quote_frame({"key": "REW", "1": 10.27})],
        on_quote=board.update)
    feed.poll()                                  # the trade print
    traded_at = clock.t
    clock.advance(19 * 60)
    feed.poll()                                  # the bid, 19 minutes later
    q = feed.quotes["REW"]
    assert q["at"] != q["last_at"]
    assert board.snapshot()["REW"]["at_ms"] == int(traded_at * 1000)
    assert board.get("REW")["bid"] == 10.27


def test_a_quote_with_no_symbol_is_skipped():
    board = QuoteBoard()
    feed, _, _, _ = make_feed([LOGIN_OK, quote_frame({"3": 1.0},
                                                     {"key": "", "3": 2.0})],
                              on_quote=board.update)
    feed.poll()
    assert board.quotes == {} and feed.quotes == {}


def test_a_non_numeric_last_costs_one_field_not_the_connection():
    board = QuoteBoard()
    feed, _, _, _ = make_feed([LOGIN_OK, quote_frame({"key": "BAD", "3": "n/a"})],
                             on_quote=board.update)
    feed.poll()
    assert board.snapshot() == {}
    assert feed.state.connected is True


# ---- connection trouble -----------------------------------------------------

def test_a_dropped_socket_reconnects_and_re_subscribes_the_current_symbols():
    feed, conn, clock, _ = make_feed([LOGIN_OK], [LOGIN_OK], symbols=("TQQQ",))
    feed.poll()
    feed.subscribe(["QQQ"])
    conn.sockets[0].script.append(ConnectionResetError("socket died"))
    feed.poll()
    assert feed.state.connected is False and conn.sockets[0].closed
    assert feed.state.symbols == ()
    assert feed.poll() == 0                      # still inside the backoff
    assert len(conn.sockets) == 1
    clock.advance(5)
    feed.poll()
    assert len(conn.sockets) == 2
    keys = conn.sockets[1].sent[1]["requests"][0]["parameters"]["keys"]
    assert keys == "QQQ,TQQQ"
    assert feed.state.connected is True and feed.state.error is None


def test_the_backoff_grows_instead_of_hot_looping():
    feed, conn, clock, lines = make_feed(
        [LOGIN_OK, ConnectionResetError("x")],
        [LOGIN_OK, ConnectionResetError("y")], [LOGIN_OK])
    feed.poll()
    feed.poll()                                  # the read dies
    assert feed.poll() == 0                      # 1s backoff
    clock.advance(1.0)
    feed.poll()
    feed.poll()
    assert len(conn.sockets) == 2
    clock.advance(1.0)
    assert feed.poll() == 0                      # 2s now, not 1s
    assert len(conn.sockets) == 2
    clock.advance(1.0)
    feed.poll()
    assert len(conn.sockets) == 3
    assert any(x[0] == "sleep" for x in lines if isinstance(x, tuple))


def test_a_send_on_a_dead_socket_drops_the_connection_rather_than_raising():
    feed, conn, _, _ = make_feed([LOGIN_OK], [LOGIN_OK])
    feed.poll()

    def dead(_text):
        raise ConnectionResetError("broken pipe")

    conn.sockets[0].send = dead
    feed.subscribe(["QQQ"])                      # no exception reaches here
    assert feed.state.connected is False
    assert "send failed" in feed.state.error
    assert feed.symbols == ["QQQ", "TQQQ"]       # and the reconnect wants both


def test_a_refused_subscription_is_logged_and_the_socket_stays():
    refused = json.dumps({"response": [{"service": "LEVELONE_EQUITIES",
                                        "command": "ADD",
                                        "content": {"code": 22,
                                                    "msg": "Symbol not found"}}]})
    feed, _, _, lines = make_feed([LOGIN_OK, refused])
    assert feed.poll() == 1
    assert feed.state.connected is True
    assert any("LEVELONE_EQUITIES ADD refused (22)" in str(x) for x in lines)


def test_a_login_refusal_mid_stream_drops_the_socket_the_same_way():
    feed, conn, clock, _ = make_feed([LOGIN_OK, login_denied(code=9, msg="bad")],
                                     [LOGIN_OK])
    feed.poll()
    assert feed.state.connected is False and conn.sockets[0].closed
    assert "login denied (9)" in feed.state.error
    clock.advance(mod.AUTH_BACKOFF_S - 1)
    assert feed.poll() == 0 and len(conn.sockets) == 1


# ---- state and health -------------------------------------------------------

def test_the_state_fields_track_the_connection_the_frames_and_the_bars():
    feed, conn, clock, _ = make_feed([LOGIN_OK])
    assert feed.state.as_status() == {"connected": False, "last_frame_at": None,
                                      "last_bar_at": None, "error": None,
                                      "symbols": []}
    feed.poll()
    assert feed.state.connected is True and feed.state.symbols == ("TQQQ",)
    assert feed.state.last_frame_at is None

    sock = conn.sockets[0]
    clock.advance(10)
    sock.script.append(HEARTBEAT)
    assert feed.poll() == 1                      # a heartbeat is a frame
    assert feed.state.last_frame_at == clock.t and feed.state.last_bar_at is None

    clock.advance(10)
    sock.script.append(chart_frame(candle("TQQQ", DAY.replace(hour=9, minute=31))))
    feed.poll()
    assert feed.state.last_bar_at == clock.t
    assert feed.state.as_status()["last_bar_at"].endswith("+00:00")


def test_closing_drops_the_socket_and_the_subscription():
    feed, conn, _, _ = make_feed([LOGIN_OK])
    feed.poll()
    assert feed.state.symbols == ("TQQQ",)
    feed.close()
    assert conn.sockets[0].closed and feed.state.connected is False
    assert feed.state.symbols == ()


def test_health_reads_the_feeds_own_state():
    feed, _, clock, _ = make_feed([LOGIN_OK])
    feed.poll()
    now_et = datetime(2026, 9, 18, 11, 0, tzinfo=ET)
    clock.t = now_et.timestamp()
    feed.state.last_frame_at = feed.state.last_bar_at = clock.t - 5
    assert feed.health(["TQQQ"], now_et=now_et).ok
    clock.advance(200)
    silent = feed.health(["TQQQ"], now_et=now_et)
    assert silent.silent and "no frame" in silent.reason
    assert feed.health(["SPY"], now_et=now_et).missing == ("SPY",)


# ---- second bars ------------------------------------------------------------

def test_second_bars_are_off_unless_a_caller_asks_for_them():
    got = []
    feed, _, _, _ = make_feed([LOGIN_OK, quote_frame({"key": "TQQQ", "3": 72.5,
                                                      "8": 1000})],
                              on_second_bars=lambda day, bars: got.append(bars))
    feed.poll()
    assert got == []


def test_second_bars_bucket_l1_prints_and_take_volume_as_the_day_delta():
    """Per-trade sizes are not in the L1 field set, so a second's volume is
    the delta of the cumulative day volume (field 8)."""
    got = []
    feed, conn, clock, _ = make_feed(
        [LOGIN_OK], symbols=("TQQQ",), second_symbols=lambda: {"TQQQ"},
        on_second_bars=lambda day, bars: got.append((day, bars)))
    clock.t = DAY.replace(hour=10, minute=0).timestamp()
    feed.poll()                                  # connect; nothing to read
    sock = conn.sockets[0]

    def tick(px, vol, at):
        clock.t = DAY.replace(hour=10, minute=0).timestamp() + at
        sock.script.append(quote_frame({"key": "TQQQ", "3": px, "8": vol}))
        feed.poll()

    tick(72.50, 1000, 0.0)      # seeds the cumulative volume
    tick(72.55, 1400, 0.4)
    tick(72.40, 1500, 0.8)
    tick(72.60, 2000, 1.4)      # a new second: the 10:00:00 bar is complete
    day, bars = got[0]
    assert day == DAY.date()
    assert len(bars) == 1
    sym, (start_ms, o, h, l, c, v) = bars[0]
    assert sym == "TQQQ" and start_ms == 36000000
    assert (o, h, l, c) == (72.50, 72.55, 72.40, 72.40)
    # per-trade sizes are not on the wire, so the bar carries the day
    # volume's growth inside the second: 400 + 100
    assert v == pytest.approx(500.0)


def test_the_session_close_is_the_engines_own_calendar():
    assert mod.session_close_ms(DAY.date()) == 16 * 3600 * 1000
    assert mod.session_close_ms(HALF.date()) == 13 * 3600 * 1000


# ---- plumbing ---------------------------------------------------------------

def test_the_feed_entry_point_resolves_and_passes_the_shape_check():
    from dqengine import feeds
    assert feeds.load_class("schwab") is SchwabQuoteFeed
    assert "schwab" in feeds.available()


def test_the_websocket_package_is_imported_lazily():
    """Installing the plugin to trade must not pull a websocket client in:
    it is reached only when a real socket is opened."""
    source = inspect.getsource(mod)
    assert "from websockets" not in source.split("def _connect_websocket")[0]
    assert "from websockets.sync.client import connect" in inspect.getsource(
        mod._connect_websocket)


def test_the_token_and_the_streamer_lookup_are_both_injectable():
    """A host with its own token store hands the feed a callable; nothing
    here reads a file or reaches Schwab."""
    calls = []
    feed = SchwabQuoteFeed(token=lambda: calls.append("token") or "T",
                           streamer_info=lambda t: calls.append(t) or dict(INFO),
                           connect=Connector([LOGIN_OK]), clock=Clock(),
                           log=lambda m: None)
    feed.poll()
    assert calls == ["token", "T"] and feed.state.connected is True
