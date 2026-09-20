"""Sell market orders and the at-close action (spec §3 Phase 2 item 3).

Two IR facts drive every assertion here:

  * `_sized_qty` is SIDE-AGNOSTIC. It returns a positive share count for
    buys and sells alike, and only pct_equity consults `ref`; `dollars`
    divides by the live price, `shares` and `pct_position` touch no price.
  * A market sell that flattens does NOT release once_per=position guards.
    Only `_market_close_position` (liquidate), a managed-target fill and a
    rebalance call `_on_flat`. Replicating that exactly is the difference
    between a rule that re-enters tomorrow and one that never fires again.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA, assert_golden                          # noqa: E402
from dqengine import codegen                                               # noqa: E402

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

WINDOW = ("2024-03-01", "2024-06-28")


def _base(sell_action, extra_when=None):
    return {
        "ir_version": "0.2", "meta": {"name": "Sell Shapes"},
        "params": {"HALF": 0.5},
        "universe": {"static": ["SPY"]},
        "rules": [
            {"id": "enter",
             "trigger": {"type": "session_open", "days": "first_of_week"},
             "when": {"not": {"pos": "invested"}},
             "guard": {"once_per": "position"},
             "action": {"type": "market_order", "side": "buy",
                        "size": {"pct_equity": 0.9}}},
            {"id": "trim",
             "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
             "when": extra_when or {"gt": [{"pos": "qty"}, 0]},
             "action": sell_action},
        ],
    }


SELLS = {
    "sell_pct_equity": {"type": "market_order", "side": "sell",
                        "size": {"pct_equity": 0.25, "ref": "last"}},
    "sell_dollars": {"type": "market_order", "side": "sell",
                     "size": {"dollars": 1500}},
    "sell_shares": {"type": "market_order", "side": "sell",
                    "size": {"shares": 3}},
    "sell_pct_position": {"type": "market_order", "side": "sell",
                          "size": {"pct_position": {"param": "HALF"}}},
    "sell_ref_prior_close": {"type": "market_order", "side": "sell",
                             "size": {"pct_equity": 0.2,
                                      "ref": "prior_close"}},
    "sell_ref_session_open": {"type": "market_order", "side": "sell",
                              "size": {"pct_equity": 0.2,
                                       "ref": "session_open"}},
}


@needs_data
@pytest.mark.parametrize("name", sorted(SELLS))
def test_a_sell_market_order_matches_its_golden(name):
    got = assert_golden(_base(SELLS[name]), start=WINDOW[0], end=WINDOW[1],
                        name=name)
    # Coverage, not just parity: the window must actually have produced a
    # sell of THIS size kind, or a refusal-turned-noop on both engines
    # would pass trivially without ever exercising the shape.
    assert any(f["qty"] < 0 for f in got["fills"]), \
        f"{name}: window too quiet, the sell shape never fired"


@needs_data
def test_a_market_sell_that_flattens_does_not_release_a_position_guard():
    """Decision 4, in fills: the entry is once_per=position, the exit is a
    plain market sell for the whole position. The IR engine never releases
    the guard, so there is exactly ONE entry over the window. A generator
    that reset on every flattening fill would re-enter every week."""
    ir = _base({"type": "market_order", "side": "sell",
                "size": {"pct_position": 1.0}})
    got = assert_golden(ir, start=WINDOW[0], end=WINDOW[1],
                        name="sell_flat_keeps_guard")
    buys = [f for f in got["fills"] if f["qty"] > 0]
    assert len(buys) == 1, buys


@needs_data
def test_a_liquidate_does_release_the_position_guard():
    """The mirror image: same IR, but the exit is `liquidate`, which DOES
    call _on_flat. The entry must fire again the next first_of_week."""
    ir = _base({"type": "liquidate"})
    got = assert_golden(ir, start=WINDOW[0], end=WINDOW[1],
                        name="liquidate_releases_guard")
    buys = [f for f in got["fills"] if f["qty"] > 0]
    assert len(buys) > 1, buys


@needs_data
def test_a_guarded_liquidate_fires_again_every_time_the_position_returns():
    """The guard on the EXIT — the case above with the guard moved from
    the entry to the liquidate, and the one shape neither covered.

    `_liquidate_all` calls `_on_flat`, which POPS this rule's own
    once_per=position flag. The IR engine consumes the guard BEFORE it
    closes (engine.py's liquidate branch), so the bar ends with the flag
    CLEAR and the rule liquidates again the next time it is invested.
    Consuming after the action inverts that: the flag lands on a guard the
    exit had just released and the rule sells once and never again —
    exactly the money-path failure the spec names, a flat-sleeve
    instruction that silently stops being emitted. Generated code that
    gets this backwards produced 3 fills here against the oracle's 34:
    one buy, one sell, and a position held for the last sixteen weeks.
    """
    ir = _base({"type": "liquidate"})
    ir["rules"][1]["guard"] = {"once_per": "position"}
    got = assert_golden(ir, start=WINDOW[0], end=WINDOW[1],
                        name="liquidate_guarded_exit")
    buys = [f for f in got["fills"] if f["qty"] > 0]
    sells = [f for f in got["fills"] if f["qty"] < 0]
    # every entry is closed again, weekly, for the whole window — not one
    # exit and then a sleeve that never sells
    assert len(sells) == len(buys) > 10, (len(buys), len(sells))


def _at_close(action, trigger=None):
    ir = _base({"type": "liquidate"})
    ir["rules"][1] = {
        "id": "moc",
        "trigger": trigger or {"type": "before_close", "minutes": 1,
                               "days": "all"},
        "when": {"gt": [{"pos": "qty"}, 0]},
        "action": action,
    }
    return ir


@needs_data
def test_a_market_on_close_sell_matches_its_golden():
    got = assert_golden(_at_close({"type": "at_close_order", "side": "sell",
                                   "qty": "all"}),
                        start=WINDOW[0], end=WINDOW[1], name="atclose_moc_all")
    assert any(f["qty"] < 0 for f in got["fills"]), \
        "window too quiet: the MOC sell never fired"


@needs_data
def test_a_marketable_limit_on_close_matches_its_golden():
    """LOC with a limit the close clears: it fills. `price` well below the
    market for a SELL is marketable (close >= limit)."""
    got = assert_golden(_at_close({"type": "at_close_order", "side": "sell",
                                   "qty": "all",
                                   "price": {"mul": [{"ind": "price"}, 0.9]}}),
                        start=WINDOW[0], end=WINDOW[1], name="atclose_loc_fills")
    assert any(f["qty"] < 0 for f in got["fills"]), \
        "window too quiet: the marketable LOC sell never fired"


@needs_data
def test_an_unmarketable_limit_on_close_expires_on_both_engines():
    """A SELL LOC 10% ABOVE the close is not marketable: the IR engine
    records an `expired` order and no fill, and consumes the guard anyway.
    The generated code must do the same — an LOC that quietly became a
    market sell is a position gone at the wrong price."""
    got = assert_golden(_at_close({"type": "at_close_order", "side": "sell",
                                   "qty": "all",
                                   "price": {"mul": [{"ind": "price"}, 1.1]}}),
                        start=WINDOW[0], end=WINDOW[1],
                        name="atclose_loc_expires")
    assert not [f for f in got["fills"] if f["qty"] < 0], "nothing may sell"


@needs_data
def test_an_at_close_triggered_order_matches_its_golden():
    """Under the `at_close` trigger the IR engine fires the rule in the
    BEFORE-CLOSE batch (not in the morning) while pinning its pnl basis to
    the session open. Task 2 compiles that as pnl_pct(use_open=True)."""
    ir = _at_close({"type": "at_close_order", "side": "sell", "qty": "all"},
                   trigger={"type": "at_close", "assess": "session_open",
                            "days": "all"})
    ir["rules"][1]["when"] = {"gt": [{"pos": "pnl_pct"}, 0.0]}
    got = assert_golden(ir, start=WINDOW[0], end=WINDOW[1],
                        name="atclose_trigger")
    assert any(f["qty"] < 0 for f in got["fills"]), \
        "window too quiet: the at_close-triggered sell never fired"


def test_a_partial_quantity_at_close_order_is_not_silently_all():
    """at_close_order DOES honour qty (unlike managed_target — the IR
    engine evaluates `action["qty"]` for this action). Assert the
    generated code sizes from the expression, not from the position."""
    code = codegen.generate_python(
        _at_close({"type": "at_close_order", "side": "sell", "qty": 5}))
    i = code.index("def _rule_moc")
    assert "qty = int(round(5))" in code[i:i + 600]
