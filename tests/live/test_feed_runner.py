"""The loop around a live feed: the bar_days write, the two published keys.

The wire is a plugin's and is tested there. What is tested here is the
order (store, then announce), the filters (session window, sane bar,
usable price), and the two cadences.
"""
import json
from datetime import date, datetime, timezone

import pytest

from dqengine.feeds import FeedState, MinuteBar
from dqengine.live import feed_runner
from dqengine.live import persistence
from dqengine.live.feed_runner import BarDayStore, FeedRunner

DAY = date(2026, 9, 4)                 # a Friday, full session
MS_1030 = (10 * 3600 + 30 * 60) * 1000


def _bar(sym="TQQQ", ms=MS_1030, px=100.0, vol=500.0, day=DAY):
    return MinuteBar(symbol=sym, day=day, start_ms=ms, open=px, high=px + 1,
                     low=px - 1, close=px, volume=vol)


def _epoch_ms(day, hh, mm, utc_offset=4):
    """Epoch ms of an ET wall clock, given the day's UTC offset in hours."""
    dt = datetime(day.year, day.month, day.day, hh + utc_offset, mm,
                  tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class FakeBus:
    def __init__(self, dead=False):
        self.published = []
        self.sets = []
        self.dead = dead

    def publish(self, stream, fields):
        if self.dead:
            raise ConnectionError("bus down")
        self.published.append((stream, fields))

    def set_ex(self, key, value, ex_s):
        if self.dead:
            raise ConnectionError("bus down")
        self.sets.append((key, json.loads(value), ex_s))


@pytest.fixture()
def bus(monkeypatch):
    from dqengine.live import bus as bus_mod
    fb = FakeBus()
    monkeypatch.setattr(bus_mod, "BUS", fb)
    return fb


# ------------------------------------------------------------ the bar store

def test_a_whole_frame_lands_in_one_row_and_reports_what_changed(pg):
    store = BarDayStore(source="unit-test")
    out = store.write([_bar("AAA"), _bar("BBB", px=20.0)])
    assert {b.symbol for b in out} == {"AAA", "BBB"}
    with pg() as s:
        rec = s.get(persistence.BarDay, ("AAA", DAY))
        assert rec.rows == [[MS_1030, 100.0, 101.0, 99.0, 100.0, 500.0]]
        assert rec.source == "unit-test", "the tag is the feed's, not the store's"


def test_an_identical_resend_is_neither_stored_nor_announced(pg):
    store = BarDayStore()
    assert len(store.write([_bar()])) == 1
    assert store.write([_bar()]) == [], "identical: nothing to say"
    changed = store.write([_bar(px=100.5)])
    assert len(changed) == 1, "a correction IS announced"
    with pg() as s:
        rec = s.get(persistence.BarDay, ("TQQQ", DAY))
        assert [r for r in rec.rows if r[0] == MS_1030][0][1] == 100.5
        assert len(rec.rows) == 1, "one row per minute, replaced in place"
        assert rec.fetched_at is not None, "the export change signal is stamped"


def test_an_empty_frame_touches_nothing(pg):
    assert BarDayStore().write([]) == []


def test_a_second_minute_is_appended_in_order(pg):
    store = BarDayStore()
    store.write([_bar(ms=MS_1030 + 60_000)])
    store.write([_bar(ms=MS_1030)])
    with pg() as s:
        rec = s.get(persistence.BarDay, ("TQQQ", DAY))
    assert [r[0] for r in rec.rows] == [MS_1030, MS_1030 + 60_000]


# ------------------------------------------------------- the single-bar path

def test_store_bar_writes_and_announces_after_the_commit(pg, bus):
    ok = feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 10, 30), 10, 10.1, 9.9,
                               10.05, 500)
    assert ok
    assert bus.published == [("bars", {"sym": "TQQQ", "day": DAY.isoformat(),
                                       "start_ms": MS_1030})]
    with pg() as s:
        assert s.get(persistence.BarDay, ("TQQQ", DAY)) is not None


def test_store_bar_can_defer_the_announcement_to_the_caller(pg, bus):
    assert feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 10, 30), 10, 10.1,
                                 9.9, 10.05, 500, notify=False)
    assert bus.published == [], "a whole frame is stored before any of it is said"


def test_store_bar_refuses_a_bar_outside_the_regular_session(pg, bus):
    assert not feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 8, 30), 10, 10.1,
                                     9.9, 10.05, 500)
    assert not feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 16, 0), 10, 10.1,
                                     9.9, 10.05, 500)
    with pg() as s:
        assert s.get(persistence.BarDay, ("TQQQ", DAY)) is None


def test_store_bar_refuses_a_malformed_candle(pg, bus, capsys):
    """low above high, an open eight times the price: stored, it would mark
    every position off nonsense."""
    assert not feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 10, 30),
                                     539.0, 72.55, 72.65, 72.40, 73)
    assert "dropped malformed TQQQ bar" in capsys.readouterr().out
    with pg() as s:
        assert s.get(persistence.BarDay, ("TQQQ", DAY)) is None


