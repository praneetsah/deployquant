"""Alpaca market-data websocket (v2 stream).

    wss://stream.data.alpaca.markets/v2/{iex|sip}

`iex` is the default because real-time SIP needs a paid data subscription;
the free plan streams IEX, which is one exchange (a few percent of volume).
Set APCA_API_DATA_FEED=sip, or pass `feed="sip"`, with the subscription.
History is unaffected — `dqengine data fetch` gets free SIP bars.

Credentials are the same two environment variables the history client
uses: APCA_API_KEY_ID and APCA_API_SECRET_KEY.

The wire protocol, as documented: connect, receive
`[{"T":"success","msg":"connected"}]`, send `{"action":"auth",...}`,
receive `[{"T":"success","msg":"authenticated"}]`, then
`{"action":"subscribe","bars":[...],...}`. Data frames are JSON arrays of
objects keyed by `T`: `b` bar, `u` updated bar, `t` trade, `q` quote,
`subscription`, `error`. Every frame is an array, even a single message.

Failure postures, each because the quiet version is worse:
  * a bar that is not a bar is dropped with a line naming it, and the
    stream carries on;
  * a lost connection is retried with exponential backoff, and the
    CURRENT symbol set is re-subscribed on the new socket;
  * error 406 (connection limit exceeded) means another process holds the
    one connection this account gets. Reconnecting immediately would
    produce a hot loop against the venue, so it takes the long floor;
  * the feed never raises out of `poll()`. The loop calling it has a
    strategy to run, and `state` plus the silence check are how trouble
    is meant to surface.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

from dqengine.feed import KEY_ENV, SECRET_ENV, bars_to_days
from dqengine.feeds.base import (FeedState, MinuteBar, QuoteTick, bar_is_sane,
                                 say)
from dqengine.feeds.health import SilencePolicy, check_feed

FEED_ENV = "APCA_API_DATA_FEED"
STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"

# documented stream error codes worth naming
CONNECTION_LIMIT = 406
AUTH_CODES = {401: "not authenticated", 402: "auth failed",
              404: "auth timeout", 409: "insufficient subscription"}

BACKOFF_S = 1.0                 # first retry after a dropped socket
BACKOFF_MAX_S = 60.0
CONN_LIMIT_BACKOFF_S = 30.0     # 406: another process owns the connection
HANDSHAKE_FRAMES = 5            # `connected`, maybe a stray, then `authenticated`
HANDSHAKE_TIMEOUT_S = 20.0


class FeedError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def _connect_websocket(url: str):
    """The real socket. Imported here so `pip install deployquant` stays
    numpy+pandas for backtesting; the extra is `deployquant[live]`."""
    try:
        from websockets.sync.client import connect
    except ImportError as e:                                  # pragma: no cover
        raise FeedError("the live feed needs the websockets package: "
                        "pip install 'deployquant[live]'") from e
    return connect(url)


class AlpacaQuoteFeed:
    """Bars and L1 for a changing symbol set. One socket, pumped by
    `poll()`; see dqengine.feeds.base for the contract."""

    def __init__(self, key_id: Optional[str] = None, secret_key: Optional[str] = None,
                 *, feed: Optional[str] = None, store=None, on_bar=None,
                 on_quote=None, symbols: Iterable[str] = (), url: Optional[str] = None,
                 connect=None, sleep=time.sleep, clock=time.time, log=say):
        self.key_id = key_id if key_id is not None else os.environ.get(KEY_ENV, "")
        self.secret_key = (secret_key if secret_key is not None
                           else os.environ.get(SECRET_ENV, ""))
        self.feed = feed or os.environ.get(FEED_ENV, "").strip() or "iex"
        self.url = url or STREAM_URL.format(feed=self.feed)
        self.store = store
        self.on_bar = on_bar
        self.on_quote = on_quote
        self.state = FeedState()
        self._symbols = {s.upper() for s in symbols}
        self._connect = connect or _connect_websocket
        self._sleep, self._clock, self._log = sleep, clock, log
        self._sock = None
        self._backoff = BACKOFF_S
        self._retry_at = 0.0
        self._rest = None          # the REST bar client, built on first use

    # ---- subscriptions ----------------------------------------------------

    @property
    def symbols(self) -> list:
        """What this feed has been ASKED to carry. `state.symbols` is what
        the venue confirmed; the two differ while a subscribe is in flight
        and after a rejection, which is the point of keeping both."""
        return sorted(self._symbols)

    def subscribe(self, symbols: Iterable[str]) -> None:
        fresh = {s.upper() for s in symbols} - self._symbols
        self._symbols |= fresh
        if fresh and self._sock is not None:
            self._send_subscription("subscribe", fresh)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        gone = {s.upper() for s in symbols} & self._symbols
        self._symbols -= gone
        if gone and self._sock is not None:
            self._send_subscription("unsubscribe", gone)

    def _send_subscription(self, action: str, symbols) -> None:
        keys = sorted(symbols)
        self._send({"action": action, "bars": keys, "updatedBars": keys,
                    "trades": keys, "quotes": keys})

    def _send(self, payload: dict) -> None:
        """A write on a socket the venue has already closed must not raise
        into a caller that was only changing its symbol set: drop the
        connection, and the reconnect re-subscribes what is wanted then."""
        try:
            self._sock.send(json.dumps(payload))
        except Exception as e:                                # noqa: BLE001
            self._fail(f"send failed: {e!r}")

    # ---- connection -------------------------------------------------------

    def _ensure_connected(self, timeout: float) -> bool:
        if self._sock is not None:
            return True
        now = self._clock()
        if now < self._retry_at:
            # wait inside the caller's budget rather than spinning
            self._sleep(min(timeout, self._retry_at - now))
            return False
        sock = None
        try:
            sock = self._connect(self.url)
            self._handshake(sock)
        except Exception as e:                                # noqa: BLE001
            self._sock = sock          # so _fail closes the half-open socket
            code = getattr(e, "code", None)
            if code == CONNECTION_LIMIT:
                self._backoff = max(self._backoff, CONN_LIMIT_BACKOFF_S)
            self._fail(f"connect failed: {e}")
            return False
        self._sock = sock
        self.state.connected = True
        self.state.error = None
        self._log(f"[feed] connected to {self.url}; "
                  f"watching {sorted(self._symbols)}")
        if self._symbols:
            self._send_subscription("subscribe", self._symbols)
        # a subscribe that failed has already dropped the socket
        return self._sock is not None

    def _handshake(self, sock) -> None:
        sock.send(json.dumps({"action": "auth", "key": self.key_id,
                              "secret": self.secret_key}))
        for _ in range(HANDSHAKE_FRAMES):
            for msg in _frames(sock.recv(timeout=HANDSHAKE_TIMEOUT_S)):
                if msg.get("T") == "error":
                    code = _int_or_none(msg.get("code"))
                    raise FeedError(f"stream error {code}: "
                                    f"{msg.get('msg') or AUTH_CODES.get(code, '')}",
                                    code=code)
                if msg.get("T") == "success" and msg.get("msg") == "authenticated":
                    return
        raise FeedError("no authentication response from the stream")

    def _fail(self, reason: str) -> None:
        """Drop the socket, record why, and schedule the retry."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:                                 # noqa: BLE001
                pass
        self._sock = None
        self.state.connected = False
        self.state.symbols = ()        # the subscription died with the socket
        self.state.error = reason[:300]
        self._retry_at = self._clock() + self._backoff
        self._log(f"[feed] {reason} — retrying in {self._backoff:.0f}s")
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:                                 # noqa: BLE001
                pass
        self._sock = None
        self.state.connected = False
        self.state.symbols = ()

    # ---- the pump ---------------------------------------------------------

    def poll(self, timeout: float = 1.0) -> int:
        if not self._ensure_connected(timeout):
            return 0
        try:
            raw = self._sock.recv(timeout=timeout)
        except TimeoutError:
            return 0
        except Exception as e:                                # noqa: BLE001
            self._fail(f"read failed: {e!r}")
            return 0
        self.state.last_frame_at = self._clock()
        # a frame — not merely a socket that opened — is what proves the
        # connection works. Resetting on connect turns a socket that dies
        # the moment it opens into a one-second reconnect loop.
        self._backoff = BACKOFF_S
        try:
            return self._handle(raw)
        except Exception as e:                                # noqa: BLE001
            # a frame this code cannot digest must not take the feed down
            self._log(f"[feed] dropped unhandled frame: {e!r}")
            return 0

    def _handle(self, raw) -> int:
        try:
            messages = _frames(raw)
        except (TypeError, ValueError) as e:
            self._log(f"[feed] dropped malformed frame ({e}): {raw!r:.200}")
            return 0
        bars, handled = [], 0
        for msg in messages:
            if not isinstance(msg, dict):
                self._log(f"[feed] dropped malformed message: {msg!r:.200}")
                continue
            handled += 1
            kind = msg.get("T")
            if kind in ("b", "u"):
                bar = self._parse_bar(msg)
                if bar is not None:
                    bars.append(bar)
            elif kind == "t":
                self._emit_quote(_trade_tick(msg))
            elif kind == "q":
                self._emit_quote(_quote_tick(msg))
            elif kind == "subscription":
                self.state.symbols = tuple(sorted(
                    str(s).upper() for s in (msg.get("bars") or [])))
                self._log(f"[feed] subscribed: {list(self.state.symbols)}")
            elif kind == "error":
                self._on_error(msg)
            elif kind == "success":
                pass
            else:
                self._log(f"[feed] dropped unknown message type {kind!r}")
        if bars:
            self._publish(bars)
        return handled

    def _publish(self, bars) -> None:
        """Store the WHOLE frame, then announce what changed. A consumer
        woken by the first callback pulls from the store, so a half-written
        minute must never be reachable."""
        stored = self.store.write(bars) if self.store is not None else list(bars)
        if not stored:
            return
        self.state.last_bar_at = self._clock()
        if self.on_bar is None:
            return
        for bar in stored:
            try:
                self.on_bar(bar)
            except Exception as e:                            # noqa: BLE001
                self._log(f"[feed] bar callback failed for {bar.symbol}: {e!r}")

    def _emit_quote(self, tick) -> None:
        if tick is None:
            return
        if self.on_quote is None:
            return
        try:
            self.on_quote(tick)
        except Exception as e:                                # noqa: BLE001
            self._log(f"[feed] quote callback failed for {tick.symbol}: {e!r}")

    def _parse_bar(self, msg: dict):
        symbol = str(msg.get("S") or "").upper()
        if not symbol:
            self._log(f"[feed] dropped bar with no symbol: {msg!r:.200}")
            return None
        if not bar_is_sane(msg.get("o"), msg.get("h"), msg.get("l"), msg.get("c")):
            self._log(f"[feed] dropped malformed {symbol} bar: {msg!r:.200}")
            return None
        try:
            # bars_to_days is the one place a vendor bar becomes an exchange
            # minute, so the live day and the backfill agree on both the
            # timestamp and where the regular session ends (13:00 half-days)
            days = bars_to_days([msg])
        except Exception as e:                                # noqa: BLE001
            self._log(f"[feed] dropped unreadable {symbol} bar ({e!r}): {msg!r:.200}")
            return None
        for day, rows in days.items():
            ms, o, h, l, c, v = rows[0]
            return MinuteBar(symbol, day, int(ms), o, h, l, c, v)
        return None        # outside the regular session: not a bar we store

    def _on_error(self, msg: dict) -> None:
        code = _int_or_none(msg.get("code"))
        text = msg.get("msg") or AUTH_CODES.get(code, "")
        if code == CONNECTION_LIMIT:
            self._backoff = max(self._backoff, CONN_LIMIT_BACKOFF_S)
            self._fail(f"stream error {code} ({text}): another connection "
                       "holds this account's stream slot")
        elif code in AUTH_CODES:
            self._fail(f"stream error {code}: {text}")
        else:
            self._log(f"[feed] stream error {code}: {text}")

    # ---- the REST half of the same vendor ---------------------------------

    def refresh_bars(self, symbol: str) -> int:
        """Alpaca's record of `symbol`'s recent minutes, into `bar_days`;
        returns the number of days it changed (`BarRefresher` in
        dqengine.feeds.base).

        This is what a worker asks for while this stream is quiet, so it
        reads the same account and the same tape the socket above does: an
        install streaming SIP refreshes from SIP, one on the free IEX tape
        refreshes from IEX, and the bars a replay then steps on came from
        the feed it thinks it is running.

        The bookkeeping is `dqengine.live.history.refresh` -- the session
        window, the in-progress minute, the merge with what is already
        stored, the per-symbol lock. It is imported here rather than at the
        top of the file because it reaches a database: a backtest-only
        install streams bars without ever asking for this."""
        from dqengine.live import history
        return history.refresh(symbol, feed=self._rest_bars())

    def _rest_bars(self):
        """The REST bar client, built once and kept: a host that refreshes
        58 symbols a minute must not mint a client per symbol."""
        if self._rest is None:
            from dqengine.live.history import refresh_feed
            self._rest = refresh_feed(self._rest_creds, feed=self.feed)
        return self._rest

    def _rest_creds(self) -> tuple:
        """This feed's credentials, and the standard environment variables
        when it was built without any -- the same two names and the same
        refusal a self-hoster gets from `dqengine data fetch`."""
        if self.key_id and self.secret_key:
            return self.key_id, self.secret_key
        from dqengine.live.history import env_creds
        return env_creds()

    # ---- health -----------------------------------------------------------

    def health(self, symbols: Iterable[str] = (), now_et=None,
               policy: Optional[SilencePolicy] = None):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now_et = now_et or datetime.now(ZoneInfo("America/New_York"))
        return check_feed(self.state, symbols or self._symbols, now_et,
                          policy, now=self._clock())


def _frames(raw) -> list:
    """The wire carries a JSON array even for one message."""
    data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    return data if isinstance(data, list) else [data]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(iso) -> Optional[int]:
    if not iso:
        return None
    from datetime import datetime
    try:
        text = str(iso).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _trade_tick(msg: dict):
    symbol = str(msg.get("S") or "").upper()
    if not symbol:
        return None
    at = _epoch_ms(msg.get("t"))
    return QuoteTick(symbol, at_ms=at or 0, last=_float_or_none(msg.get("p")),
                     last_at_ms=at)


def _quote_tick(msg: dict):
    symbol = str(msg.get("S") or "").upper()
    if not symbol:
        return None
    return QuoteTick(symbol, at_ms=_epoch_ms(msg.get("t")) or 0,
                     bid=_float_or_none(msg.get("bp")),
                     ask=_float_or_none(msg.get("ap")))
