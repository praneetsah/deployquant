"""The Alpaca v2 stream, driven by a scripted socket.

Frames are written from the documented message format: an array of
objects keyed by `T` (`b` bar, `u` updated bar, `t` trade, `q` quote,
`subscription`, `error`). No test opens a connection.
"""
import inspect
import json

from conftest import (HANDSHAKE, Clock, Connector, RecordingStore, bar_msg, et,
                      quote_msg, sub_msg, trade_msg)

from dqengine.feeds.alpaca import CONN_LIMIT_BACKOFF_S, AlpacaQuoteFeed
from dqengine.feeds.base import MinuteZipStore, QuoteBoard
from dqengine.runtime.core.data import DataStore

DAY = et(2026, 9, 18)                    # a normal Friday session
HALF = et(2026, 11, 27)                  # 13:00 close


def make_feed(*scripts, symbols=("TQQQ",), store=None, on_bar=None,
              on_quote=None, log=None):
    conn = Connector(*scripts)
    clock = Clock()
    lines = [] if log is None else log
    feed = AlpacaQuoteFeed("key", "secret", store=store, on_bar=on_bar,
                           on_quote=on_quote, symbols=symbols,
                           connect=conn, sleep=lambda s: lines.append(("sleep", s)),
                           clock=clock, log=lines.append)
    return feed, conn, clock, lines


# ---- handshake and subscription --------------------------------------------

def test_the_handshake_authenticates_then_subscribes_the_symbol_set():
    feed, conn, _, _ = make_feed(HANDSHAKE, symbols=("TQQQ", "qqq"))
    feed.poll()
    sent = conn.sockets[0].sent
    assert sent[0] == {"action": "auth", "key": "key", "secret": "secret"}
    assert sent[1] == {"action": "subscribe", "bars": ["QQQ", "TQQQ"],
                       "updatedBars": ["QQQ", "TQQQ"], "trades": ["QQQ", "TQQQ"],
                       "quotes": ["QQQ", "TQQQ"]}
    assert feed.state.connected is True
    assert feed.state.error is None


def test_the_url_carries_the_feed_and_iex_is_the_default():
    feed, conn, _, _ = make_feed(HANDSHAKE)
    feed.poll()
    assert conn.urls == ["wss://stream.data.alpaca.markets/v2/iex"]
    sip = AlpacaQuoteFeed("k", "s", feed="sip", connect=Connector())
    assert sip.url.endswith("/v2/sip")


def test_the_feed_and_keys_come_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv("APCA_API_DATA_FEED", "sip")
    monkeypatch.setenv("APCA_API_KEY_ID", "envkey")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "envsecret")
    feed = AlpacaQuoteFeed()
    assert feed.feed == "sip" and feed.url.endswith("/v2/sip")
    assert (feed.key_id, feed.secret_key) == ("envkey", "envsecret")


def test_a_denied_handshake_is_recorded_and_retried_not_raised():
    script = HANDSHAKE[:1] + [json.dumps([{"T": "error", "code": 402,
                                           "msg": "auth failed"}])]
    feed, _, _, _ = make_feed(script)
    assert feed.poll() == 0
    assert feed.state.connected is False
    assert "402" in feed.state.error


def test_subscribe_and_unsubscribe_while_connected_send_only_the_difference():
    feed, conn, _, _ = make_feed(HANDSHAKE, symbols=("TQQQ",))
    feed.poll()
    feed.subscribe(["qqq", "TQQQ"])
    feed.unsubscribe(["TQQQ", "SPY"])
    sent = conn.sockets[0].sent
    assert sent[2] == {"action": "subscribe", "bars": ["QQQ"], "updatedBars": ["QQQ"],
                       "trades": ["QQQ"], "quotes": ["QQQ"]}
    assert sent[3] == {"action": "unsubscribe", "bars": ["TQQQ"],
                       "updatedBars": ["TQQQ"], "trades": ["TQQQ"],
                       "quotes": ["TQQQ"]}
    assert feed.symbols == ["QQQ"]


def test_a_subscribe_on_a_dead_socket_drops_the_connection_rather_than_raising():
    feed, conn, _, _ = make_feed(HANDSHAKE, HANDSHAKE)
    feed.poll()

    def dead(_text):
        raise ConnectionResetError("broken pipe")

    conn.sockets[0].send = dead
    feed.subscribe(["QQQ"])                      # no exception reaches here
    assert feed.state.connected is False
    assert "send failed" in feed.state.error
    assert feed.symbols == ["QQQ", "TQQQ"]       # and the reconnect wants both


class FailOnSubscribe:
    """A socket whose auth write lands and whose subscribe write dies."""

    def __init__(self, inner):
        self.inner = inner
        self.sent = inner.sent
        self.closed = False

    def send(self, text):
        if self.sent:
            raise ConnectionResetError("broken pipe")
        self.inner.send(text)

    def recv(self, timeout=None):
        return self.inner.recv(timeout)

    def close(self):
        self.closed = True
        self.inner.close()