def test_a_dead_bus_never_blocks_the_store_write(pg, monkeypatch, capsys):
    from dqengine.live import bus as bus_mod
    monkeypatch.setattr(bus_mod, "BUS", FakeBus(dead=True))
    assert feed_runner.store_bar("TQQQ", _epoch_ms(DAY, 10, 30), 10, 10.1,
                                 9.9, 10.05, 500)
    assert "bus publish failed for TQQQ" in capsys.readouterr().out
    with pg() as s:
        assert s.get(persistence.BarDay, ("TQQQ", DAY)) is not None


def test_no_bus_is_not_an_error(monkeypatch):
    from dqengine.live import bus as bus_mod
    monkeypatch.setattr(bus_mod, "BUS", None)
    feed_runner.notify_bar("TQQQ", DAY, MS_1030)       # no raise


# ------------------------------------------------------------- the two keys

class FakeFeed:
    def __init__(self, state=None):
        self.quotes = {}
        self.state = state or FeedState()
        self.polls = []
        self.raise_on_poll = None

    def poll(self, timeout=1.0):
        self.polls.append(timeout)
        if self.raise_on_poll is not None:
            raise self.raise_on_poll


def test_the_snapshot_carries_the_last_trade_and_when_it_printed(bus):
    at = datetime(2026, 9, 16, 19, 59, 0, 250_000, tzinfo=timezone.utc)
    last_at = datetime(2026, 9, 16, 19, 40, 0, tzinfo=timezone.utc)
    board = {"REW": {"last": 11.85, "bid": 10.27, "at": at.isoformat(),
                     "last_at": last_at.isoformat()},
             "NOPRINT": {"bid": 1.0, "at": at.isoformat()}}
    runner = FeedRunner(FakeFeed(), quotes=board)
    runner.publish_quote_snapshot()
    key, snap, ttl = bus.sets[0]
    assert key == "quotes:last" and ttl == 30
    assert snap == {"REW": {"last": 11.85,
                            "at_ms": int(last_at.timestamp() * 1000)}}, \
        "a symbol with no last trade is left out, not published as None; " \
        "at_ms is the LAST TRADE's time, not the frame's"


def test_a_non_numeric_last_costs_one_symbol_not_the_stream(bus):
    at = datetime(2026, 9, 16, 19, 59, tzinfo=timezone.utc).isoformat()
    board = {"BAD": {"last": "n/a", "at": at},
             "NAN": {"last": float("nan"), "at": at},
             "ZERO": {"last": 0.0, "at": at},
             "NEG": {"last": -1.0, "at": at},
             "REW": {"last": 11.85, "at": at}}
    FeedRunner(FakeFeed(), quotes=board).publish_quote_snapshot()
    assert list(bus.sets[0][1]) == ["REW"]


def test_an_empty_board_publishes_nothing(bus):
    FeedRunner(FakeFeed(), quotes={}).publish_quote_snapshot()
    assert bus.sets == []


def test_the_snapshot_is_throttled_to_100ms(bus, monkeypatch):
    board = {"REW": {"last": 1.0}}
    runner = FeedRunner(FakeFeed(), quotes=board)
    clock = {"t": 1000.0}
    monkeypatch.setattr(feed_runner.time, "time", lambda: clock["t"])
    runner.publish_quote_snapshot()
    clock["t"] += 0.05
    runner.publish_quote_snapshot()
    assert len(bus.sets) == 1, "50 ms later: collapsed"
    clock["t"] += 0.06
    runner.publish_quote_snapshot()
    assert len(bus.sets) == 2, "110 ms after the first: published"
    assert feed_runner.QSNAP_MIN_S == 0.1


def test_the_feed_state_is_published_in_the_health_blocks_shape(bus):
    """A process that cannot see this one reads `feed:state` to find out the
    stream died -- the same shape a health block serves, so one shape covers
    both readers."""
    feed = FakeFeed(FeedState(connected=True, last_frame_at=1787671800.0,
                              symbols=("TQQQ",)))
    runner = FeedRunner(feed)
    runner.refresh_status()
    runner.publish_feed_state()
    key, payload, ttl = bus.sets[0]
    assert key == "feed:state" and ttl == 30
    assert set(payload) == {"connected", "last_frame_at", "last_bar_at",
                            "error", "symbols"}
    assert payload["connected"] is True and payload["symbols"] == ["TQQQ"]
    assert FeedState.from_status(payload).last_frame_at == 1787671800.0, \
        "the reader reads back the epoch it does its arithmetic on"


