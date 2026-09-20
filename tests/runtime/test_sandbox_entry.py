import json
import os

import pytest

from dqengine.runtime import run_python_backtest
from dqengine.runtime.sandbox_entry import main as entry_main

CODE = '''
from AlgorithmImports import *

class My(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 6, 1)
        self.set_end_date(2026, 6, 3)
        self.set_cash(1000)
        self.set_warm_up(2)
        self.sym = self.add_equity("TQQQ", Resolution.MINUTE).symbol
        # history during discovery must not blow up without data
        _ = self.history(self.sym, 5, Resolution.DAILY)
    def on_data(self, data):
        if not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 1)
'''

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
needs_data = pytest.mark.skipif(
    not os.path.isdir(os.path.join(DATA, "equity", "usa", "minute", "tqqq")),
    reason="local TQQQ minute data not present")


def test_manifest_only_no_data_needed():
    res = run_python_backtest(CODE, data_root="/nonexistent", manifest_only=True)
    assert "error" not in res, res.get("error")
    m = res["manifest"]
    assert m["subscriptions"] == ["TQQQ"] and m["resolution"] == "minute"
    assert m["start"] == "2026-06-01" and m["cash"] == 1000
    assert m["warmup_days"] == 2
    assert os.environ.get("DQENGINE_MANIFEST_PASS") is None   # restored


def test_manifest_only_overrides_win():
    res = run_python_backtest(CODE, data_root="/x", manifest_only=True,
                              overrides={"start": "2026-06-02", "cash": 5000})
    assert res["manifest"]["start"] == "2026-06-02"
    assert res["manifest"]["cash"] == 5000


def test_entry_manifest_mode(tmp_path):
    (tmp_path / "main.py").write_text(CODE)
    (tmp_path / "run.json").write_text(json.dumps(
        {"mode": "manifest", "data_root": "/nonexistent"}))
    assert entry_main(str(tmp_path)) == 0
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["manifest"]["subscriptions"] == ["TQQQ"]


@needs_data
def test_entry_full_mode(tmp_path):
    (tmp_path / "main.py").write_text(CODE)
    (tmp_path / "run.json").write_text(json.dumps(
        {"mode": "full", "start": "2026-06-01", "end": "2026-06-03",
         "cash": 1000, "data_root": DATA}))
    assert entry_main(str(tmp_path)) == 0
    r = json.loads((tmp_path / "result.json").read_text())
    assert "error" not in r, r.get("error")
    assert r["stats"]["fills"] >= 1


def test_entry_user_error_lands_in_json_exit_zero(tmp_path):
    (tmp_path / "main.py").write_text("def broken(:\n")
    (tmp_path / "run.json").write_text(json.dumps(
        {"mode": "full", "data_root": "/x"}))
    assert entry_main(str(tmp_path)) == 0
    r = json.loads((tmp_path / "result.json").read_text())
    assert r["error"]["type"] == "SyntaxError"


def test_entry_runner_failure_exit_one(tmp_path):
    (tmp_path / "run.json").write_text("not json{")
    assert entry_main(str(tmp_path)) == 1


@needs_data
def test_entry_full_mode_writes_progress(tmp_path):
    (tmp_path / "main.py").write_text(CODE)
    (tmp_path / "run.json").write_text(json.dumps(
        {"mode": "full", "start": "2026-06-01", "end": "2026-06-03",
         "cash": 1000, "data_root": DATA}))
    assert entry_main(str(tmp_path)) == 0
    p = json.loads((tmp_path / "progress.json").read_text())
    assert p["pct"] == 1.0 and "ts" in p
    r = json.loads((tmp_path / "result.json").read_text())
    assert p["equity"] == r["equity"]
