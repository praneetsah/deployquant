"""Codegen invariant: for every shipped template, the ejected Python
backtested on dqengine.runtime lands on the fills and the equity curve that
template was PROVED to produce — same fills, equity to the penny.

That proof was a second engine until Phase 3; it is a committed golden
fixture now. The run is the same run: real bars, every fill, every daily
equity value. What changed is the thing it is compared against."""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dqengine import codegen                                                # noqa: E402
from parity import assert_golden                                 # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
STRATS = os.path.join(os.path.dirname(__file__), "..", "..", "strategies")

from conftest_helpers import reference_bars                           # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq", "tqqq")

# (file, margin, window) — margin mirrors how the template is meant to run
CASES = [
    ("sma_trend.json", 1.0, ("2024-01-02", "2025-06-30")),
    ("rsi_swing.json", 1.0, ("2024-01-02", "2025-06-30")),
    ("golden_cross.json", 1.0, ("2023-01-03", "2025-06-30")),
    ("monthly_invest.json", 1.0, ("2024-01-02", "2025-06-30")),
    ("dca_dip.json", 1.0, ("2024-01-02", "2025-06-30")),
    ("tqqq_weekly.json", 1.33, ("2024-01-02", "2025-06-30")),
    # starts on a WEDNESDAY: the weekly entry must not fire on day one
    # (2026-08-20 live: the compiled first_of_week read "no previous
    # session in this run" as "first of week"; the IR engine asks the
    # calendar, which reaches back through the store)
    ("tqqq_weekly.json", 1.33, ("2024-01-03", "2024-03-01")),
    # allocation (set_weights): multi-symbol, and the one shape the
    # single-symbol generator cannot express. Emitted code drives the SAME
    # WeightEngine/IndicatorEngine the IR engine drives.
    #
    # THREE windows, deliberately. The first version of this case used only
    # a window starting 2024-01-02 — which is the placeholder date codegen
    # emits into set_start_date — and that coincidence hid two real bugs:
    # the warm-up anchored on the placeholder instead of the run's start,
    # and it read daily zips where the IR engine reads minute session
    # closes. Both produced silently different holdings. A window that
    # equals the placeholder proves nothing about the ones that do not.
    ("rotation.json", 1.0, ("2024-01-02", "2024-06-28")),
    ("rotation.json", 1.0, ("2024-03-01", "2024-06-28")),
    ("rotation.json", 1.0, ("2025-01-02", "2025-06-30")),
]


def _ir(fname):
    return json.load(open(os.path.join(STRATS, fname)))


def test_generated_code_is_valid_python_for_all_templates():
    for fname, margin, _ in CASES:
        code = codegen.generate_python(_ir(fname), margin_max=margin)
        compile(code, fname, "exec")
        assert "class " in code and "def initialize" in code


@needs_data
def test_a_mixed_allocation_strategy_matches_its_golden():
    """set_weights alongside per-symbol rules used to be refused because
    the two would contend for the same book. They share one now — one
    sleeve, one WeightEngine, one indicator engine — exactly as the IR
    engine has always run them."""
    ir = _ir("rotation.json")
    ir["rules"] = ir["rules"] + [{
        "id": "extra", "trigger": {"type": "session_open", "days": "all"},
        "when": {"not": {"pos": "invested", "symbol": "SPY"}},
        "action": {"type": "market_order", "side": "buy",
                   "size": {"pct_equity": 0.1}, "symbol": "SPY"}}]
    # 1.33x, not 1.0x: rotation.json's weights sum to 1.0, so a 1.0x sleeve
    # is fully invested from its first close and the per-symbol buy has no
    # buying power left — it fires once, on the all-cash first morning, and
    # never again. Leverage is what lets the two rules actually contend for
    # the one book, which is the thing this shape used to be refused for.
    got = assert_golden(ir, start="2024-03-01", end="2024-06-28",
                        name="rotation_mixed", margin=1.33)
    # both rules have to be doing something, or this is the rotation case
    # again under a new name: the per-symbol rule buys SPY at the open
    # (09:31), the rotation rebalances at the close (15:59) — and sells the
    # morning's SPY back whenever SPY is not the momentum winner.
    opens = [f for f in got["fills"] if f["ms"] == 34_260_000]
    assert len(opens) > 20, f"the per-symbol rule barely fired ({len(opens)})"
    assert all(f["sym"] == "SPY" and f["qty"] > 0 for f in opens), opens
    assert any(f["ms"] == 57_540_000 for f in got["fills"]), \
        "the allocation rule never rebalanced"


