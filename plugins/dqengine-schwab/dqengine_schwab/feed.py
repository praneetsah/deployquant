"""Schwab market-data websocket (the Trader API streamer).

The socket url is not fixed: `GET trader/v1/userPreference` returns a
`streamerInfo` block carrying the url and the four identifiers every
request has to repeat. Connect there, send an ADMIN/LOGIN request with a
fresh access token, read the response code, then subscribe.

Two services are subscribed, for two different jobs:

  * `CHART_EQUITY` pushes one candle per symbol the moment the minute
    closes. Those are the bars the engine runs on;
  * `LEVELONE_EQUITIES` pushes sub-second quotes. They price a scheduled
    fire and they detect a breached simulated stop. They never reach the
    engine as bars, and no sub-minute bar is synthesized from them.

The socket is not the only way to reach this vendor's minutes:
`refresh_bars` fetches the recent sessions over REST (`dqengine_schwab.bars`)
for a stream that has gone quiet.

Each service is subscribed with its own key list, because a feed usually
wants quotes for more symbols than it wants bars for (market anchors, a
symbol someone just looked up). `SUBS` replaces a service's whole key
list, `ADD` extends it, `UNSUBS` removes keys.

The chart field layout is the interesting part of this file; see
`parse_chart_fields`.

Failure postures, each because the quiet version is worse:

  * a candle that is not a bar is dropped with a line naming it, and the
    stream carries on;
  * a lost connection is retried with exponential backoff, and the
    CURRENT symbol sets are re-subscribed on the new socket;
  * a refresh token Schwab has expired, and a login it denies, take a
    long backoff and say in `state.error` that the account owner has to
    authorize the app again. Schwab expires a refresh token seven days
    after it is issued, so this happens every week to an installation
    that does not renew it, and a dead feed that never says why is the
    outcome worth spending code to avoid;
  * the feed never raises out of `poll()`. The loop calling it has a
    strategy to run, and `state` plus the silence check are how trouble
    is meant to surface.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from dqengine.adapters.base import BrokerAuthExpired, BrokerUnavailable
from dqengine.feeds.base import (FeedState, MinuteBar, QuoteTick, bar_is_sane,
                                 in_regular_session)
from dqengine.feeds.health import SilencePolicy, check_feed

from . import oauth

ET = ZoneInfo("America/New_York")

USER_PREFERENCE_URL = "https://api.schwabapi.com/trader/v1/userPreference"

CHART_SERVICE = "CHART_EQUITY"
QUOTE_SERVICE = "LEVELONE_EQUITIES"

# CHART_EQUITY: 0 key, 1..6 the OHLCV block (see parse_chart_fields), 7 the
# candle's start in epoch ms, 8 the chart day
CHART_FIELDS = "0,1,2,3,4,5,6,7,8"
# LEVELONE fields: 0 sym, 1 bid, 2 ask, 3 last, 8 volume, 10 high, 11 low,
# 12 prev close, 17 open, 32 security status (Normal/Halted/Closed)
QUOTE_FIELDS = "0,1,2,3,8,10,11,12,17,32"
_QUOTE_KEYS = {"1": "bid", "2": "ask", "3": "last", "8": "volume",
               "10": "high", "11": "low", "12": "prev_close", "17": "open",
               "32": "status"}

BACKOFF_S = 1.0                 # first retry after a dropped socket
BACKOFF_MAX_S = 60.0
AUTH_BACKOFF_S = 300.0          # a dead refresh token needs a person, not a retry
LOGIN_TIMEOUT_S = 20.0

HALF_DAY_CLOSE_MS = 13 * 3600 * 1000
FULL_DAY_CLOSE_MS = 16 * 3600 * 1000


class FeedError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None,
                 auth: bool = False):
        super().__init__(message)
        self.code = code
        self.auth = auth        # a person has to re-authorize; do not hot-loop


# ---- the wire protocol, as pure functions ----------------------------------
# The hosted platform's own streamer imports these rather than holding a
# second copy of them.

def parse_chart_fields(bar: dict):
    """CHART_EQUITY content -> (o, h, l, c, v), or None if unparseable.

    Two field layouts exist in the wild. Schwab's OFFICIAL documentation
    (developer.schwab.com Trader API streamer guide, checked 2026-08-19)
    specifies 1=open, 2=high, 3=low, 4=close, 5=volume, 6=sequence — the
    layout this code was originally written against, and what the stream
    delivered until ~2026-08-18. Since then the production stream has been
    sending a SHIFTED, doc-violating layout — 1=sequence, 2=open, 3=high,
    4=low, 5=close, 6=volume (field 1 increments by exactly 1 per minute) —
    which made every streamed bar fail bar_is_sane and get dropped. Because
    the wire contradicts the docs, Schwab may revert without notice, so
    accept both: a bar that is sane under one layout is structurally
    impossible under the other (the sequence integer never fits inside the
    OHLC range), so this cannot mis-pick."""
    for o_k, h_k, l_k, c_k, v_k in (("2", "3", "4", "5", "6"),
                                    ("1", "2", "3", "4", "5")):
        o, h, l, c = bar.get(o_k), bar.get(h_k), bar.get(l_k), bar.get(c_k)
        if bar_is_sane(o, h, l, c):
            return o, h, l, c, bar.get(v_k, 0)
    return None


def merge_quote(store: dict, sym: str, content: dict, now_iso: str) -> dict:
    """Fold one (possibly partial — the stream conflates and sends only
    changed fields) LEVELONE frame into the quote board."""
    q = store.setdefault(sym, {})
    for k, name in _QUOTE_KEYS.items():
        if k in content and content[k] is not None:
            q[name] = content[k]
    q["at"] = now_iso
    # `at` is the frame time -- ANY field change (bid/ask/volume) moves it,
    # and a market page reads it as such. The quote snapshot the engine
    # primes from needs the time of the last TRADE: a thin ETF whose last
    # print was 15:40 but whose bid ticked at 15:58:59 would otherwise
    # report ~1 s of staleness for a 19-minute-old price.
    if content.get("3") is not None:
        q["last_at"] = now_iso
    return q


def quote_tick(sym: str, content: dict, at_ms: int) -> QuoteTick:
    """One LEVELONE frame as a `QuoteTick`, partial like the frame itself.

    The subscribed field set carries no per-field timestamp, so `at_ms` is
    when the frame arrived. `last_at_ms` is set only when this frame
    actually carried a last price, which is the same at/last_at
    distinction `merge_quote` keeps on the board."""
    last = _float_or_none(content.get("3"))
    return QuoteTick(sym, at_ms=at_ms,
                     bid=_float_or_none(content.get("1")),
                     ask=_float_or_none(content.get("2")),
                     last=last,
                     last_at_ms=at_ms if last is not None else None)


def login_request(info: dict, token: str, request_id: str = "1") -> dict:
    """The ADMIN/LOGIN request. `info` is a `streamerInfo` block."""
    return {"requests": [{
        "requestid": request_id, "service": "ADMIN", "command": "LOGIN",
        "SchwabClientCustomerId": info["schwabClientCustomerId"],
        "SchwabClientCorrelId": info["schwabClientCorrelId"],
        "parameters": {"Authorization": token,
                       "SchwabClientChannel": info["schwabClientChannel"],
                       "SchwabClientFunctionId": info["schwabClientFunctionId"]}}]}


def subs_request(info: dict, request_id: str, service: str, command: str,
                 symbols, fields: str) -> dict:
    """A SUBS / ADD / UNSUBS request for one service."""
    return {"requests": [{
        "requestid": request_id, "service": service, "command": command,
        "SchwabClientCustomerId": info["schwabClientCustomerId"],
        "SchwabClientCorrelId": info["schwabClientCorrelId"],
        "parameters": {"keys": ",".join(sorted(symbols)), "fields": fields}}]}


def chart_bars(content, log=print) -> list:
    """One CHART_EQUITY frame -> the regular-session `MinuteBar`s in it.

    Field 7 is the candle's start in epoch ms. A candle outside the
    regular session is not a bar this stores: Schwab keeps streaming
    candles until 16:00 on a half day, and stored as regular bars they
    fill resting orders on after-hours prints."""
    bars = []
    for bar in content:
        sym = (bar.get("key") or "").upper()
        ts = bar.get("7")
        if not sym or ts is None:
            continue
        parsed = parse_chart_fields(bar)
        if parsed is None:
            log(f"[feed] dropped malformed {sym} chart content: {bar}")
            continue
        o, h, l, c, v = parsed
        try:
            dt = datetime.fromtimestamp(int(ts) / 1000,
                                        tz=timezone.utc).astimezone(ET)
        except (TypeError, ValueError, OSError, OverflowError):
            log(f"[feed] dropped {sym} candle with an unreadable time: {ts!r}")
            continue
        ms = (dt.hour * 3600 + dt.minute * 60) * 1000
        if not in_regular_session(dt.date(), ms):
            continue
        bars.append(MinuteBar(sym, dt.date(), ms, float(o), float(h),
                              float(l), float(c), float(v)))
    return bars


def session_close_ms(day: date) -> int:
    """The session's close in ms since midnight ET: 13:00 on an early-close
    day, 16:00 otherwise. Read off the engine's own calendar rather than a
    second holiday table — 13:00 sits inside a full session and outside a
    half one."""
    return (FULL_DAY_CLOSE_MS if in_regular_session(day, HALF_DAY_CLOSE_MS)
            else HALF_DAY_CLOSE_MS)


# ---- credentials -----------------------------------------------------------

def access_token_from_creds(creds: dict) -> str:
    """A fresh access token from app key, app secret and refresh token,
    through the same refresh the broker adapter uses.

    A rotated refresh token is written back into `creds`, so a feed that
    runs for weeks keeps working against a mapping its owner persists.
    Raises `BrokerAuthExpired` when Schwab has expired the refresh token,
    which it does seven days after issuing it."""
    tok = oauth.schwab_refresh(creds.get("app_key", ""),
                               creds.get("app_secret", ""),
                               creds.get("refresh_token", ""))
    if tok.get("refresh_token"):
        creds["refresh_token"] = tok["refresh_token"]
    token = tok.get("access_token")
    if not token:
        raise BrokerAuthExpired("Schwab returned no access token; "
                                "authorize the app again")
    return token


def fetch_streamer_info(access_token: str) -> dict:
    """The `streamerInfo` block: socket url plus the identifiers every
    request repeats."""
    req = urllib.request.Request(
        USER_PREFERENCE_URL,
        headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            prefs = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:                                     # noqa: BLE001
            pass
        if e.code in (401, 403):
            raise BrokerAuthExpired(f"Schwab refused the streamer lookup "
                                    f"({e.code}): {detail}")
        raise BrokerUnavailable(f"Schwab userPreference {e.code}: {detail}")
    except BrokerAuthExpired:
        raise
    except Exception as e:                                    # noqa: BLE001
        raise BrokerUnavailable(f"could not reach Schwab: {e}")
    info = (prefs.get("streamerInfo") or [{}])[0]
    if not info.get("streamerSocketUrl"):
        raise BrokerUnavailable("Schwab returned no streamer socket url")
    return info


def _connect_websocket(url: str):
    """The real socket. Imported here so installing the plugin stays a
    pure-standard-library broker adapter; the extra is
    `deployquant-schwab[live]`."""
    try:
        from websockets.sync.client import connect
    except ImportError as e:                                  # pragma: no cover
        raise FeedError("the Schwab feed needs the websockets package: "
                        "pip install 'deployquant-schwab[live]'") from e
    return connect(url)


class SchwabQuoteFeed:
    """Minute bars and L1 for a changing symbol set, over one Schwab
    streamer socket, pumped by `poll()`. See dqengine.feeds.base for the
    contract.

    `symbols` are the ones bars are wanted for. `quote_symbols` get
    quotes only, which is what a market strip or a symbol someone looked
    up needs: quotes for those cost nothing on the engine side.

    `token` is a callable returning a fresh access token. It defaults to
    refreshing `creds`, and a host with its own token store passes its
    own callable instead. `connect` and `clock` are injectable for the
    same reason: no test here opens a connection.

    Schwab allows one streamer connection per user, so a process that
    owns the connection owns it for every strategy in it."""

    def __init__(self, creds: Optional[dict] = None, *, token=None,
                 streamer_info=None, store=None, on_bar=None, on_quote=None,
                 symbols: Iterable[str] = (), quote_symbols: Iterable[str] = (),
                 connect=None, sleep=time.sleep, clock=time.time, log=print,
                 second_symbols=None, on_second_bars=None, close_ms=None):
        self.creds = creds if creds is not None else {}
        self.store = store
        self.on_bar = on_bar
        self.on_quote = on_quote
        self.state = FeedState()
        self.quotes: dict = {}        # the full L1 board, folded by merge_quote
        self._symbols = {s.upper() for s in symbols}
        self._quote_symbols = {s.upper() for s in quote_symbols}
        self._token = token or (lambda: access_token_from_creds(self.creds))
        self._streamer_info = streamer_info or fetch_streamer_info
        self._connect = connect or _connect_websocket
        self._sleep, self._clock, self._log = sleep, clock, log
        self._sock = None
        self._info: dict = {}
        self._backoff = BACKOFF_S
        self._retry_at = 0.0
        # second-resolution consolidation, off unless a caller asks for it
        self._second_symbols = second_symbols
        self.on_second_bars = on_second_bars
        self._close_ms = close_ms or session_close_ms
        self._sec = {"cons": None, "day": None, "vol": {}}

    # ---- subscriptions ----------------------------------------------------

    @property
    def symbols(self) -> list:
        """What this feed has been ASKED to carry bars for. `state.symbols`
        is what has been subscribed on a live socket; the two differ while
        the feed is down, which is the point of keeping both."""
        return sorted(self._symbols)

    @property
    def quote_symbols(self) -> list:
        """Every symbol the quote service carries: the bar symbols plus the
        quote-only ones."""
        return sorted(self._symbols | self._quote_symbols)

    def subscribe(self, symbols: Iterable[str]) -> None:
        """Bars and quotes for these symbols."""
        fresh = {s.upper() for s in symbols} - self._symbols
        if not fresh:
            return
        self._symbols |= fresh
        if self._sock is not None:
            self._send_subscription("ADD", fresh, fresh - self._quote_symbols)
            self._note_subscribed()

    def subscribe_quotes(self, symbols: Iterable[str]) -> None:
        """Quotes only. Nothing here reaches the engine as a bar."""
        fresh = ({s.upper() for s in symbols}
                 - self._symbols - self._quote_symbols)
        if not fresh:
            return
        self._quote_symbols |= fresh
        if self._sock is not None:
            self._send_subscription("ADD", (), fresh)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        gone = {s.upper() for s in symbols}
        chart_gone = gone & self._symbols
        quote_gone = gone & (self._symbols | self._quote_symbols)
        if not quote_gone:
            return
        self._symbols -= chart_gone
        self._quote_symbols -= gone
        if self._sock is not None:
            self._send_subscription("UNSUBS", chart_gone, quote_gone)
            self._note_subscribed()

    def _send_subscription(self, command: str, chart_syms, quote_syms) -> None:
        """One request per service. Schwab wants the keys as one
        comma-separated string, and the field list repeated every time."""
        request_id = str(int(self._clock()))
        if chart_syms:
            self._send(subs_request(self._info, request_id, CHART_SERVICE,
                                    command, chart_syms, CHART_FIELDS))
        if quote_syms and self._sock is not None:
            self._send(subs_request(self._info, request_id, QUOTE_SERVICE,
                                    command, quote_syms, QUOTE_FIELDS))

    def _note_subscribed(self) -> None:
        """Schwab's SUBS acknowledgement carries a status code and no key
        list (Alpaca's carries the keys), so what the socket is carrying is
        what was successfully sent on it."""
        if self._sock is not None:
            self.state.symbols = tuple(sorted(self._symbols))

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
            token = self._token()
            info = self._streamer_info(token)
            sock = self._connect(info["streamerSocketUrl"])
            self._login(sock, info, token)
        except BrokerAuthExpired as e:
            self._sock = sock          # so _fail closes the half-open socket
            self._backoff = max(self._backoff, AUTH_BACKOFF_S)
            self._fail(f"Schwab authorization expired: {e}. A Schwab refresh "
                       "token stops working 7 days after it is issued; "
                       "authorize the app again and store the new token")
            return False
        except Exception as e:                                # noqa: BLE001
            self._sock = sock
            if getattr(e, "auth", False):
                self._backoff = max(self._backoff, AUTH_BACKOFF_S)
            self._fail(f"connect failed: {e}")
            return False
        self._sock, self._info = sock, info
        self.state.connected = True
        self.state.error = None
        self._log(f"[feed] connected to the Schwab streamer; "
                  f"watching {sorted(self._symbols)}")
        # SUBS replaces a service's whole key list, so each service gets one
        self._send_subscription("SUBS", self._symbols,
                                self._symbols | self._quote_symbols)
        self._note_subscribed()
        # a subscribe that failed has already dropped the socket
        return self._sock is not None

    def _login(self, sock, info: dict, token: str) -> None:
        sock.send(json.dumps(login_request(info, token)))
        raw = sock.recv(timeout=LOGIN_TIMEOUT_S)
        try:
            content = json.loads(raw)["response"][0]["content"]
            code = int(content["code"])
        except (TypeError, ValueError, KeyError, IndexError) as e:
            raise FeedError(f"unreadable streamer login response: "
                            f"{raw!r:.200}") from e
        if code != 0:
            detail = content.get("msg") or ""
            raise FeedError(f"streamer login denied ({code}) — refresh token "
                            f"may need the weekly re-auth: {detail}",
                            code=code, auth=True)

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
            msg = (json.loads(raw) if isinstance(raw, (str, bytes, bytearray))
                   else raw)
        except (TypeError, ValueError) as e:
            self._log(f"[feed] dropped malformed frame ({e}): {raw!r:.200}")
            return 0
        if not isinstance(msg, dict):
            self._log(f"[feed] dropped malformed frame: {msg!r:.200}")
            return 0
        handled = 0
        for block in (msg.get("data") or []):
            handled += 1
            service = block.get("service")
            if service == CHART_SERVICE:
                bars = chart_bars(block.get("content") or [], log=self._log)
                if bars:
                    self._publish(bars)
            elif service == QUOTE_SERVICE:
                self._on_quotes(block.get("content") or [])
            else:
                self._log(f"[feed] dropped unknown service {service!r}")
        for block in (msg.get("response") or []):
            handled += 1
            self._on_response(block)
        # a heartbeat carries no data and is exactly what it looks like:
        # proof the socket is alive on a quiet tape
        handled += len(msg.get("notify") or [])
        return handled

    def _on_response(self, block: dict) -> None:
        content = block.get("content") or {}
        try:
            code = int(content.get("code"))
        except (TypeError, ValueError):
            code = None
        if code == 0 or code is None:
            return
        service = block.get("service")
        command = block.get("command")
        detail = content.get("msg") or ""
        if service == "ADMIN" and command == "LOGIN":
            self._backoff = max(self._backoff, AUTH_BACKOFF_S)
            self._fail(f"streamer login denied ({code}) — refresh token may "
                       f"need the weekly re-auth: {detail}")
            return
        self._log(f"[feed] {service} {command} refused ({code}): {detail}")

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

    def _on_quotes(self, content) -> None:
        now = self._clock()
        at_ms = int(now * 1000)
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        now_et = datetime.fromtimestamp(now, tz=ET)
        for q in content:
            sym = (q.get("key") or "").upper()
            if not sym:
                continue
            merge_quote(self.quotes, sym, q, now_iso)
            try:
                self._sec_on_quote(sym, q, now_et)
            except Exception as e:                            # noqa: BLE001
                self._log(f"[feed] second consolidation {sym}: {e!r}")
            self._emit_quote(quote_tick(sym, q, at_ms))
        try:
            self._sec_flush(now_et)
        except Exception as e:                                # noqa: BLE001
            self._log(f"[feed] second flush: {e!r}")

    def _emit_quote(self, tick) -> None:
        if tick is None or self.on_quote is None:
            return
        try:
            self.on_quote(tick)
        except Exception as e:                                # noqa: BLE001
            self._log(f"[feed] quote callback failed for {tick.symbol}: {e!r}")

    # ---- second bars from L1 ----------------------------------------------
    # Off unless a caller passes `second_symbols`. Bars are bucketed by the
    # ARRIVAL wall-clock second off the L1 last-price updates; per-trade
    # sizes are not in the L1 field set, so volume is the delta of the
    # cumulative day volume (field 8). L1 conflation means a quiet second
    # can show no update, which is a gap bar the engine handles natively.
    # The arithmetic itself is dqengine.live.secbars, the same function
    # historical second bars are built by.

    def _wanted_seconds(self) -> set:
        want = self._second_symbols
        if want is None:
            return set()
        return {s.upper() for s in (want() if callable(want) else want)}

    def _sec_on_quote(self, sym: str, content: dict, now_et: datetime) -> None:
        if sym not in self._wanted_seconds():
            return
        last = content.get("3")
        if last is None:
            return
        today = now_et.date()
        if self._sec["day"] != today or self._sec["cons"] is None:
            from dqengine.live.secbars import SecondBucketConsolidator
            self._sec["day"] = today
            self._sec["vol"] = {}
            self._sec["cons"] = SecondBucketConsolidator(
                close_ms=self._close_ms(today))
        vol = content.get("8")
        size = 0.0
        if vol is not None:
            prev = self._sec["vol"].get(sym)
            self._sec["vol"][sym] = float(vol)
            if prev is not None and float(vol) > prev:
                size = float(vol) - prev
        self._sec["cons"].add_trade(sym, _sec_now_ms(now_et), float(last), size)

    def _sec_flush(self, now_et: datetime) -> None:
        cons = self._sec["cons"]
        if cons is None or self.on_second_bars is None:
            return
        done = cons.flush_before(_sec_now_ms(now_et))
        if done:
            self.on_second_bars(self._sec["day"], done)

    # ---- the REST half of the same account --------------------------------

    def refresh_bars(self, symbol: str) -> int:
        """Schwab's record of `symbol`'s recent minutes, into `bar_days`;
        returns the number of days it wrote (`BarRefresher` in
        dqengine.feeds.base).

        This is what a worker asks for while this stream is quiet, and it
        asks the same account the socket above streams from: the same
        token callable, the same vendor, the same record of the session.

        `dqengine_schwab.bars` holds the call itself, the parsing and the
        per-symbol lock. It is looked up on the module at call time rather
        than bound here, so a host or a test that replaces it is
        honoured."""
        from . import bars
        return bars.refresh_bar_cache(symbol, token=self._token)

    # ---- health -----------------------------------------------------------

    def health(self, symbols: Iterable[str] = (), now_et=None,
               policy: Optional[SilencePolicy] = None):
        now_et = now_et or datetime.now(ET)
        return check_feed(self.state, symbols or self._symbols, now_et,
                          policy, now=self._clock())


def _sec_now_ms(now_et: datetime) -> int:
    return ((now_et.hour * 3600 + now_et.minute * 60 + now_et.second) * 1000
            + now_et.microsecond // 1000)


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
