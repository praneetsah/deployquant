"""Python capsule blocks: arbitrary code spliced verbatim into generated
strategies — the mechanism that makes every python algorithm expressible
as blocks (native skeleton + capsules)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest                                                     # noqa: E402

from dqengine import codegen                                                 # noqa: E402
from dqengine.runtime import run_python_backtest                        # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
needs_data = pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "tqqq")),
    reason="local bar data not present")

CAPSULE_IR = {
    "ir_version": "0.2",
    "meta": {"name": "Capsule Demo"},
    "params": {"SIZE": 0.5},
    "universe": {"static": ["TQQQ"]},
    "capsules": {
        "setup": "self.high_water = 0.0\nself.entries = 0",
    },
    "rules": [
        {"id": "enter", "trigger": {"type": "session_open", "days": "all"},
         "when": {"not": {"pos": "invested"}},
         "action": {"type": "market_order", "side": "buy",
                    "size": {"pct_equity": {"param": "SIZE"}}}},
        {"id": "trail", "trigger": {"type": "before_close", "minutes": 5,
                                    "days": "all"},
         "action": {"type": "python",
                    "params": {"TRAIL_PCT": 0.05},
                    "code": ("px = self.securities[self.sym].price\n"
                             "if px > self.high_water:\n"
                             "    self.high_water = px\n"
                             "if (self.portfolio[self.sym].invested and\n"
                             "        px < self.high_water * (1 - self.TRAIL_PCT)):\n"
                             "    self.liquidate(self.sym)\n"
                             "    self.high_water = 0.0\n"
                             "    self.log('capsule trail exit')")}},
    ],
}


def test_capsule_ir_generates_valid_python():
    code = codegen.generate_python(CAPSULE_IR)
    assert "TRAIL_PCT = 0.05" in code          # param hoisted to a field
    assert "self.high_water = 0.0" in code     # setup spliced
    assert "capsule trail exit" in code        # rule code spliced
    compile(code, "<gen>", "exec")


@needs_data
def test_capsule_strategy_runs_and_capsule_logic_fires():
    code = codegen.generate_python(CAPSULE_IR)
    res = run_python_backtest(code, data_root=DATA,
                              overrides={"start": "2022-01-03",
                                         "end": "2022-06-30", "cash": 10000})
    assert "error" not in res, res.get("error")
    assert res["stats"]["fills"] >= 2          # entries AND capsule exits
    assert any("capsule trail exit" in ln for ln in res["logs"])


def test_multi_symbol_allowed_with_capsules():
    ir = {**CAPSULE_IR,
          "universe": {"static": ["TQQQ", "SPY"]}}
    code = codegen.generate_python(ir)
    assert "'SPY'" in code and "self.symbols" in code
    compile(code, "<gen>", "exec")


def test_multi_symbol_no_longer_needs_capsules():
    """Multi-symbol rule strategies compile since Phase 2 Task 2: the
    generator drives the IR engine's own IndicatorEngine, so there is
    nothing single-symbol about it any more."""
    ir = dict(CAPSULE_IR)
    ir["capsules"] = {}
    ir["universe"] = {"static": ["TQQQ", "SPY"]}
    ir["rules"] = [r for r in ir["rules"] if r["action"]["type"] != "python"]
    code = codegen.generate_python(ir)
    compile(code, "<gen>", "exec")
    assert "SYMS = ['TQQQ', 'SPY']" in code


def test_bad_param_names_refused():
    ir = {**CAPSULE_IR}
    ir["rules"] = [dict(CAPSULE_IR["rules"][1])]
    ir["rules"][0] = {**ir["rules"][0],
                      "action": {**ir["rules"][0]["action"],
                                 "params": {"lower_case": 1}}}
    with pytest.raises(codegen.CodegenUnsupported):
        codegen.generate_python(ir)


def test_on_data_capsule_emitted():
    ir = {**CAPSULE_IR,
          "capsules": {"on_data": "if self.sym in data.bars:\n"
                                  "    self._last_bar = data.bars[self.sym]"}}
    code = codegen.generate_python(ir)
    assert "def on_data(self, data):" in code
    compile(code, "<gen>", "exec")


@needs_data
def test_a_capsule_strategy_takes_a_deposit():
    """jobs used to refuse deposits for capsule strategies because the
    router could not route them anywhere else. The runtime always could."""
    code = codegen.generate_python(CAPSULE_IR)
    plain = run_python_backtest(code, data_root=DATA,
                                overrides={"start": "2024-01-02",
                                           "end": "2024-03-01",
                                           "cash": 10000.0})
    dep = run_python_backtest(code, data_root=DATA,
                              overrides={"start": "2024-01-02",
                                         "end": "2024-03-01",
                                         "cash": 10000.0,
                                         "cash_events": [("2024-01-20", 2500.0)]})
    assert "error" not in dep, dep.get("error")
    i = dep["equity_days"].index("2024-01-22")     # the 20th is a Saturday
    assert dep["flows"][i] == 2500.0
    assert dep["equity"][-1] > plain["equity"][-1]
