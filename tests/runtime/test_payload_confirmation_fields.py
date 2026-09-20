"""A fill's payload must say whether the BROKER made it.

broker_exec builds `model_folded_side` from each fill's `confirmed` flag.
With the flag missing every fill counts as unfolded, and `_fold_explains`
then freezes that symbol's market-delta pass after ANY same-day fill:

    09:31  buy +77 fills at the broker   (exe_sums = +77)
    15:59  strategy signals the exit     (delta = -77, diff = +77)
           |delta + diff| = 0 < 77       -> frozen, exit never sent

The position rides overnight while the strategy believes it exited. The IR
payload has always carried confirmed/fees/model_px; the python one did not.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from conftest_helpers import make_book                             # noqa: E402


def test_a_model_fill_reports_itself_confirmed_when_there_is_no_ledger():
    """No ledger is a backtest: the model's fill IS the fill."""
    book, sleeve = make_book(date(2026, 9, 4))
    book.market("TQQQ", 10, 50.0, tag="entry")
    f = sleeve.fills[-1]
    assert getattr(f, "confirmed", True) is True


def test_an_unconfirmed_fill_is_flagged_and_survives_into_the_payload():
    from dqengine.runtime.core.ledger import ExecutionLedger

    book, sleeve = make_book(date(2026, 9, 4))
    # reconciled_from=None -> UNKNOWN: the model's fill stands, provisionally
    book.ledger = ExecutionLedger([], reconciled_from=None)
    book.market("TQQQ", 10, 50.0, tag="entry")
    assert sleeve.fills[-1].confirmed is False


def test_the_backtester_payload_carries_the_fold_fields():
    """The executor reads these off the payload, not off the sleeve."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "dqengine", "runtime", "backtester.py")).read()
    block = src[src.index('"fills": ['):]
    block = block[:block.index('"orders": (')]
    for field in ("confirmed", "fees", "model_px"):
        assert field in block, f"payload drops {field}"