@needs_data
@pytest.mark.parametrize("fname,margin,window", CASES,
                         ids=[f"{c[0]}-{c[2][0]}" for c in CASES])
def test_ejected_python_matches_its_golden(fname, margin, window):
    """Every shipped template, frozen.

    Through `assert_golden`, not a hand-rolled comparison, for the reason
    the spec states outright: "the generated python of each case is frozen
    as a golden fixture so the coverage survives the oracle". Phase 3
    deletes the IR engine and every test that compared against it; without
    a golden, sma_trend, rsi_swing, golden_cross, monthly_invest, dca_dip
    and rotation would lose their generated-code coverage with it, leaving
    only tqqq_weekly.json (covered by the acceptance gate).

    It is also a STRICTER comparison than the cross-engine one it replaces:
    assert_golden compares fills as (day, ms, SYMBOL, qty, price), and the
    old assertion dropped the symbol — on rotation.json, a four-symbol
    strategy, two runs that bought the same share count of different ETFs
    on the same minute at the same price would have passed.
    """
    ir = _ir(fname)
    start, end = window
    # one golden per (template, window): the three rotation windows produce
    # identical SOURCE and quite different fills, and it is the fills that
    # have to be pinned per window
    name = f"tpl_{fname[:-len('.json')]}_{start.replace('-', '')}"
    py = assert_golden(ir, start=start, end=end, name=name, cash=10000.0,
                       margin=margin, data=DATA)

    # ejected code must stay quiet-bar eligible (no on_data override, no
    # minute indicators) — this is what makes eject-vs-IR a fast-path gate.
    # Allocation strategies are the exception: they track per-session OHLC in
    # on_data to feed the daily series, which is what the IR engine does in
    # _end_session_multi, so the fast path cannot apply to them.
    if fname != "rotation.json":
        fp = py["fast_path"]
        assert fp["eligible"] and fp["sessions"] == fp["of"], (fname, fp)
    assert py["fills"], (fname, window, "window too quiet to prove anything")


def test_a_resting_entry_is_refused_not_turned_into_a_market_buy():
    """market_order with order_type=stop is a RESTING breakout entry in the
    IR engine. Emitting self.market_order() for it buys immediately, and
    on the python engine that trades the wrong way."""
    ir = _ir("sma_trend.json")
    for r in ir["rules"]:
        if r["action"]["type"] == "market_order":
            r["action"]["order_type"] = "stop"
            r["action"]["stop"] = {"mul": [{"ind": "price"}, 1.02]}
            break
    with pytest.raises(codegen.CodegenUnsupported, match="resting"):
        codegen.generate_python(ir)


def _flat_sell_with_a_resting_target_ir():
    """A managed target resting over a position a MARKET sell flattens.

    The oracle can never fill that target: `_check_targets` deletes it the
    moment `sleeve.qty <= 0`. `dqengine.runtime`'s `check_resting` has no such
    guard — it fills a resting LIMIT sell whenever the price crosses,
    position or no position — so the generated code has to cancel the
    ticket itself when a fill leaves the symbol flat, or the sleeve goes
    SHORT where the oracle stays flat.

    Reachable only through the shapes this phase unlocked: a market_order
    with side='sell' (and, equally, a sell at_close_order, which compiles
    to a plain market order). A `liquidate` cancels before it sells and a
    managed-target fill is itself a LIMIT fill, which is why the old
    LIMIT-only handler was enough before.
    """
    return {
        "ir_version": "0.2", "meta": {"name": "Flat Sell"}, "params": {},
        "universe": {"static": ["SPY"]},
        "rules": [
            {"id": "enter",
             "trigger": {"type": "session_open", "days": "first_of_week"},
             "when": {"not": {"pos": "invested"}},
             "action": {"type": "market_order", "side": "buy",
                        "size": {"pct_equity": 0.5}}},
            # a resting sell limit 1% above the entry, refreshed each
            # morning only while the position exists
            {"id": "target",
             "trigger": {"type": "session_open", "days": "all"},
             "when": {"pos": "invested"},
             "action": {"type": "managed_target", "qty": "all",
                        "price": {"mul": [{"pos": "entry_price"}, 1.01]}}},
            # ... and a market sell that flattens mid-week, leaving that
            # ticket resting under a position that no longer exists
            {"id": "flat",
             "trigger": {"type": "at_time", "time": "14:00",
                         "days": "day_before_last_of_week"},
             "when": {"pos": "invested"},
             "action": {"type": "market_order", "side": "sell",
                        "size": {"pct_position": 1.0}}},
        ],
    }


