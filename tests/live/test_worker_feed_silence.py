"""A worker whose feed has died.

The worker is woken by bar notifications and by nothing else. If the
process that owns the stream loses its socket, no notification arrives,
and the fleet poll does not cover this deployment either -- it skips
worker-owned rows precisely because a worker owns them. The strategy then
stops trading while every heartbeat says the worker is fine.

So the worker reads the feed's own state off the bus and makes its own
verdict. Silent, during the session, means: say so once, and fetch the
deployment's bars over REST on a throttle until the feed comes back.

Nothing here opens a connection or touches a database: the bus, the store,
the bar source and the tick are all fakes.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from dqengine.feeds.base import FeedState
from dqengine.live.driver import engine, ports
from dqengine.live.driver import loop as worker_mod
from dqengine.live.driver.loop import WorkerLoop

ET = ZoneInfo("America/New_York")
SESSION = datetime(2026, 9, 18, 11, 0, tzinfo=ET)       # Friday, mid-session
PRE_OPEN = datetime(2026, 9, 18, 8, 0, tzinfo=ET)
AFTER_CLOSE = datetime(2026, 9, 18, 17, 0, tzinfo=ET)
WEEKEND = datetime(2026, 9, 19, 11, 0, tzinfo=ET)       # Saturday
DEP = "dep-feed-silence"
SYMS = ["AAA", "BBB"]


def alive(symbols=SYMS, frame_age=2.0, bar_age=30.0, now=None) -> str:
    """The feed state a healthy stream publishes, as the json it publishes."""
    now = now if now is not None else SESSION.timestamp()
    return json.dumps(FeedState(connected=True, last_frame_at=now - frame_age,
                                last_bar_at=now - bar_age,
                                symbols=tuple(symbols)).as_status())


def dead(error="read failed: ConnectionClosed") -> str:
    return json.dumps(FeedState(connected=False, error=error).as_status())


class FakeBus:
    def __init__(self):
        self.keys = {}
        self.events = []
        self.acked = []

    def set_ex(self, key, value, ex_s):
        self.keys[key] = value

    def get(self, key):
        return self.keys.get(key)

    def ensure_group(self, stream, group):
        pass

    def read(self, stream, group, consumer, block_ms=0):
        out, self.events = self.events, []
        return out

    def ack(self, stream, group, *ids):
        self.acked.extend(ids)

    def publish(self, stream, fields):
        pass


class FakeBars:
    """The driver's bar source: `refresh` and what it would ask are all
    that is on this path."""

    def __init__(self, wrote=1, fail=(), source="REST"):
        self.calls = []
        self.wrote = wrote
        self.fail = set(fail)
        self.source = source

    def refresh_source(self):
        return self.source

    def refresh(self, symbol):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("the vendor said no")
        return self.wrote


class OlderBars:
    """A bar source from before the question existed: `refresh` and nothing
    to ask it with."""

    def __init__(self, bars):
        self._bars = bars

    def refresh(self, symbol):
        return self._bars.refresh(symbol)


class FakeStore:
    def __init__(self, dep):
        self._dep = dep

    @contextmanager
    def open(self, dep_id):
        yield SimpleNamespace(dep=self._dep, events=[])


class Rig:
    def __init__(self, loop, bus, bars, ticks, capsys):
        self.loop, self.bus, self.bars, self.ticks = loop, bus, bars, ticks
        self._capsys = capsys

    def run(self, at=SESSION):
        return self.loop.run_once(at)

    def lines(self):
        """Every [worker] line printed since the last call."""
        out = self._capsys.readouterr().out
        return [ln for ln in out.splitlines() if ln.startswith("[worker]")]


@pytest.fixture()
def rig(monkeypatch, capsys):
    dep = SimpleNamespace(id=DEP, kind="python", ir=None, code="pass",
                          universe=list(SYMS), resolution="minute",
                          status="running", paused_at=None,
                          cash_initial=1000.0, margin_max=None, position=None,
                          start_date=SESSION.date(), broker_connection_id=None)
    bus, bars = FakeBus(), FakeBars()
    monkeypatch.setattr(ports, "_STORE", FakeStore(dep))
    monkeypatch.setattr(ports, "_BARS", bars)
    monkeypatch.setattr(ports, "_SINK",
                        lambda b: SimpleNamespace(acted=lambda *a: None,
                                                  sync_floor_s=0.0))
    ticks = []
    lp = WorkerLoop(DEP, bus, tick=lambda d, refresh_bars=True:
                    ticks.append((d, refresh_bars)))
    try:
        yield Rig(lp, bus, bars, ticks, capsys)
    finally:
        engine.undefer_exports_for(DEP)


# ---- the feed is feeding us -------------------------------------------------

def test_a_live_feed_costs_a_read_and_nothing_else(rig):
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive()
    assert rig.run() == "idle"
    assert rig.bars.calls == [], "bars arrive pushed; nothing to fetch"
    assert rig.lines() == []


# ---- the feed has died ------------------------------------------------------

def test_a_dead_stream_says_so_once_and_fetches_the_bars_itself(rig):
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "ticked", "the bars the refresh wrote are a tick"
    assert rig.bars.calls == SYMS, "every symbol this deployment can touch"
    assert rig.ticks == [(DEP, False)], \
        "refresh_bars=False: the fallback already wrote them to the store"
    lines = rig.lines()
    assert len(lines) == 1 and "silent" in lines[0] and DEP in lines[0]
    assert "read failed" in lines[0], "the feed's own reason, not a guess"


def test_a_feed_that_is_still_silent_says_nothing_and_waits_its_turn(rig):
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    rig.run()
    rig.lines()
    assert rig.run(SESSION + timedelta(seconds=1)) == "idle"
    assert rig.run(SESSION + timedelta(seconds=59)) == "idle"
    assert rig.bars.calls == SYMS, "one REST pass per window, not per cycle"
    assert rig.lines() == [], "one line per transition, not per cycle"
    assert rig.run(SESSION + timedelta(seconds=61)) == "ticked"
    assert rig.bars.calls == SYMS + SYMS
    assert rig.lines() == [], "still the same silence: still one line"


def test_the_feed_comes_back_and_the_worker_goes_quiet_again(rig):
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    rig.run()
    rig.lines()
    back = SESSION + timedelta(seconds=120)
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive(now=back.timestamp())
    assert rig.run(back) == "idle"
    lines = rig.lines()
    assert len(lines) == 1 and "feed back" in lines[0]
    assert rig.bars.calls == SYMS, "no REST pass while the feed is live"
    assert rig.run(back + timedelta(seconds=61)) == "idle"
    assert rig.lines() == [], "recovery is a transition too"


def test_a_feed_carrying_other_symbols_is_silence_for_this_deployment(rig):
    """Connected, frames flowing, and not subscribed to what this
    deployment trades: nothing will ever arrive for it."""
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive(symbols=["ZZZ"])
    assert rig.run() == "ticked"
    assert rig.bars.calls == SYMS
    assert "not subscribed" in rig.lines()[0]


# ---- out of hours -----------------------------------------------------------

@pytest.mark.parametrize("when", [PRE_OPEN, WEEKEND])
def test_outside_the_session_a_dead_feed_is_not_news(rig, when):
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run(when) == "idle"
    assert rig.bars.calls == [] and rig.lines() == []


def test_a_silence_that_outlives_the_session_announces_itself_again(rig):
    """The line is per transition, and the close is a transition: a feed
    still dead tomorrow morning is news again, not a silence nobody
    mentioned since yesterday."""
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    rig.run()
    assert len(rig.lines()) == 1
    rig.run(AFTER_CLOSE)
    assert rig.lines() == []
    next_day = SESSION + timedelta(days=3)          # the following Monday
    rig.run(next_day)
    assert len(rig.lines()) == 1


# ---- nobody is publishing the state at all ----------------------------------

def test_a_missing_key_is_grace_first_and_silence_after(rig):
    """No key at all: an older api mid-rolling-deploy, or a redis hiccup.
    That is not silence on its own -- but nothing published for longer
    than three of the key's own lifetimes is."""
    assert rig.run() == "idle", "born before the publisher: wait"
    assert rig.bars.calls == [] and rig.lines() == []
    inside = SESSION + timedelta(seconds=worker_mod.FEED_ABSENT_GRACE_S - 1)
    assert rig.run(inside) == "idle"
    assert rig.bars.calls == [] and rig.lines() == []
    past = SESSION + timedelta(seconds=worker_mod.FEED_ABSENT_GRACE_S + 1)
    assert rig.run(past) == "ticked"
    assert rig.bars.calls == SYMS
    assert "no feed state on the bus" in rig.lines()[0]


