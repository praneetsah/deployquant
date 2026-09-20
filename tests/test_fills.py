"""The shared fill rules, tested at the boundary.

These used to live twice — once in ir_engine/engine.py and once, copied, in
dqengine/runtime/orders.py. Now there is one implementation, so this is where the
semantics are pinned. Every case here is a boundary case, because the
boundary IS the rule: `>` vs `>=` is a different set of trades, and the
2022-04-20 TQQQ bar (high == limit exactly, no fill) is the reason.
"""
from dqengine.runtime.core import fills


# ------------------------------------------------------------------ limits

def test_buy_limit_fills_only_on_a_strict_dip_below():
    # low strictly below -> fills; the open wins when the bar gapped through
    assert fills.limit_fill(True, o=10.0, h=11.0, l=8.9, limit=9.0) == 9.0
    assert fills.limit_fill(True, o=8.5, h=11.0, l=8.0, limit=9.0) == 8.5
    # touching is not breaching
    assert fills.limit_fill(True, o=10.0, h=11.0, l=9.0, limit=9.0) is None
    assert fills.limit_fill(True, o=10.0, h=11.0, l=9.1, limit=9.0) is None


def test_sell_limit_fills_only_on_a_strict_rise_above():
    assert fills.limit_fill(False, o=10.0, h=11.1, l=9.0, limit=11.0) == 11.0
    assert fills.limit_fill(False, o=11.5, h=12.0, l=9.0, limit=11.0) == 11.5
    # the 2022-04-20 case: high == limit exactly -> no fill
    assert fills.limit_fill(False, o=10.0, h=11.0, l=9.0, limit=11.0) is None


# ------------------------------------------------------------------- stops

def test_buy_stop_triggers_above_sell_stop_below():
    assert fills.stop_fill(True, o=10.0, h=11.1, l=9.0, stop=11.0) == 11.0
    assert fills.stop_fill(True, o=11.5, h=12.0, l=9.0, stop=11.0) == 11.5
    assert fills.stop_fill(True, o=10.0, h=11.0, l=9.0, stop=11.0) is None

    assert fills.stop_fill(False, o=10.0, h=11.0, l=8.9, stop=9.0) == 9.0
    assert fills.stop_fill(False, o=8.5, h=11.0, l=8.0, stop=9.0) == 8.5
    assert fills.stop_fill(False, o=10.0, h=11.0, l=9.0, stop=9.0) is None


def test_a_missing_level_never_fills():
    """A resting order with no price on the leg being tested is not a fill at
    zero — it is not a fill at all."""
    assert fills.limit_fill(True, 10.0, 11.0, 9.0, None) is None
    assert fills.limit_fill(False, 10.0, 11.0, 9.0, None) is None
    assert fills.stop_fill(True, 10.0, 11.0, 9.0, None) is None
    assert fills.stop_fill(False, 10.0, 11.0, 9.0, None) is None


# ---------------------------------------------------------------- triggers

def test_stop_trigger_is_adverse_side_touch_trigger_is_favourable():
    # stop_limit: a BUY converts on a RISE to the trigger
    assert fills.stop_triggered(True, h=11.1, l=9.0, stop=11.0) is True
    assert fills.stop_triggered(True, h=11.0, l=9.0, stop=11.0) is False
    assert fills.stop_triggered(False, h=11.0, l=8.9, stop=9.0) is True

    # limit_if_touched: the mirror — a BUY converts on a FALL to the trigger
    assert fills.touch_triggered(True, h=11.0, l=8.9, trigger=9.0) is True
    assert fills.touch_triggered(True, h=11.0, l=9.0, trigger=9.0) is False
    assert fills.touch_triggered(False, h=11.1, l=9.0, trigger=11.0) is True

    assert fills.stop_triggered(True, 11.0, 9.0, None) is False
    assert fills.touch_triggered(True, 11.0, 9.0, None) is False


# ------------------------------------------------------------------ prices

def test_prices_round_to_the_minimum_price_variation():
    assert fills.round_price(24.17999) == 24.18
    assert fills.round_price(1) == 1.0
    assert fills.MIN_PRICE_VARIATION == 0.01


# --------------------------------------------- the engine calls THESE

def test_the_engine_imports_the_module_rather_than_copying_it():
    """The point of the refactor: not that the rules are correct here, but
    that there is nowhere else for them to be correct differently.

    Until Phase 3 Task 9 this asserted the same identity for two engines.
    There is one engine now, which makes the assertion MORE load-bearing,
    not less: with no second implementation to disagree with, a private copy
    growing inside dqengine/runtime/orders.py is the only way these rules can
    drift, and nothing would notice. `is` rather than `==`: a re-exported
    name would pass an equality check on the functions while still being a
    second module object with its own MIN_PRICE_VARIATION.
    """
    import dqengine.runtime.orders as qc_orders

    assert qc_orders.fills is fills