@needs_data
def test_a_market_sell_that_flattens_cancels_the_resting_target():
    got = assert_golden(_flat_sell_with_a_resting_target_ir(),
                        start="2024-03-01", end="2024-06-28",
                        name="flat_sell_cancels_target")
    pos = 0
    for f in got["fills"]:
        pos += int(f["qty"])
        assert pos >= 0, ("the sleeve went SHORT on a resting target under "
                          "a flat position", f, got["fills"])
    assert any(f["qty"] < 0 for f in got["fills"]), \
        "window too quiet: no flattening sell, so the gap is never reached"


def test_an_unknown_param_inside_an_indicator_window_is_refused():
    """Indicator nodes ship VERBATIM into the generated source now, so a
    `{"param": ...}` inside a window never passes through the expression
    compiler. Without an explicit check an unknown name would surface only
    as a KeyError partway through a backtest — on both engines, since both
    resolve it against the same params dict."""
    ir = _ir("sma_trend.json")
    for r in ir["rules"]:
        for node in (r.get("when") or {}).get("all", []):
            if "gt" in node:
                node["gt"][0] = {"ind": "sma", "window": {"param": "NOPE"}}
    with pytest.raises(codegen.CodegenUnsupported, match="NOPE"):
        codegen.generate_python(ir)


@pytest.mark.parametrize("bad", ["my param", "2x", "class"])
def test_a_param_name_that_is_not_a_python_name_is_refused_by_name(bad):
    """A param becomes a class attribute of the generated algorithm
    (`NAME = value`) and `self.NAME` at every use site, so `{"my param":
    10}` emits `my param = 10` and the module does not parse. The editor's
    param field stripped everything outside [A-Z0-9_] but allowed a leading
    digit, so typing `2x` yielded `2X` and an uncompilable strategy; the
    field strips the leading digit now, but a document saved before that,
    or imported, or written by the AI builder, never passed through it.

    Refuse it here, naming the param: the alternative is a SyntaxError
    raised on import, which names a line of generated source and no
    strategy at all -- and which jobs._blocks_as_python's bare `except`
    turns into a silent IR-engine fallback that Phase 3 deletes."""
    ir = _ir("sma_trend.json")
    ir["params"] = dict(ir.get("params") or {}, **{bad: 10})
    with pytest.raises(codegen.CodegenUnsupported, match="param named"):
        codegen.generate_python(ir)


def test_a_valid_param_name_still_generates_a_module_that_parses():
    """The other half: the check must not refuse the names strategies
    actually use. (Every other case in this file is a positive control
    too -- this one just adds an unused param to make the point.)"""
    ir = _ir("sma_trend.json")
    ir["params"] = dict(ir.get("params") or {}, SPARE_2X=10)
    compile(codegen.generate_python(ir), "<gen>", "exec")


def test_a_python_capsule_that_does_not_parse_reaches_the_generated_module():
    """The shape the corpus walker's compile() exists for: capsule code is
    spliced VERBATIM, so a strategy whose python block has a syntax error
    generates happily and only fails when something imports it."""
    ir = {"ir_version": "0.2", "meta": {"name": "Broken Capsule"},
          "params": {}, "universe": {"static": ["SPY"]},
          "rules": [{"id": "cap",
                     "trigger": {"type": "session_open", "days": "all"},
                     "action": {"type": "python",
                                "code": "if True\n    self.x = 1"}}]}
    code = codegen.generate_python(ir)          # no refusal
    with pytest.raises(SyntaxError):
        compile(code, "<gen>", "exec")


