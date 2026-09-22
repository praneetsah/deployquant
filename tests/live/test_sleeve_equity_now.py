"""A deposit the replay hasn't reached yet — dated after the last replayed
session, or made before the sleeve's first session ever runs — is real money
already sitting in the account for this sleeve. Valuing the sleeve without it
shows the top-up as "not managed by a strategy" on the brokerage/portfolio
pages, and the add-cash dialog then offers the same dollars as free room a
second time: a top-up on a sleeve whose first session has not run."""
from datetime import date
from types import SimpleNamespace

from dqengine.live.driver import deployment as live


def dep(amount, day):
    return SimpleNamespace(kind="deposit", amount=amount,
                           effective_date=day)


def test_no_sessions_yet_is_worth_its_starting_cash():
    assert live.sleeve_equity_now([], [], 1000.0, []) == 1000.0


def test_top_up_before_the_first_session_counts_immediately():
    """The reported bug: cash added to a sleeve whose start_date hasn't
    traded yet vanished into "not managed"."""
    ev = [dep(100.0, date(2026, 8, 20))]
    assert live.sleeve_equity_now([], [], 1000.0, ev) == 1100.0


def test_out_of_hours_top_up_on_a_running_sleeve_counts_immediately():
    days = [date(2026, 8, 18), date(2026, 8, 19)]
    ev = [dep(100.0, date(2026, 8, 20))]
    assert live.sleeve_equity_now([1000.0, 1050.0], days, 1000.0, ev) == 1150.0


def test_deposit_already_inside_the_curve_is_not_double_counted():
    days = [date(2026, 8, 18), date(2026, 8, 19)]
    ev = [dep(100.0, date(2026, 8, 19))]
    assert live.sleeve_equity_now([1000.0, 1150.0], days, 1000.0, ev) == 1150.0


def test_only_deposits_count():
    ev = [SimpleNamespace(kind="note", amount=999.0,
                          effective_date=date(2026, 8, 21))]
    assert live.sleeve_equity_now([], [], 1000.0, ev) == 1000.0
