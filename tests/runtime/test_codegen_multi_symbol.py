"""Multi-symbol RULE strategies (no allocation tree) — spec §3 Phase 2
item 2. The IR engine runs these on its multi path: 260 warm-up sessions,
rules fired at union timestamps, a rule's action symbol from
action["symbol"], and expression leaves defaulting to universe[0] (NOT to
the action symbol — `Backtester._ctx` builds every EvalContext with
self.default_symbol). All four properties are load-bearing here."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA, assert_golden                          # noqa: E402
from dqengine import codegen                                               # noqa: E402

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

WINDOW = ("2024-03-01", "2024-06-28")


def two_symbol_ir():
    """One sleeve, two tickers, two rules with different action symbols.
    The `when` on the TLT rule reads an SMA with NO symbol of its own —
    which the IR engine resolves to universe[0] (SPY), not to TLT."""
    return {
        "ir_version": "0.2", "meta": {"name": "Two Sleeves"},
        "params": {"FRAC": 0.4},
        "universe": {"static": ["SPY", "TLT"]},
        "rules": [
            {"id": "buy-spy",
             "trigger": {"type": "session_open", "days": "first_of_week"},
             "when": {"not": {"pos": "invested", "symbol": "SPY"}},
             "action": {"type": "market_order", "side": "buy", "symbol": "SPY",
                        "size": {"pct_equity": {"param": "FRAC"}}}},
            {"id": "buy-tlt",
             "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
             "when": {"lt": [{"ind": "sma", "window": 10},
                             {"ind": "sma", "window": 30}]},
             "guard": {"once_per": "week"},
             "action": {"type": "market_order", "side": "buy", "symbol": "TLT",
                        "size": {"pct_equity": 0.25}}},
        ],
    }


def cross_symbol_ir():
    """One traded symbol, an indicator on another — collect_ir_symbols
    returns two, so the IR engine takes its MULTI path (and its 260-session
    warm-up) even though only one ticker is ever held."""
    ir = {
        "ir_version": "0.2", "meta": {"name": "Cross Gate"},
        "params": {},
        "universe": {"static": ["SPY"]},
        "rules": [
            {"id": "enter",
             "trigger": {"type": "session_open", "days": "all"},
             "when": {"all": [{"not": {"pos": "invested"}},
                              {"gt": [{"ind": "rsi", "window": 14,
                                       "symbol": "QQQ"}, 55]}]},
             "action": {"type": "market_order", "side": "buy",
                        "size": {"pct_equity": 0.5}}},
            {"id": "exit",
             "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
             "when": {"lt": [{"ind": "rsi", "window": 14, "symbol": "QQQ"},
                             45]},
             "action": {"type": "liquidate"}},
        ],
    }
    return ir


@needs_data
def test_two_symbol_rule_strategy_matches_its_golden():
    assert_golden(two_symbol_ir(), start=WINDOW[0], end=WINDOW[1],
                  name="multi_two_symbol")


@needs_data
def test_a_cross_symbol_gate_matches_its_golden():
    assert_golden(cross_symbol_ir(), start=WINDOW[0], end=WINDOW[1],
                  name="multi_cross_symbol")


def test_every_referenced_symbol_is_subscribed():
    """A symbol the generated code never subscribes has no price, so its
    indicator reads NOT_READY forever and the gate silently never fires."""
    code = codegen.generate_python(cross_symbol_ir())
    assert '"QQQ"' in code or "'QQQ'" in code
    assert "SYMS = ['SPY', 'QQQ']" in code or "SYMS = ['QQQ', 'SPY']" in code


def test_a_leaf_without_a_symbol_binds_to_the_universe_head():
    """IR truth: Backtester._ctx passes self.default_symbol (universe[0])
    for EVERY rule, whatever the action's own symbol is. A generator that
    bound bare leaves to the action symbol would gate the TLT rule on TLT's
    own SMA and trade differently, silently."""
    code = codegen.generate_python(two_symbol_ir())
    i = code.index("def _rule_buy_tlt")
    body = code[i:i + 900]
    assert "'SPY'" in body and "'TLT'" not in body.split("market_order")[0]