def test_a_before_close_rule_with_minutes_other_than_one_gets_its_own_slot():
    """Decision 1 of the one-engine cleanup: the IR engine SKIPS a
    before_close rule whose `minutes` is not 1 (engine.py's intraday walk
    and `_fire_before_close_batch_multi` both filter on minutes == 1),
    while the editor's free-form minutes field promises a fire at close
    minus m. The generated code keeps that promise, so no parity case can
    cover it — this pins the shape instead: a schedule entry and a
    dispatcher of its own, separate from the before-close(1) batch."""
    ir = {
        "ir_version": "0.2", "meta": {"name": "Two Slots"}, "params": {},
        "universe": {"static": ["SPY"]},
        "rules": [
            {"id": "late", "trigger": {"type": "before_close", "minutes": 1,
                                       "days": "all"},
             "action": {"type": "liquidate"}},
            {"id": "early", "trigger": {"type": "before_close", "minutes": 5,
                                        "days": "all"},
             "action": {"type": "liquidate"}},
        ],
    }
    code = codegen.generate_python(ir)
    compile(code, "<gen>", "exec")
    assert "before_market_close(self.sym, 1)" in code
    assert "before_market_close(self.sym, 5)" in code
    assert "def _before_close_5(self):" in code
    i = code.index("def _before_close_5(self):")
    assert "self._rule_early()" in code[i:]
    assert "self._rule_late()" not in code[i:]


LED_START, LED_END, LED_CASH = "2024-01-02", "2024-03-01", 10000.0


def _harvest(ir):
    """The model's OWN fills over the ledger window, with no ledger and no
    cash events in play. Every led_* case builds its broker rows from
    these, so the rows are a function of the same engine the case then
    replays -- which is why the case's own golden, not this harvest, is
    what pins the answer. If the harvest drifts, the rows drift and the
    golden fails."""
    from parity import generate, run_py
    py = run_py(generate(ir, 1.0), LED_START, LED_END, LED_CASH, DATA,
                None, None, None)
    assert "error" not in py, py.get("error")
    return py


@needs_data
def test_a_broker_ledger_replays_into_the_python_engine():
    """Plan D2 step 4, the adoption case: a deployment that already HOLDS a
    position with broker fills in its ledger. The replay must take the
    ledger rows for its own orders and land on the ledger's holdings, cash
    and fill prices -- the executor trades the difference otherwise.

    Frozen as `led_broker_fills`. Until Phase 3 this ran on both engines
    and compared them; the digest it now compares against was written from
    a run whose cross-engine agreement was checked one last time on
    2026-09-15, fill for fill and equity to the cent."""
    from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill
    ir = _ir("tqqq_weekly.json")
    # harvest the model's own fills, then hand them back as BROKER fills at
    # slightly different prices (the venue's, not the model's)
    base = _harvest(ir)["fills"]
    assert len(base) >= 4, "window too quiet to exercise the ledger"
    rows = [LedgerFill(day=date.fromisoformat(f["day"]), time_ms=f["ms"],
                       symbol=f["sym"], qty=int(f["qty"]),
                       price=round(f["px"] * (1.0007 if f["qty"] > 0 else 0.9993), 4),
                       fees=0.0, rule_tag=None, broker_order_id=f"b{i}")
            for i, f in enumerate(base)]
    ledger = ExecutionLedger(list(rows),
                             reconciled_from=date.fromisoformat(LED_START))
    py = assert_golden(ir, start=LED_START, end=LED_END, cash=LED_CASH,
                       margin=1.0, name="led_broker_fills", ledger=ledger)
    assert {r.price for r in rows} >= {f["px"] for f in py["fills"]}, \
        "fills carry the BROKER prices"
    assert all(f["confirmed"] for f in py["fills"])


@needs_data
def test_a_capped_ledger_with_partials_replays_into_the_python_engine():
    """The LIVE shape: LiveCappedLedger (today's rows absent -> the model's
    own fill stands, unconfirmed), a partial fill split across two rows
    with one broker_order_id, and a symbol the reconcile marked unknown.

    Frozen as `led_capped_partials`. The golden pins the holdings and
    prices; the `confirmed` flags are asserted here, because the digest
    does not carry them."""
    from dqengine.runtime.core.ledger import (ExecutionLedger, LedgerFill,
                                        LiveCappedLedger)
    ir = _ir("tqqq_weekly.json")
    fills = _harvest(ir)["fills"]
    assert len(fills) >= 4
    live_from = fills[-1]["day"]                   # the last fill's day is "today"
    rows = []
    for i, f in enumerate(fills):
        if f["day"] >= live_from:
            continue                               # no broker rows for today yet
        day, qty = date.fromisoformat(f["day"]), int(f["qty"])
        px = round(f["px"] * (1.0007 if qty > 0 else 0.9993), 4)
        if i == 0 and abs(qty) >= 2:               # first fill arrives in two pieces
            a = qty // 2; b = qty - a
            rows.append(LedgerFill(day=day, time_ms=f["ms"], symbol=f["sym"], qty=a,
                                   price=px, broker_order_id="b0"))
            rows.append(LedgerFill(day=day, time_ms=f["ms"] + 1, symbol=f["sym"], qty=b,
                                   price=round(px * 1.0001, 4), broker_order_id="b0"))
        else:
            rows.append(LedgerFill(day=day, time_ms=f["ms"], symbol=f["sym"],
                                   qty=qty, price=px, broker_order_id=f"b{i}"))

    inner = ExecutionLedger(list(rows),
                            reconciled_from=date.fromisoformat(LED_START))
    py = assert_golden(ir, start=LED_START, end=LED_END, cash=LED_CASH,
                       margin=1.0, name="led_capped_partials",
                       ledger=LiveCappedLedger(inner,
                                               live_from=date.fromisoformat(live_from)))
    today = [f for f in py["fills"] if f["day"] == live_from]
    assert today and not any(f["confirmed"] for f in today), \
        "today's fills are the model's, unconfirmed"
    assert all(f["confirmed"] for f in py["fills"] if f["day"] < live_from), \
        "settled days confirmed"