def test_the_grace_restarts_when_the_key_goes_missing_later(rig):
    """A key that WAS there and disappears gets the same window from the
    moment it disappeared -- not from the worker's birth."""
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive()
    rig.run()
    gone = SESSION + timedelta(seconds=300)
    del rig.bus.keys[worker_mod.FEED_STATE_KEY]
    assert rig.run(gone) == "idle", "the absence starts here"
    assert rig.run(gone + timedelta(
        seconds=worker_mod.FEED_ABSENT_GRACE_S - 1)) == "idle"
    assert rig.bars.calls == []
    assert rig.run(gone + timedelta(
        seconds=worker_mod.FEED_ABSENT_GRACE_S + 1)) == "ticked"
    assert rig.bars.calls == SYMS


def test_a_key_that_comes_back_clears_the_absence_clock(rig):
    """A blip: the key is missed once and is there on the next read. The
    next time it goes missing the window starts again -- otherwise a worker
    that saw one hiccup this morning would call the next one silence the
    moment it happened."""
    assert rig.run() == "idle"                 # no key yet: the clock starts
    back = SESSION + timedelta(seconds=30)
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive(now=back.timestamp())
    assert rig.run(back) == "idle"             # present: the clock is cleared
    gone = back + timedelta(seconds=10)
    del rig.bus.keys[worker_mod.FEED_STATE_KEY]
    later = gone + timedelta(seconds=worker_mod.FEED_ABSENT_GRACE_S - 1)
    assert rig.run(later) == "idle", \
        "the second absence gets its own window, not the first one's"
    assert rig.bars.calls == [] and rig.lines() == []


