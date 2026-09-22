"""A scripted feed and an in-memory bus, for running a whole session in a
test with no network and no container.

`FakeQuoteFeed` is registered through the feed loader's own table, the way a
plugin's entry point is, so the code under test resolves it by name exactly
as it resolves the bundled Alpaca feed. `FakeBus` is everything the driver,
the consumers and the feed runner ask of Redis: streams with groups, and a
few keys with a TTL nobody here needs to expire.
"""
import threading
import time
from datetime import date

from dqengine.feeds.base import FeedState, MinuteBar


class FakeQuoteFeed:
    """Plays `SCRIPT` -- a list of frames, each a list of MinuteBar -- one
    frame per `poll()`, then idles. Set it on the class before the loader
    builds one; the loader resolves classes, not instances."""

    SCRIPT: list = []

    def __init__(self, store=None, on_bar=None, on_quote=None, symbols=(),
                 **_cfg):
        self.store = store
        self.on_bar = on_bar
        self.on_quote = on_quote
        self.state = FeedState(connected=True)
        self.quotes: dict = {}
        self.script = [list(frame) for frame in self.SCRIPT]
        self.polls = 0
        self.closed = False
        self._symbols = {s.upper() for s in symbols}
        self.state.symbols = tuple(sorted(self._symbols))

    @property
    def symbols(self) -> list:
        return sorted(self._symbols)

    def subscribe(self, symbols) -> None:
        self._symbols |= {s.upper() for s in symbols}
        self.state.symbols = tuple(sorted(self._symbols))

    def unsubscribe(self, symbols) -> None:
        self._symbols -= {s.upper() for s in symbols}
        self.state.symbols = tuple(sorted(self._symbols))

    def poll(self, timeout: float = 1.0) -> None:
        self.polls += 1
        if not self.script:
            time.sleep(min(timeout, 0.01))
            return
        frame = self.script.pop(0)
        self.state.last_frame_at = time.time()
        changed = self.store.write(frame) if self.store is not None else frame
        for bar in changed:
            self.state.last_bar_at = time.time()
            if self.on_bar is not None:
                self.on_bar(bar)

    def health(self):
        return self.state

    def close(self) -> None:
        self.closed = True
        self.state.connected = False


def frame(day: date, symbol: str, start_ms: int, price: float,
          volume: float = 1000.0) -> MinuteBar:
    return MinuteBar(symbol=symbol.upper(), day=day, start_ms=start_ms,
                     open=price, high=price, low=price, close=price,
                     volume=volume)


class FakeBus:
    """Redis streams and keys, in a dict.

    `read` returns whatever has arrived since this group last read, and
    sleeps a little when there is nothing rather than spinning: the
    consumers call it in a loop, and a busy wait in a test process starves
    the threads the test is actually waiting on."""

    def __init__(self, idle_s: float = 0.01):
        self.lock = threading.Lock()
        self.streams: dict = {}
        self.cursors: dict = {}
        self.keys: dict = {}
        self.acked: list = []
        self.idle_s = idle_s
        self._seq = 0

    # ---- streams ----
    def publish(self, stream: str, fields: dict) -> str:
        with self.lock:
            self._seq += 1
            eid = f"{self._seq}-0"
            self.streams.setdefault(stream, []).append(
                (eid, {k: str(v) for k, v in fields.items()}))
            return eid

    def ensure_group(self, stream: str, group: str) -> None:
        with self.lock:
            self.streams.setdefault(stream, [])
            self.cursors.setdefault((stream, group), 0)

    def read(self, stream: str, group: str, consumer: str,
             block_ms: int = 1000, count: int = 100) -> list:
        with self.lock:
            entries = self.streams.setdefault(stream, [])
            at = self.cursors.setdefault((stream, group), 0)
            out = entries[at:at + count]
            self.cursors[(stream, group)] = at + len(out)
        if not out:
            time.sleep(min(self.idle_s, max(block_ms, 0) / 1000.0))
        return out

    def ack(self, stream: str, group: str, *ids) -> None:
        with self.lock:
            self.acked.append((stream, group, list(ids)))

    # ---- keys ----
    def set_ex(self, key: str, value: str, ex_s: int) -> None:
        with self.lock:
            self.keys[key] = value

    def get(self, key: str):
        with self.lock:
            return self.keys.get(key)

    def delete(self, key: str) -> None:
        with self.lock:
            self.keys.pop(key, None)


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.05):
    """Poll `predicate` until it is truthy. Returns its value, or None when
    the deadline passes -- the caller's assertion says what was missing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(interval)
    return None
