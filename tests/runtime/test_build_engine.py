"""One run.json, two entry points, one engine: the sandbox server and the
in-process mode must construct byte-for-byte the same engine -- ledger
type and cap, bar size, generated flag, overrides."""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from conftest_helpers import SynthStore, synth_day                       # noqa: E402
from dqengine.runtime.core.ledger import LiveCappedLedger                            # noqa: E402
from dqengine.runtime import engine_server as srv                              # noqa: E402
from dqengine.runtime.build import build_engine                                # noqa: E402

CODE = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 21); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
    def on_data(self, data): pass
"""
CFG = {"mode": "serve", "start": "2026-08-21", "cash": 2500.0, "bar_ms": 1000,
       "generated": True,
       "ledger": {"fills": [{"day": "2026-08-21", "ms": 34260000, "sym": "TQQQ",
                             "qty": 3, "px": 100.5, "fees": 0.1}],
                  "unknown": [], "reconciled_from": "2026-08-20",
                  "live_from": "2026-08-24"}}


class _Capture:
    """Stands in for WarmPyEngine and records exactly what it was built with."""
    built = []

    def __init__(self, code, data_root=None, overrides=None, ledger=None,
                 bar_ms=60_000, generated=False, store=None):
        _Capture.built.append({"code": code, "data_root": data_root,
                               "overrides": overrides, "ledger": ledger,
                               "bar_ms": bar_ms, "generated": generated})
        self.dead = None


def test_server_and_direct_build_are_identical(tmp_path):
    _Capture.built.clear()
    (tmp_path / "main.py").write_text(CODE)
    cfg = dict(CFG, data_root="/attacker/chosen")      # ignored by the server
    (tmp_path / "run.json").write_text(json.dumps(cfg))
    s = srv.EngineServer(str(tmp_path), engine_cls=_Capture, data_root=str(tmp_path))
    s.build()
    build_engine(cfg, CODE, str(tmp_path), engine_cls=_Capture)
    a, b = _Capture.built
    for k in ("code", "data_root", "overrides", "bar_ms", "generated"):
        assert a[k] == b[k], k
    assert a["overrides"] == {"start": "2026-08-21", "cash": 2500.0}
    assert a["bar_ms"] == 1000 and a["generated"] is True
    for led in (a["ledger"], b["ledger"]):
        assert isinstance(led, LiveCappedLedger)
        assert led.live_from == date(2026, 8, 24)
        inner = led._inner if hasattr(led, "_inner") else led.inner
        assert inner.reconciled_from == date(2026, 8, 20)
        assert len(inner._fills) == 1 and inner._fills[0].fees == 0.1


def test_build_engine_reads_bars_from_the_given_root_not_the_cfg():
    """data_root is the caller's: a run.json cannot point an engine at a
    path of its choosing."""
    seen = {}

    class Cap(_Capture):
        def __init__(self, code, data_root=None, **k):
            seen["root"] = data_root
            self.dead = None
    build_engine(dict(CFG, data_root="/somewhere/else"), CODE, "/mine", engine_cls=Cap)
    assert seen["root"] == "/mine"


def test_the_run_end_overrides_the_codes_placeholder_end_date():
    """Compiled blocks carry set_end_date(2026, 6, 9) as a placeholder. A
    warm engine built without `end` warmed to THAT and stepped the live
    day months stale. With `end` in the cfg the engine sees every session
    through it."""
    from datetime import date as _d
    from conftest_helpers import SynthStore, synth_day
    days = [_d(2026, 8, 24), _d(2026, 8, 25), _d(2026, 8, 26)]
    store = SynthStore({d: synth_day(d, [100, 101, 102]) for d in days})
    code = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24); self.set_end_date(2026, 8, 24)   # placeholder
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
        self.seen = []
    def on_data(self, data): self.seen.append(self.time.date())
"""
    from dqengine.runtime.warm import WarmPyEngine
    eng = build_engine({"start": "2026-08-24", "end": "2026-08-26", "cash": 1000.0},
                       code, "/nowhere", engine_cls=lambda c, **k: WarmPyEngine(c, store=store, **{x: v for x, v in k.items() if x != "data_root"}))
    eng.warm(through=_d(2026, 8, 26))
    assert sorted(set(eng._bt.algo.seen)) == days
    eng2 = build_engine({"start": "2026-08-24", "cash": 1000.0}, code, "/nowhere",
                        engine_cls=lambda c, **k: WarmPyEngine(c, store=store, **{x: v for x, v in k.items() if x != "data_root"}))
    eng2.warm(through=_d(2026, 8, 26))
    assert sorted(set(eng2._bt.algo.seen)) == days[:1], "the placeholder end wins without an override"
