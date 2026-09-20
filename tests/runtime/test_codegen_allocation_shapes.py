"""set_weights as an ORDINARY rule action (spec §3 Phase 2 item 5).

Everything a rule can carry, an allocation rule can carry: a day
selector, a `when`, a once_per guard, any trigger. And a strategy may
hold several of them alongside per-symbol rules. The IR engine has always
done this — `_fire_rule_inner` dispatches set_weights like any other
action — it is only the generator that treated allocation as a
whole-strategy mode.

Every parity case below also asserts that its shape actually FIRED, and
where it fires: a case whose gate never opens matches its golden
trivially and proves nothing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA, assert_golden                          # noqa: E402
from dqengine import codegen                                               # noqa: E402

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

SYMS = ["SPY", "QQQ", "IWM", "TLT"]
WINDOW = ("2024-03-01", "2024-06-28")
BEST = {"best": {"of": [{"asset": s} for s in SYMS],
                 "by": {"metric": "cum_return", "window": 60},
                 "order": "top", "n": 1}}
# A SUBSET of the universe on purpose: the close rule's `best` can hold any
# of the four, so this morning rebalance has to sell whatever is not SPY or
# TLT — a target vector that omits a held symbol is an instruction to sell
# it, on both engines.
#
# Two assets, and specifically two whose prices differ ~5.5x, because a
# rebalance submits same-delta symbols in `set` iteration order — see
# test_the_rebalance_delta_tie_break_is_still_hash_ordered. Equal-weighting
# all four at $10k puts SPY and QQQ on the same integer delta, and the fill
# ORDER then depends on the process's hash seed. SPY and TLT can never
# land on the same share count.
EQUAL = {"equal": [{"asset": "SPY"}, {"asset": "TLT"}]}

# The three fill stamps this suite reads. The IR engine fires the
# session_open batch at the first bar's END and the before_close(1) batch
# at the bar ending 60s before the close; at minute resolution that is
# 09:31 and 15:59. at_time fires on the bar ENDING at its wall time.
OPEN_MS = 34_260_000        # 09:31
BC_MS = 57_540_000          # 15:59
AT_1400_MS = 50_400_000     # 14:00


def _ir(rules):
    return {"ir_version": "0.2", "meta": {"name": "Alloc Shapes"},
            "params": {}, "universe": {"static": SYMS}, "rules": rules}


def alloc(rid, weights, **kw):
    rule = {"id": rid,
            "trigger": kw.pop("trigger", {"type": "before_close",
                                          "minutes": 1, "days": "all"}),
            "action": {"type": "set_weights", "weights": weights}}
    rule.update(kw)
    return rule


CASES = {
    # a day selector on the allocation rule
    "alloc_weekly": _ir([alloc("w", BEST, trigger={
        "type": "before_close", "minutes": 1, "days": "first_of_week"})]),
    # a guard
    "alloc_guarded": _ir([alloc("g", BEST, guard={"once_per": "week"})]),
    # a condition
    "alloc_conditional": _ir([alloc("c", BEST, when={
        "gt": [{"ind": "rsi", "window": 14, "symbol": "SPY"}, 40]})]),
    # a non-before_close trigger
    "alloc_at_open": _ir([alloc("o", BEST, trigger={
        "type": "session_open", "days": "all"})]),
    "alloc_at_time": _ir([alloc("t", BEST, trigger={
        "type": "at_time", "time": "14:00", "days": "all"})]),
    # TWO allocation rules — one morning, one at the close. They share the
    # engine's single WeightEngine, path collision and all (see below).
    "alloc_two_rules": _ir([
        alloc("morning", EQUAL, trigger={"type": "session_open",
                                         "days": "first_of_week"}),
        alloc("close", BEST)]),
    # allocation MIXED with a per-symbol rule.
    #
    # The hedge's threshold is 45, not the 35 this case was drafted with:
    # SPY's rsi(14) as the OPEN of a first-of-month session reads it (the
    # prior session's close) is 62.45 / 70.43 / 39.12 / 59.80 across this
    # window's four first-of-month sessions, so `< 35` never opens and the
    # case would have proved only the allocation rule it shares with
    # alloc_* above. At 45 the hedge fires on 2024-05-01 and is blocked on
    # the other three — a gate that actually gates.
    #
    # It also runs at 1.33x (MARGINS below). The rotation's weight vector
    # sums to 1.0, so at 1.0x the sleeve is fully invested from the first
    # close onwards and the hedge's buying power is zero: the order never
    # fills and the case degenerates into alloc_* again. Leverage is what
    # lets the two rules actually contend for the one book — which is the
    # thing this shape was refused for.
    "alloc_mixed": _ir([
        alloc("rot", BEST),
        {"id": "hedge",
         "trigger": {"type": "session_open", "days": "first_of_month"},
         "when": {"lt": [{"ind": "rsi", "window": 14, "symbol": "SPY"}, 45]},
         "action": {"type": "market_order", "side": "buy", "symbol": "TLT",
                    "size": {"pct_equity": 0.1}}}]),
    # once_per=POSITION on an allocation rule — the one guard kind a
    # rebalance can release itself, through the on_flat it hands the
    # Allocator. The IR engine consumes it BEFORE `_rebalance_to`
    # (engine.py's set_weights branch), so a rebalance that takes the
    # rule's symbol to zero ends with the flag CLEAR; consuming after
    # inverts it and the sleeve stops rebalancing for the rest of the run
    # (187 fills -> 4 over this window, before the fix).
    #
    # It has to be MIXED to show anything. A pure allocation strategy
    # cannot: the guard is consumed on the first rebalance, which starts
    # from an empty sleeve and so takes nothing to zero, and the rule is
    # then blocked forever on BOTH engines. Here a per-symbol rule re-buys
    # the PRIMARY (= the allocation rule's own symbol, since the action
    # carries none) every morning, and the close's rebalance sells it back
    # to zero: on_flat(SPY) releases the guard, and tomorrow it rebalances
    # again. The `best` tree deliberately excludes SPY so that sale is
    # unconditional — a tree that could pick SPY would keep the position
    # open, leave the guard consumed, and deadlock the case.
    "alloc_guard_position": _ir([
        {"id": "buyspy",
         "trigger": {"type": "session_open", "days": "all"},
         "when": {"not": {"pos": "invested", "symbol": "SPY"}},
         "action": {"type": "market_order", "side": "buy", "symbol": "SPY",
                    "size": {"pct_equity": 0.2}}},
        alloc("reb", {"best": {"of": [{"asset": s}
                                      for s in ("QQQ", "IWM", "TLT")],
                               "by": {"metric": "cum_return", "window": 60},
                               "order": "top", "n": 1}},
              guard={"once_per": "position"})]),
}


# 1.33x for the two mixed cases: at 1.0x the rotation's weights sum to 1.0,
# the sleeve is fully invested from the first close, and the per-symbol buy
# has no buying power left to contend with it (see alloc_mixed above).
MARGINS = {"alloc_mixed": 1.33, "alloc_guard_position": 1.33}


def _stamps(got):
    return {f["ms"] for f in got["fills"]}


def _days_at(got, ms):
    return sorted({f["day"] for f in got["fills"] if f["ms"] == ms})


def _fires_weekly(got):
    """A weekly cadence, at the close — not a daily one."""
    days = _days_at(got, BC_MS)
    assert days, "the allocation rule never rebalanced"
    assert _stamps(got) == {BC_MS}, _stamps(got)
    # 17 weeks in the window, 83 sessions: weekly must be far short of daily
    assert 6 <= len(days) <= 18, (len(days), len(got["equity_days"]))


def _fires_only_at(ms):
    def check(got):
        assert got["fills"], "the allocation rule never rebalanced"
        assert _stamps(got) == {ms}, _stamps(got)
    return check


def _fires_morning_and_close(got):
    assert _stamps(got) == {OPEN_MS, BC_MS}, _stamps(got)
    # the morning rule is first_of_week, the close rule is daily
    morning = _days_at(got, OPEN_MS)
    close = _days_at(got, BC_MS)
    assert 6 <= len(morning) <= 18, len(morning)
    assert len(close) > len(morning), (len(close), len(morning))


def _fires_hedge_and_rotation(got):
    """The rotation rebalances at the close; the hedge buys TLT at the open
    of the ONE first-of-month session whose rsi gate opens (2024-05-01)."""
    hedge = [f for f in got["fills"] if f["ms"] == OPEN_MS]
    assert hedge, "the per-symbol hedge never fired"
    assert [f["day"] for f in hedge] == ["2024-05-01"], hedge
    assert all(f["sym"] == "TLT" and f["qty"] > 0 for f in hedge), hedge
    assert BC_MS in _stamps(got), "the allocation rule never rebalanced"


def _keeps_rebalancing_after_the_guard_is_released(got):
    """The point of the case: the rebalance keeps happening. Its own
    on_flat releases the once_per=position guard every time it sells the
    morning's SPY back to zero, so a daily trigger stays daily. A
    generator that consumed the guard after the action rebalanced on
    2024-03-01 and never again."""
    closes = _days_at(got, BC_MS)
    opens = _days_at(got, OPEN_MS)
    assert len(closes) > 60, (len(closes), len(got["equity_days"]))
    assert len(opens) > 60, (len(opens), len(got["equity_days"]))
    # and the position really does come back each morning, which is what
    # makes each release a fresh one rather than a single early reset
    assert all(f["sym"] == "SPY" and f["qty"] > 0
               for f in got["fills"] if f["ms"] == OPEN_MS), got["fills"][:6]


CHECKS = {
    "alloc_weekly": _fires_weekly,
    "alloc_guarded": _fires_weekly,
    "alloc_conditional": _fires_only_at(BC_MS),
    "alloc_at_open": _fires_only_at(OPEN_MS),
    "alloc_at_time": _fires_only_at(AT_1400_MS),
    "alloc_two_rules": _fires_morning_and_close,
    "alloc_mixed": _fires_hedge_and_rotation,
    "alloc_guard_position": _keeps_rebalancing_after_the_guard_is_released,
}


@needs_data
@pytest.mark.parametrize("name", sorted(CASES))
def test_allocation_shape_matches_its_golden(name):
    got = assert_golden(CASES[name], start=WINDOW[0], end=WINDOW[1],
                        name=name, margin=MARGINS.get(name, 1.0))
    CHECKS[name](got)


@needs_data
def test_a_condition_on_an_allocation_rule_actually_gates_it():
    """alloc_conditional matching its golden is only worth something if the
    `when` ever closes. SPY's rsi(14) sits at or below 40 for twelve
    sessions of this window (the April 2024 drawdown), so the gated run and
    the ungated one must not be the same run."""
    from parity import generate, run_py
    gated = run_py(generate(CASES["alloc_conditional"]), WINDOW[0], WINDOW[1],
                   10000.0, DATA, None, None, None)
    ungated_ir = _ir([alloc("c", BEST)])
    ungated = run_py(generate(ungated_ir), WINDOW[0], WINDOW[1],
                     10000.0, DATA, None, None, None)
    assert "error" not in gated and "error" not in ungated
    assert gated["fills"] != ungated["fills"], \
        "the rsi gate never closed: the case proves nothing"


def test_all_allocation_rules_share_one_weight_engine():
    """IR truth, not an optimisation: Backtester holds ONE WeightEngine and
    calls evaluate(..., path="w") for every set_weights rule, so two rules
    share shadow state under the same node paths. Replicating that is why
    every Allocator here is built with ctx=self._bc."""
    code = codegen.generate_python(CASES["alloc_two_rules"])
    assert code.count("Allocator(") == 2
    assert code.count("ctx=self._bc") == 2
    assert code.count("BlockContext(") == 1


def test_a_weight_tree_is_never_compiled_to_python():
    """Weight trees are evaluated by the IR engine's own WeightEngine on
    both sides. A generator that compiled them would be the second
    implementation this whole phase exists to avoid."""
    code = codegen.generate_python(CASES["alloc_two_rules"])
    assert "WEIGHTS_W" not in code           # names come from the rule id
    assert "'best'" in code or '"best"' in code   # the tree, verbatim
    assert "self._cmp(lambda" not in code.split("def _rule_close")[1][:400]


def test_the_rebalance_delta_tie_break_is_still_hash_ordered():
    """PINNED, NOT ENDORSED.

    A rebalance seeds its deltas from `set(sleeve.qty) | set(want)` and
    sorts stably, so two symbols taking the SAME integer delta are submitted
    in the union set's iteration order -- Python's per-process string hash
    order. The submission SEQUENCE of such a rebalance is therefore not
    reproducible across processes, and the sequence is what decides which
    order gets filled when buying power binds part-way through.

    This used to be pinned on BOTH engines, so that a fix had to land on
    both at once or they would diverge silently. There is one engine now, so
    the constraint is gone and the fix is a one-liner:

        sorted(deltas, key=lambda x: (x[1], x[0]))

    Phase 3 does not take it -- changing what a live sleeve submits in the
    same phase that deletes the only second implementation is the one
    combination to avoid (cleanup plan Phase 3, Decision 2). Phase 4 owns
    it; this test is here so it is a decision and not an accident.
    """
    import inspect

    from dqengine.runtime.allocation import Allocator
    src = inspect.getsource(Allocator.rebalance)
    assert "for sym in set(sleeve.qty) | set(want):" in src
    assert src.count("sorted(deltas, key=lambda x: x[1])") == 1


def test_a_rebalance_cancels_resting_orders_first():
    """_rebalance_to deletes every managed target before it sizes — a
    rebalance owns the book. With allocation and per-symbol rules in one
    strategy a protective limit can now actually be resting there."""
    import inspect

    from dqengine.runtime.allocation import Allocator
    src = inspect.getsource(Allocator.rebalance)
    i, j = src.index("ensure_warm"), src.index("target_weights")
    assert "cancel()" in src[i:j], "cancel must precede sizing"


@needs_data
def test_a_partial_quantity_managed_target_exits_the_whole_position():
    """Decision 2: the IR engine never reads managed_target['qty'] —
    _check_targets sells the whole position. Accept the field, ignore it,
    and SAY so in the generated code rather than trading differently from
    the oracle over a field that has never done anything."""
    ir = _ir([
        {"id": "buy", "trigger": {"type": "session_open",
                                  "days": "first_of_week"},
         "when": {"not": {"pos": "invested", "symbol": "SPY"}},
         "action": {"type": "market_order", "side": "buy", "symbol": "SPY",
                    "size": {"pct_equity": 0.8}}},
        {"id": "tp", "trigger": {"type": "session_open", "days": "all"},
         "when": {"gt": [{"pos": "qty", "symbol": "SPY"}, 0]},
         "action": {"type": "managed_target", "symbol": "SPY", "qty": 0.5,
                    "price": {"mul": [{"pos": "entry_price",
                                       "symbol": "SPY"}, 1.02]}}},
    ])
    got = assert_golden(ir, start=WINDOW[0], end=WINDOW[1],
                        name="alloc_partial_tp")
    code = codegen.generate_python(ir)
    assert "never reads managed_target['qty']" in code

    # the fixture has to SHOW the whole-position exit: every target fill
    # closes the entire position the preceding buy opened, never half of
    # it, even though qty=0.5 asks for half.
    held = 0
    sells = 0
    for f in got["fills"]:
        assert f["sym"] == "SPY", f
        if f["qty"] > 0:
            held += f["qty"]
            continue
        sells += 1
        assert -f["qty"] == held, (f, held)
        held = 0
    assert sells >= 2, f"the +2% target must fill more than once ({sells})"


def test_a_carried_day_rebalance_sell_does_not_release_the_position_guard():
    """PINNED, NOT ENDORSED -- Hazard 6 of the Phase 3 spec.

    Chain: on a data-less session OrderBook.carried is True
    (backtester.py:410), so a market order RESTS as SUBMITTED
    (orders.py:309-315) instead of filling. Allocator.rebalance therefore
    skips on_flat, because it gates on `ticket.status == FILLED`
    (allocation.py:160-163) -- correctly, since a no-fill must not release
    a guard. The ticket fills later in check_resting (orders.py:423-426),
    which emits a FILLED event; the generated on_order_event then declines
    to release, because it only releases for LIMIT tickets
    (dqengine/codegen.py:793) and this one is MARKET.

    Net: a once_per=position guard whose position was closed by a
    carried-day rebalance is never released, so the rule that would
    re-enter stays blocked. The direction is safe (no wrong-way trade) but
    silent (a strategy quietly stops trading). The IR engine released it
    inline; with the oracle gone this is simply the behaviour.

    The fix belongs to Phase 4 and is small: on_order_event releases for a
    MARKET ticket too when the fill flattened the symbol AND the ticket
    carries a rebalance tag. Pinned here so taking it is deliberate.

    `alloc_guard_position` is the IR: a set_weights rule carrying
    once_per=position alongside a per-symbol rule, which is the only shape
    that can reach the hole.
    """
    code = codegen.generate_python(CASES["alloc_guard_position"])
    i = code.index("def on_order_event")
    # to the END of the method, not a fixed slice: the emitted block is
    # ~1100 characters, so a 900-char window would stop short of the
    # release itself and `OrderType.MARKET not in body` would be checking
    # the comment rather than the code.
    body = code[i:code.index("\n    def ", i)]
    assert "self._on_flat(e.symbol)" in body, body
    assert "ticket.order_type == OrderType.LIMIT" in body
    assert "OrderType.MARKET" not in body