def test_the_feed_state_is_published_on_its_own_cadence(bus, monkeypatch):
    """Once a second, TTL 30: the key must be refreshed well inside its own
    expiry, or a healthy stream reads as an absent feed."""
    runner = FeedRunner(FakeFeed())
    clock = {"t": 1000.0}
    monkeypatch.setattr(feed_runner.time, "time", lambda: clock["t"])
    runner.publish_feed_state()
    clock["t"] += 0.5
    runner.publish_feed_state()
    assert len(bus.sets) == 1, "half a second later: collapsed"
    clock["t"] += 0.6
    runner.publish_feed_state()
    assert len(bus.sets) == 2
    assert feed_runner.FSTATE_MIN_S < feed_runner.FEED_STATE_TTL_S


def test_the_state_is_published_whether_the_socket_is_up_or_down(bus):
    """A dead socket sends no quote frames, so publishing on the quote
    cadence would only let the key expire. It says connected: false within
    a second instead."""
    feed = FakeFeed(FeedState(connected=False, error="socket closed"))
    runner = FeedRunner(feed)
    runner.poll_once()
    assert bus.sets[0][1]["connected"] is False
    assert bus.sets[0][1]["error"] == "socket closed"


def test_a_bus_handed_in_wins_over_the_process_bus(monkeypatch):
    from dqengine.live import bus as bus_mod
    monkeypatch.setattr(bus_mod, "BUS", FakeBus())
    mine = FakeBus()
    FeedRunner(FakeFeed(), quotes={"X": {"last": 2.0}},
               bus=mine).publish_quote_snapshot()
    assert len(mine.sets) == 1 and bus_mod.BUS.sets == []


def test_no_bus_publishes_nothing_and_raises_nothing(monkeypatch):
    from dqengine.live import bus as bus_mod
    monkeypatch.setattr(bus_mod, "BUS", None)
    runner = FeedRunner(FakeFeed(), quotes={"X": {"last": 2.0}})
    runner.publish_quote_snapshot()
    runner.publish_feed_state()


# ------------------------------------------------------------------ the pump

def test_the_snapshot_goes_out_once_per_frame_that_carried_a_quote(bus):
    runner = FeedRunner(FakeFeed(), quotes={"X": {"last": 2.0}})
    runner.poll_once()
    assert [k for k, _, _ in bus.sets] == ["feed:state"], \
        "no quote in that frame: no snapshot"
    runner.on_quote(object())
    runner.poll_once()
    assert "quotes:last" in [k for k, _, _ in bus.sets]
    bus.sets.clear()
    runner.poll_once()
    assert [k for k, _, _ in bus.sets] == [], \
        "the flag is cleared: a quiet frame republishes nothing"


def test_on_bar_announces_the_stored_bar(bus):
    FeedRunner(FakeFeed()).on_bar(_bar())
    assert bus.published == [("bars", {"sym": "TQQQ", "day": DAY.isoformat(),
                                       "start_ms": MS_1030})]


def test_a_poll_that_raises_is_logged_and_the_loop_survives(bus, monkeypatch,
                                                            capsys):
    monkeypatch.setattr(feed_runner.time, "sleep", lambda s: None)
    feed = FakeFeed()
    feed.raise_on_poll = RuntimeError("socket exploded")
    runner = FeedRunner(feed)
    runner.poll_once()
    assert "poll failed" in capsys.readouterr().out
    assert bus.sets, "the state still goes out: a dead stream must be visible"


def test_run_rescans_on_the_first_pass_and_then_on_its_cadence(bus,
                                                               monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(feed_runner.time, "time", lambda: clock["t"])
    scans = []

    class Stop(BaseException):
        pass

    runner = FeedRunner(FakeFeed())
    passes = {"n": 0}

    def poll_once(timeout=1.0):
        passes["n"] += 1
        if passes["n"] >= 4:
            raise Stop
        clock["t"] += 10.0          # 10 s per pass

    monkeypatch.setattr(runner, "poll_once", poll_once)
    with pytest.raises(Stop):
        runner.run(rescan=lambda: scans.append(clock["t"]), rescan_s=15.0)
    assert scans == [1000.0, 1020.0], \
        "scanned before the first poll, then once past rescan_s -- never" \
        " once per pass"


def test_run_without_a_rescan_just_pumps(bus, monkeypatch):
    class Stop(BaseException):
        pass

    runner = FeedRunner(FakeFeed())
    passes = {"n": 0}

    def poll_once(timeout=1.0):
        passes["n"] += 1
        if passes["n"] >= 3:
            raise Stop

    monkeypatch.setattr(runner, "poll_once", poll_once)
    with pytest.raises(Stop):
        runner.run()
    assert passes["n"] == 3


def test_the_runner_and_the_feed_share_one_board(bus):
    board = {}
    feed = FakeFeed()
    runner = FeedRunner(feed, quotes=board)
    assert feed.quotes is board and runner.quotes is board, \
        "the feed folds its frames into the dict the snapshot reads"
    assert FeedRunner(feed).quotes is feed.quotes, \
        "no board handed in: the feed's own"
