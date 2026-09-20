"""Codegen must emit PYTHON literals. A weight tree straight from a
Composer import carries JSON booleans (`include_today: true`) and params
may carry null; the generated module has to import, not NameError on
`true`. Found 2026-09-08: the rotation sleeve's adoption report died on
`name 'true' is not defined`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dqengine import codegen                                                # noqa: E402
from dqengine.runtime.algorithm_imports import register_algorithm_imports  # noqa: E402


def _ir_with_json_literals():
    return {
        "ir_version": "0.2", "meta": {"name": "literals"},
        "universe": {"static": ["SPY", "TLT"]},
        "params": {"flag": True, "nothing": None, "n": 3},
        "rules": [{"id": "alloc",
                   "trigger": {"type": "before_close", "minutes": 1,
                               "days": "all"},
                   "action": {"type": "set_weights", "weights": {
                       "if": {"gt": [{"ind": "rsi", "symbol": "SPY",
                                      "window": 10, "include_today": True},
                                     50.0]},
                       "then": [{"asset": "SPY"}],
                       "else": [{"asset": "TLT"}]}}}],
    }


def test_generated_module_with_json_booleans_and_null_imports():
    register_algorithm_imports()
    code = codegen.generate_python(_ir_with_json_literals(), margin_max=1.0)
    ns = {"__name__": "user_algorithm"}
    exec(compile(code, "<gen>", "exec"), ns)      # noqa: S102 -- the test
    assert ns["PARAMS"] == {"flag": True, "nothing": None, "n": 3}
    # one WEIGHTS_<rule id> constant per allocation rule, since set_weights
    # became an ordinary rule action
    assert ns["WEIGHTS_ALLOC"]["if"]["gt"][0]["include_today"] is True