def test_a_subscribe_that_fails_on_a_fresh_socket_leaves_no_half_connection():
    conn, clock, opened = Connector(HANDSHAKE, HANDSHAKE), Clock(), []

    def connect(url):
        opened.append(url)
        sock = conn(url)
        return FailOnSubscribe(sock) if len(opened) == 1 else sock

    feed = AlpacaQuoteFeed("k", "s", symbols=("TQQQ",), connect=connect,
                           sleep=lambda s: None, clock=clock, log=lambda m: None)
    assert feed.poll() == 0
    assert feed.state.connected is False and "send failed" in feed.state.error
    assert feed.poll() == 0                      # the next poll finds no socket
    clock.advance(5)
    feed.poll()
    assert feed.state.connected is True


def test_subscribing_while_disconnected_sends_nothing_and_waits_for_the_socket():
    feed, conn, _, _ = make_feed(HANDSHAKE)
    feed.subscribe(["SPY"])
    assert conn.sockets == []
    feed.poll()
    assert conn.sockets[0].sent[1]["bars"] == ["SPY", "TQQQ"]


# ---- bars -------------------------------------------------------------------

def test_a_bar_is_in_the_store_before_the_callback_is_told_about_it(tmp_path):
    seen = []

    def on_bar(bar):
        # the callback's whole contract: pull the bar from the store
        loaded = DataStore(str(tmp_path)).load_minute_day(bar.symbol, bar.day)
        seen.append((bar.symbol, bar.start_ms, None if loaded is None else loaded.n))

    feed, _, _, _ = make_feed(
        HANDSHAKE + [json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=31))])],
        store=MinuteZipStore(str(tmp_path)), on_bar=on_bar)
    feed.poll()
    assert seen == [("TQQQ", 34260000, 1)]


def test_a_whole_frame_is_stored_before_any_of_it_is_announced():
    events = []
    frame = [bar_msg("TQQQ", DAY.replace(hour=9, minute=31)),
             bar_msg("QQQ", DAY.replace(hour=9, minute=31), c=400.0, o=399.0,
                     h=401.0, l=398.0)]
    feed, _, _, _ = make_feed(HANDSHAKE + [json.dumps(frame)],
                              store=RecordingStore(events),
                              on_bar=lambda b: events.append(("bar", b.symbol)))
    feed.poll()
    assert events == [("store", ["TQQQ", "QQQ"]), ("bar", "TQQQ"), ("bar", "QQQ")]


def test_a_bar_the_store_already_holds_is_not_announced():
    events = []
    feed, _, _, _ = make_feed(
        HANDSHAKE + [json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=31))])],
        store=RecordingStore(events, changed=lambda bars: []),
        on_bar=lambda b: events.append(("bar", b.symbol)))
    feed.poll()
    assert events == [("store", ["TQQQ"])]
    assert feed.state.last_bar_at is None


def test_an_updated_bar_travels_the_same_path_as_a_bar(tmp_path):
    seen = []
    minute = DAY.replace(hour=9, minute=31)
    feed, _, _, _ = make_feed(
        HANDSHAKE
        + [json.dumps([bar_msg("TQQQ", minute)])]
        + [json.dumps([bar_msg("TQQQ", minute, c=10.9, kind="u")])],
        store=MinuteZipStore(str(tmp_path)), on_bar=lambda b: seen.append(b.close))
    feed.poll()
    feed.poll()
    assert seen == [10.5, 10.9]
    assert list(DataStore(str(tmp_path)).load_minute_day("TQQQ", DAY.date()).close) == [10.9]


def test_an_after_hours_bar_on_an_early_close_day_never_becomes_a_bar():
    events = []
    frame = [bar_msg("TQQQ", HALF.replace(hour=12, minute=59)),
             bar_msg("TQQQ", HALF.replace(hour=13, minute=5))]
    feed, _, _, _ = make_feed(HANDSHAKE + [json.dumps(frame)],
                              store=RecordingStore(events),
                              on_bar=lambda b: events.append(("bar", b.start_ms)))
    feed.poll()
    assert events == [("store", ["TQQQ"]), ("bar", 46740000)]


def test_a_premarket_bar_never_becomes_a_bar():
    events = []
    feed, _, _, _ = make_feed(
        HANDSHAKE + [json.dumps([bar_msg("TQQQ", DAY.replace(hour=8, minute=15))])],
        store=RecordingStore(events), on_bar=lambda b: events.append(("bar", b)))
    feed.poll()
    assert events == []


# ---- malformed input --------------------------------------------------------

