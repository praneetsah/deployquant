import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ENGINE = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ENGINE)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A contributor with Alpaca keys exported must get the same run as CI."""
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_API_DATA_FEED"):
        monkeypatch.delenv(name, raising=False)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def et(year, month, day, hour=0, minute=0, second=0, micro=0):
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=ET)


def wire_time(dt_et):
    """An ET wall-clock time as the RFC3339 UTC string the stream sends."""
    return dt_et.astimezone(UTC).isoformat().replace("+00:00", "Z")


def bar_msg(symbol, dt_et, o=10.0, h=11.0, l=9.5, c=10.5, v=1000, kind="b"):
    return {"T": kind, "S": symbol, "o": o, "h": h, "l": l, "c": c, "v": v,
            "t": wire_time(dt_et), "n": 12, "vw": c}


def trade_msg(symbol, dt_et, price=10.5, size=100):
    return {"T": "t", "S": symbol, "i": 1, "x": "V", "p": price, "s": size,
            "t": wire_time(dt_et), "c": ["@"], "z": "C"}


def quote_msg(symbol, dt_et, bid=10.4, ask=10.6):
    return {"T": "q", "S": symbol, "bx": "V", "bp": bid, "bs": 1,
            "ax": "V", "ap": ask, "as": 1, "t": wire_time(dt_et),
            "c": ["R"], "z": "C"}


def sub_msg(symbols):
    keys = sorted(s.upper() for s in symbols)
    return {"T": "subscription", "bars": keys, "updatedBars": keys,
            "trades": keys, "quotes": keys}


HANDSHAKE = [json.dumps([{"T": "success", "msg": "connected"}]),
             json.dumps([{"T": "success", "msg": "authenticated"}])]


class FakeSocket:
    """A scripted websocket. Items are returned from `recv` in order: a
    str is the frame, an exception instance is raised, and an exhausted
    script is a read timeout. Nothing here opens a connection."""

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
    """Hands out the scripted sockets in order and records the urls."""

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
        self.frames = []
        self._changed = changed

    def write(self, bars):
        self.frames.append(list(bars))
        self.events.append(("store", [b.symbol for b in bars]))
        return list(bars) if self._changed is None else self._changed(bars)
