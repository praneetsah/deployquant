"""managed_target with order_type=trailing_stop compiles to one resting
trailing sell that is updated in place.

The IR engine's semantics, which the generated code has to reproduce:
  * the stop sits at high_water * (1 - trail_pct) (the IR engine rounded
    it to the cent; the runtime does not, see the pins below);
  * high_water starts at the last price seen when the rule first fires and
    takes the max with every later bar's HIGH (never a close);
  * a re-fire of the rule keeps the high-water mark (it only ever ratchets
    up), and the whole position is the order's quantity;
  * the exit fills through fills.stop_fill: min(open, stop) on a gap.

The two pinned results below are the python engine's. The IR engine
published 13,088.99 and 17,151.60 for the same runs (window
2021-01-04..2026-07-17, $10k): fills, trades and drawdowns match, and the
end equity differs by cents because the runtime keeps the trailing stop
unrounded. Rounding it in the runtime would also change live replays of
hand-written trailing_stop_order strategies, so it was left alone.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from parity import DATA                                         # noqa: E402
from dqengine import codegen                                    # noqa: E402
from dqengine.runtime import run_python_backtest                # noqa: E402

from conftest_helpers import reference_bars                     # noqa: E402
needs_data = reference_bars(DATA, "spy", "qqq")


def _ir(trail, name="Trail Shapes", when=None):
    return {
        "ir_version": "0.2", "meta": {"name": name},
        "params": {"TRAIL": 0.10},
        "universe": {"static": ["SPY"]},
        "rules": [
            {"id": "buy",
             "trigger": {"type": "session_open", "days": "all"},
             "when": {"all": [
                 {"not": {"pos": "invested"}},
                 {"gt": [{"ind": "price", "field": "close"},
                         {"ind": "sma", "window": 200}]}]},
             "action": {"type": "market_order", "side": "buy",
                        "size": {"pct_equity": 0.98}}},
            {"id": "trail",
             "trigger": {"type": "session_open", "days": "all"},
             "when": when or {"pos": "invested"},
             "action": {"type": "managed_target", "qty": "all",
                        "order_type": "trailing_stop", "trail_pct": trail}},
        ],
    }


@pytest.mark.parametrize("bad", [0, 1, 1.5, -0.1, True, "0.1",
                                 {"ind": "atr", "window": 14}])
def test_a_trail_that_is_not_a_fraction_literal_or_param_is_refused(bad):
    with pytest.raises(codegen.CodegenUnsupported, match="trail"):
        codegen.generate_python(_ir(bad))


def test_a_param_trail_compiles_and_a_limit_target_still_does():
    py = codegen.generate_python(_ir({"param": "TRAIL"}))
    assert "self._place_or_update_trail('SPY', qty, self.TRAIL" in py
    assert "OrderType.TRAILING_STOP):" in py        # exit fill releases guards


def test_only_a_trailing_strategy_gets_the_trailing_helper():
    ir = _ir(0.1)
    ir["rules"][1]["action"] = {"type": "managed_target", "qty": "all",
                                "price": {"mul": [{"pos": "entry_price"},
                                                  1.05]}}
    py = codegen.generate_python(ir)
    assert "_place_or_update_trail" not in py
    assert "TRAILING_STOP" not in py


def test_a_name_that_starts_with_a_digit_still_compiles():
    py = codegen.generate_python(_ir(0.1, name="20-day momentum + trail"))
    compile(py, "<algorithm>", "exec")


def _run(ir, sym="SPY"):
    ir = dict(ir, universe={"static": [sym]})
    code = codegen.generate_python(ir, margin_max=1.0)
    res = run_python_backtest(
        code, data_root=DATA,
        overrides={"start": date(2021, 1, 4).isoformat(),
                   "end": date(2026, 7, 17).isoformat(), "cash": 10000.0})
    assert "error" not in res, res.get("error")
    return res


@needs_data
@pytest.mark.parametrize("sym,end_equity,dd,fills", [
    ("SPY", 13089.03, 31.23, 7),
    ("QQQ", 17151.61, 26.74, 9),
])
def test_matches_the_ir_engine_to_the_cents(sym, end_equity, dd, fills):
    res = _run(_ir(0.15), sym)
    st = res["stats"]
    assert (st["end_equity"], st["max_drawdown_pct"], len(res["fills"])) \
        == (end_equity, dd, fills)


@needs_data
def test_every_exit_is_the_trail_and_the_position_ends_flat_each_time():
    res = _run(_ir(0.15))
    pos = 0
    for f in res["fills"]:
        pos += int(f["qty"])
        assert pos >= 0, ("short under a trailing sell", f)
    sells = [f for f in res["fills"] if f["qty"] < 0]
    assert sells, "window too quiet: the trail never fired"