def test_a_malformed_bar_is_dropped_loudly_and_the_frame_survives():
    events, lines = [], []
    # low above high, open eight times the price: the observed shape
    frame = [bar_msg("TQQQ", DAY.replace(hour=9, minute=31), o=539.0, h=72.55,
                     l=72.65, c=72.40, v=73),
             bar_msg("QQQ", DAY.replace(hour=9, minute=31))]
    feed, _, _, _ = make_feed(HANDSHAKE + [json.dumps(frame)],
                              store=RecordingStore(events),
                              on_bar=lambda b: events.append(("bar", b.symbol)),
                              log=lines)
    feed.poll()
    assert events == [("store", ["QQQ"]), ("bar", "QQQ")]
    assert any("dropped malformed TQQQ bar" in str(x) for x in lines)


def test_a_frame_that_is_not_json_is_dropped_loudly_and_the_feed_keeps_going():
    events, lines = [], []
    feed, _, _, _ = make_feed(
        HANDSHAKE + ["<html>gateway timeout</html>",
                     json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=31))])],
        store=RecordingStore(events),
        on_bar=lambda b: events.append(("bar", b.symbol)), log=lines)
    feed.poll()
    feed.poll()
    assert events == [("store", ["TQQQ"]), ("bar", "TQQQ")]
    assert any("dropped malformed frame" in str(x) for x in lines)
    assert feed.state.connected is True


def test_a_bar_with_an_unreadable_timestamp_is_dropped_loudly():
    events, lines = [], []
    bad = bar_msg("TQQQ", DAY.replace(hour=9, minute=31))
    bad["t"] = "not-a-time"
    feed, _, _, _ = make_feed(HANDSHAKE + [json.dumps([bad])],
                              store=RecordingStore(events),
                              on_bar=lambda b: events.append(("bar", b)), log=lines)
    feed.poll()
    assert events == []
    assert any("unreadable TQQQ bar" in str(x) for x in lines)


def test_an_unknown_message_type_is_dropped_loudly():
    feed, _, _, lines = make_feed(HANDSHAKE + [json.dumps([{"T": "zzz"}])])
    feed.poll()
    assert any("unknown message type" in str(x) for x in lines)


# ---- quotes and trades ------------------------------------------------------

def test_trades_and_quotes_reach_the_board_with_separate_timestamps():
    board = QuoteBoard()
    traded = DAY.replace(hour=15, minute=40)
    quoted = DAY.replace(hour=15, minute=58, second=59)
    feed, _, _, _ = make_feed(
        HANDSHAKE + [json.dumps([trade_msg("TQQQ", traded, price=72.5),
                                 quote_msg("TQQQ", quoted, bid=72.4, ask=72.6)])],
        on_quote=board.update)
    feed.poll()
    q = board.get("TQQQ")
    assert (q["last"], q["bid"], q["ask"]) == (72.5, 72.4, 72.6)
    # the snapshot carries the time of the last TRADE, not of the bid that
    # ticked 19 minutes later
    assert board.snapshot() == {"TQQQ": {"last": 72.5,
                                         "at_ms": int(traded.timestamp() * 1000)}}


def test_a_non_positive_last_is_omitted_from_the_snapshot():
    board = QuoteBoard()
    feed, _, _, _ = make_feed(
        HANDSHAKE + [json.dumps([trade_msg("TQQQ", DAY.replace(hour=10), price=0.0)])],
        on_quote=board.update)
    feed.poll()
    assert board.get("TQQQ")["last"] == 0.0
    assert board.snapshot() == {}


# ---- connection trouble -----------------------------------------------------

def test_a_dropped_socket_reconnects_and_re_subscribes_the_current_symbol_set():
    feed, conn, clock, _ = make_feed(HANDSHAKE, HANDSHAKE, symbols=("TQQQ",))
    feed.poll()
    assert feed.state.connected is True
    feed.subscribe(["QQQ"])
    conn.sockets[0].script.append(ConnectionResetError("socket died"))
    feed.poll()
    assert feed.state.connected is False and conn.sockets[0].closed
    assert feed.poll() == 0                      # still inside the backoff
    assert len(conn.sockets) == 1
    clock.advance(5)
    feed.poll()
    assert len(conn.sockets) == 2
    assert conn.sockets[1].sent[1]["bars"] == ["QQQ", "TQQQ"]
    assert feed.state.connected is True and feed.state.error is None


def test_the_backoff_grows_instead_of_hot_looping():
    feed, conn, clock, lines = make_feed(
        HANDSHAKE + [ConnectionResetError("x")],
        HANDSHAKE + [ConnectionResetError("y")], HANDSHAKE)
    feed.poll()                                  # connect, then the read dies
    assert feed.poll() == 0                      # 1s backoff
    clock.advance(1.0)
    feed.poll()                                  # reconnect; dies again
    assert len(conn.sockets) == 2
    clock.advance(1.0)
    assert feed.poll() == 0                      # 2s now, not 1s
    assert len(conn.sockets) == 2
    clock.advance(1.0)
    feed.poll()
    assert len(conn.sockets) == 3
    assert any(x[0] == "sleep" for x in lines if isinstance(x, tuple))


