"""Minute bars are what a position is valued at, so a malformed one is a
wrong number on a real account.

The case this check was written for, TQQQ 2026-08-18 15:59 off a live
stream:

    15:58  o 72.44  h 72.55  l 72.42  c 72.54  v 192,759
    15:59  o 539.0  h 72.55  l 72.65  c 72.40  v 73        <- stored anyway

Low above high, an open eight times the price, 73 shares in the closing
minute. The store upserts by timestamp, so it replaced a good bar, and
being the last of the session it became the mark for every holding of
that symbol — reporting the position 13c/share below the real close.
"""
from dqengine.feeds import bar_is_sane


def test_the_observed_bad_bar_is_rejected():
    assert bar_is_sane(539.0, 72.55, 72.65, 72.40) is False


def test_its_healthy_neighbour_is_kept():
    assert bar_is_sane(72.44, 72.55, 72.42, 72.54) is True


def test_low_above_high_is_impossible():
    assert bar_is_sane(72.5, 72.4, 72.6, 72.5) is False


def test_open_or_close_outside_the_range_is_impossible():
    assert bar_is_sane(80.0, 72.55, 72.40, 72.50) is False
    assert bar_is_sane(72.5, 72.55, 72.40, 80.0) is False


def test_flat_bar_is_fine():
    """A minute that traded at one price is legitimate, not degenerate."""
    assert bar_is_sane(72.5, 72.5, 72.5, 72.5) is True


def test_nonsense_values_are_rejected():
    for bad in (("x", 1, 1, 1), (None, 1, 1, 1), (0, 0, 0, 0),
                (-1, -1, -1, -1), (float("nan"), 1, 1, 1),
                (float("inf"), 1, 1, 1)):
        assert bar_is_sane(*bad) is False, bad
