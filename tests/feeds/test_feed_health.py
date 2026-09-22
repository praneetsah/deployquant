"""The silence check as a truth table.

Pure function, so every row is stated rather than simulated: session or
not, connected or not, subscribed or not, how long since the last frame
and the last bar.
"""
import pytest
from conftest import et

from dqengine.feeds.base import FeedState
from dqengine.feeds.health import (SilencePolicy, check_feed, market_is_open,
                                   session_open_epoch)

SESSION = et(2026, 9, 18, 11, 0)             # Friday, mid-session
HALF_DAY = et(2026, 11, 27, 11, 0)           # the day after Thanksgiving


def state(connected=True, symbols=("TQQQ",), frame_age=5.0, bar_age=5.0,
          now=None, error=None):
    now = now if now is not None else SESSION.timestamp()
    return FeedState(connected=connected,
                     last_frame_at=None if frame_age is None else now - frame_age,
                     last_bar_at=None if bar_age is None else now - bar_age,
                     error=error, symbols=symbols)


def check(now_et=SESSION, **kw):
    policy = kw.pop("policy", None)
    return check_feed(state(now=now_et.timestamp(), **kw), ["TQQQ"], now_et,
                      policy, now=now_et.timestamp())


# ---- when there is nothing to be silent about -------------------------------

@pytest.mark.parametrize("when", [
    et(2026, 9, 19, 11, 0),                  # Saturday
    et(2026, 9, 20, 11, 0),                  # Sunday
    et(2026, 1, 1, 11, 0),                   # New Year's Day
    et(2026, 9, 18, 9, 0),                   # pre-open
    et(2026, 9, 18, 16, 30),                 # after the close
    et(2026, 11, 27, 13, 30),                # after a 13:00 half-day close
])
def test_outside_the_regular_session_a_quiet_feed_is_not_silent(when):
    assert market_is_open(when) is False
    verdict = check(now_et=when, connected=False, frame_age=None, bar_age=None)
    assert verdict.ok and verdict.reason == "market closed"


def test_the_session_boundaries_are_the_engines_own():
    assert market_is_open(et(2026, 9, 18, 9, 30)) is True
    assert market_is_open(et(2026, 9, 18, 16, 0)) is True
    assert market_is_open(et(2026, 9, 18, 9, 29)) is False
    # a half-day ends at 13:00 here exactly as it does for the bars
    assert market_is_open(et(2026, 11, 27, 12, 59)) is True
    assert market_is_open(et(2026, 11, 27, 13, 1)) is False


# ---- inside the session -----------------------------------------------------

def test_a_feed_delivering_frames_and_bars_is_ok():
    verdict = check()
    assert verdict.ok and verdict.reason == "receiving"
    assert verdict.silent_for_s == 5.0


def test_a_silent_socket_during_the_session_is_silent():
    verdict = check(frame_age=120.0)
    assert verdict.silent and verdict.reason == "no frame during the regular session"
    assert verdict.silent_for_s == 120.0
    assert verdict.log_line() == ("[feed] silent: no frame during the regular "
                                  "session (120s)")


def test_frames_without_bars_are_silent_on_their_own_threshold():
    verdict = check(frame_age=5.0, bar_age=400.0)
    assert verdict.silent and "no bars" in verdict.reason
    assert verdict.silent_for_s == 400.0


def test_a_disconnected_feed_is_silent_and_says_why():
    verdict = check(connected=False, error="connect failed: 406")
    assert verdict.silent and "406" in verdict.reason


def test_a_half_subscribed_feed_is_silent_for_the_symbols_it_does_not_carry():
    verdict = check_feed(state(symbols=("TQQQ",)), ["TQQQ", "QQQ", "spy"],
                         SESSION, now=SESSION.timestamp())
    assert verdict.silent and verdict.missing == ("QQQ", "SPY")
    assert "not subscribed: QQQ, SPY" in verdict.reason


def test_asking_about_no_symbols_only_asks_about_the_socket():
    assert check_feed(state(symbols=()), [], SESSION,
                      now=SESSION.timestamp()).ok


def test_a_feed_that_never_delivered_is_measured_from_the_open():
    """Otherwise a stream that fails to connect at 09:30 reads as
    'no news yet' for the whole session."""
    assert check(now_et=et(2026, 9, 18, 9, 30, 30),
                 frame_age=None, bar_age=None).ok
    verdict = check(now_et=et(2026, 9, 18, 9, 40), frame_age=None, bar_age=None)
    assert verdict.silent and verdict.silent_for_s == pytest.approx(600.0)
    assert verdict.reason == "no frame during the regular session"


def test_the_half_day_session_is_checked_like_any_other():
    assert check(now_et=HALF_DAY).ok
    assert check(now_et=HALF_DAY, frame_age=120.0).silent


def test_the_threshold_is_configurable():
    tight = SilencePolicy(frame_timeout_s=10.0, bar_timeout_s=10.0)
    assert check(frame_age=20.0, bar_age=20.0, policy=tight).silent
    assert check(frame_age=20.0, bar_age=20.0).ok        # the 90 s default
    loose = SilencePolicy(frame_timeout_s=600.0, bar_timeout_s=600.0)
    assert check(frame_age=120.0, bar_age=120.0, policy=loose).ok


def test_session_open_epoch_is_todays_open_on_the_exchange_clock():
    assert session_open_epoch(SESSION) == et(2026, 9, 18, 9, 30).timestamp()


# ---- carrying the state between processes -----------------------------------

def test_a_state_survives_the_round_trip_through_the_status_json():
    """A feed in one process, the check in another (a worker reads the
    state off the bus): the epoch times the arithmetic needs must come
    back as the epoch times that went in."""
    before = state(frame_age=12.0, bar_age=40.0, error="dropped")
    after = FeedState.from_status(before.as_status())
    assert after == before
    assert check_feed(after, ["TQQQ"], SESSION,
                      now=SESSION.timestamp()).ok


def test_an_unreadable_status_reads_as_nothing_heard():
    """Whatever arrives, the check must still answer. A field it cannot
    parse is 'nothing heard', which during the session is silence -- never
    an exception in the middle of a worker's cycle."""
    junk = FeedState.from_status({"connected": True, "last_frame_at": "soon",
                                  "last_bar_at": None, "symbols": None})
    assert junk.last_frame_at is None and junk.symbols == ()
    assert FeedState.from_status({}) == FeedState()
    assert FeedState.from_status(None) == FeedState()
    assert check_feed(junk, ["TQQQ"], SESSION, now=SESSION.timestamp()).silent