def test_the_connection_limit_error_takes_the_long_floor():
    limit = HANDSHAKE[:1] + [json.dumps([{"T": "error", "code": 406,
                                          "msg": "connection limit exceeded"}])]
    feed, conn, clock, _ = make_feed(limit, HANDSHAKE)
    feed.poll()
    assert "406" in feed.state.error
    clock.advance(CONN_LIMIT_BACKOFF_S - 1)
    assert feed.poll() == 0
    assert len(conn.sockets) == 1                # no hot loop against the venue
    clock.advance(2)
    feed.poll()
    assert len(conn.sockets) == 2 and feed.state.connected is True


def test_a_connection_limit_error_mid_stream_drops_the_socket_the_same_way():
    script = HANDSHAKE + [json.dumps([{"T": "error", "code": 406,
                                       "msg": "connection limit exceeded"}])]
    feed, conn, clock, _ = make_feed(script, HANDSHAKE)
    feed.poll()
    assert feed.state.connected is False and conn.sockets[0].closed
    clock.advance(CONN_LIMIT_BACKOFF_S - 1)
    feed.poll()
    assert len(conn.sockets) == 1


def test_a_non_fatal_stream_error_is_logged_and_the_socket_stays():
    feed, _, _, lines = make_feed(HANDSHAKE + [json.dumps(
        [{"T": "error", "code": 500, "msg": "internal error"}])])
    feed.poll()
    assert feed.state.connected is True
    assert any("stream error 500" in str(x) for x in lines)


# ---- state ------------------------------------------------------------------

def test_the_state_fields_track_the_connection_the_frames_and_the_bars():
    feed, conn, clock, _ = make_feed(HANDSHAKE)
    assert feed.state.as_status() == {"connected": False, "last_frame_at": None,
                                      "last_bar_at": None, "error": None,
                                      "symbols": []}
    feed.poll()                                  # connect; the read times out
    assert feed.state.connected is True
    assert feed.state.last_frame_at is None

    sock = conn.sockets[0]
    clock.advance(10)
    sock.script.append(json.dumps([sub_msg(["TQQQ"])]))
    feed.poll()
    assert feed.state.symbols == ("TQQQ",)
    assert feed.state.last_frame_at == clock.t and feed.state.last_bar_at is None

    clock.advance(10)
    sock.script.append(json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=31))]))
    feed.poll()
    assert feed.state.last_bar_at == clock.t
    assert feed.state.as_status()["last_bar_at"].endswith("+00:00")


def test_closing_drops_the_socket_and_the_subscription():
    feed, conn, _, _ = make_feed(HANDSHAKE + [json.dumps([sub_msg(["TQQQ"])])])
    feed.poll()
    assert feed.state.symbols == ("TQQQ",)
    feed.close()
    assert conn.sockets[0].closed and feed.state.connected is False
    assert feed.state.symbols == ()


def test_health_reads_the_feeds_own_state():
    feed, _, clock, _ = make_feed(HANDSHAKE + [json.dumps([sub_msg(["TQQQ"])])])
    feed.poll()
    now_et = et(2026, 9, 18, 11, 0)
    clock.t = now_et.timestamp()
    feed.state.last_frame_at = feed.state.last_bar_at = clock.t - 5
    assert feed.health(["TQQQ"], now_et=now_et).ok
    clock.advance(200)
    silent = feed.health(["TQQQ"], now_et=now_et)
    assert silent.silent and "no frame" in silent.reason
    # asking about a symbol this feed does not carry is its own verdict
    assert feed.health(["SPY"], now_et=now_et).missing == ("SPY",)


def test_a_callback_that_raises_does_not_take_the_feed_down():
    def boom(_bar):
        raise RuntimeError("strategy blew up")

    feed, _, _, lines = make_feed(
        HANDSHAKE + [json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=31))]),
                     json.dumps([bar_msg("TQQQ", DAY.replace(hour=9, minute=32))])],
        on_bar=boom)
    feed.poll()
    feed.poll()
    assert feed.state.connected is True
    assert sum("bar callback failed" in str(x) for x in lines) == 2


def test_the_websocket_package_is_imported_lazily():
    """`pip install deployquant` stays numpy+pandas for backtesting: the
    websocket client is only reached when a real socket is opened."""
    import dqengine.feeds.alpaca as mod
    source = inspect.getsource(mod)
    head = source.split("class AlpacaQuoteFeed")[0]
    assert "from websockets" not in head.split("def _connect_websocket")[0]
    assert "from websockets.sync.client import connect" in inspect.getsource(
        mod._connect_websocket)