@needs_data
def test_the_tagged_ledger_row_wins_on_a_multi_execution_day():
    """Two broker rows for one symbol on one day -- one tagged with the
    rule that placed it, one untagged at a different price (the
    2026-08-31 duplicate-buy shape). The replay must take the TAGGED row
    for that rule's order, or it disagrees on cash with equal holdings and
    the adoption gate refuses the sleeve forever.

    Frozen as `led_tagged_row`. The price assertion below is the one that
    carries the meaning: the stray row sorts first and is 2% worse, so
    taking the wrong one is visible in a single number rather than only in
    the digest."""
    from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill
    ir = _ir("tqqq_weekly.json")
    entry = next(f for f in _harvest(ir)["fills"] if f["qty"] > 0)
    day, qty = date.fromisoformat(entry["day"]), int(entry["qty"])
    rows = [
        # the untagged stray sorts FIRST (earlier ms) at a worse price
        LedgerFill(day=day, time_ms=entry["ms"] - 1, symbol=entry["sym"],
                   qty=qty, price=round(entry["px"] * 1.02, 4),
                   rule_tag=None, broker_order_id="stray"),
        LedgerFill(day=day, time_ms=entry["ms"], symbol=entry["sym"],
                   qty=qty, price=round(entry["px"] * 1.001, 4),
                   rule_tag=entry["tag"], broker_order_id="real"),
    ]
    assert entry["tag"], "the model's fill must carry its rule id"
    ledger = ExecutionLedger(list(rows),
                             reconciled_from=date.fromisoformat(LED_START))
    py = assert_golden(ir, start=LED_START, end=LED_END, cash=LED_CASH,
                       margin=1.0, name="led_tagged_row", ledger=ledger)
    py_first = next(f for f in py["fills"] if f["qty"] > 0)
    assert py_first["px"] == round(entry["px"] * 1.001, 4), \
        "the replay took the untagged stray row"
    assert py_first["tag"] == entry["tag"]


@needs_data
def test_a_cash_deposit_replays_into_the_python_engine():
    """A live sleeve can receive cash after it started (SleeveEvent
    deposit), applied at the next session's open. The replay must do that
    from the same input, or the adoption gate can never route a sleeve that
    ever had a deposit -- and the rotation sleeve has one (2026-09-08).

    Frozen as `led_cash_deposit`, and the deposit has to MOVE something:
    the same window with no cash event must size differently, or the golden
    would pin a run in which the feature never engaged."""
    ir = _ir("tqqq_weekly.json")
    deposit_day = "2024-01-20"          # a Saturday: rolls to Monday's open
    py = assert_golden(ir, start=LED_START, end=LED_END, cash=LED_CASH,
                       margin=1.0, name="led_cash_deposit",
                       cash_events=[(deposit_day, 2500.0)])
    plain = _harvest(ir)
    assert [f["qty"] for f in py["fills"]] != [f["qty"] for f in plain["fills"]], \
        "window too quiet: the deposit changed no sizing"
    assert py["flows"][py["equity_days"].index("2024-01-22")] == 2500.0
    assert abs(py["position"]["cash"] - py["equity"][-1]
               + sum(h["market_value"] for h in py["position"]["holdings"])) < 0.01