def test_a_bus_that_cannot_be_read_is_a_feed_that_cannot_be_heard(rig):
    """The state lives on the bus: if reading it raises, the worker is not
    hearing bar notifications either. Same fallback, never an exception
    out of the cycle."""
    def boom(key):
        raise ConnectionError("redis down")
    rig.bus.get = boom
    assert rig.run() == "idle", "still inside the grace window"
    past = SESSION + timedelta(seconds=worker_mod.FEED_ABSENT_GRACE_S + 1)
    assert rig.run(past) == "ticked"
    assert rig.bars.calls == SYMS


# ---- the fallback itself ----------------------------------------------------

def test_a_refresh_that_fetches_nothing_is_not_a_tick(rig, monkeypatch):
    """Silence off-tape (a halted symbol, a quiet minute) writes no bars,
    and a tick on no new bar is work the engine does not owe."""
    rig.bars.wrote = 0
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "idle"
    assert rig.bars.calls == SYMS, "asked anyway; nothing came back"
    assert rig.ticks == []


def test_a_refresh_that_raises_is_logged_and_the_loop_survives(rig):
    """One symbol's vendor call failing must not cost the others, and must
    not cost the cycle."""
    rig.bars.fail = {"AAA"}
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "ticked", "BBB still came back"
    assert rig.bars.calls == SYMS
    assert any("refresh of AAA" in ln for ln in rig.lines())


def test_every_refresh_failing_leaves_the_worker_running(rig):
    rig.bars.fail = set(SYMS)
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "idle"
    assert rig.run(SESSION + timedelta(seconds=61)) == "idle", \
        "and it tries again next window"
    assert rig.bars.calls == SYMS + SYMS


def test_an_install_with_no_rest_fallback_says_so_and_asks_nobody(rig):
    """A bar source whose feed has no REST bars of its own. The silence is
    still news -- the strategy has stopped trading -- but there is nothing
    to ask, and saying that once beats failing once per symbol per window
    for as long as the stream is down."""
    rig.bars.source = None
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "idle"
    assert rig.bars.calls == [], "nothing to ask, so nothing was asked"
    lines = rig.lines()
    assert len(lines) == 1 and "silent" in lines[0]
    assert "no REST fall-back" in lines[0]
    assert rig.run(SESSION + timedelta(seconds=61)) == "idle"
    assert rig.lines() == [], "and it does not repeat itself every window"


def test_the_source_the_bar_source_names_is_the_one_reported(rig):
    rig.bars.source = "SchwabQuoteFeed REST"
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "ticked"
    assert "falling back to SchwabQuoteFeed REST" in rig.lines()[0]
    assert rig.bars.calls == SYMS


def test_a_bar_source_that_does_not_answer_is_taken_to_have_one(
        rig, monkeypatch):
    """Every bar source had a refresh before the question existed, and a
    host's own implementation is entitled not to answer it."""
    monkeypatch.setattr(ports, "_BARS", OlderBars(rig.bars))
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = dead()
    assert rig.run() == "ticked"
    assert rig.bars.calls == SYMS
    assert "falling back to REST" in rig.lines()[0]


def test_the_pushed_path_is_untouched_by_the_watch(rig):
    """A live feed and a bar event: the ordinary cycle, ticked off the
    notification with no REST call anywhere in it."""
    rig.bus.keys[worker_mod.FEED_STATE_KEY] = alive()
    rig.bus.events = [("1-1", {"sym": "AAA", "start_ms": 60000}),
                      ("1-2", {"sym": "BBB", "start_ms": 60000})]
    assert rig.run() == "ticked"
    assert rig.ticks == [(DEP, False)]
    assert rig.bars.calls == []
    assert rig.bus.acked == ["1-1", "1-2"]
