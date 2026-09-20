"""Live-progress snapshots: pure observation of the running backtest —
throttled, crash-proof, and exactly consistent with the final result."""
import json

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester

from conftest_helpers import two_day_store


class Buyer(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        self.sym = self.add_equity("TQQQ").symbol

    def on_data(self, data):
        if not self.portfolio[self.sym].invested:
            self.market_order(self.sym, 5, tag="entry")
            self.log("bought!")


def test_snapshots_per_session_and_final_consistency():
    snaps = []
    algo = Buyer()
    res = PyBacktester(algo, two_day_store(), progress_cb=snaps.append,
                       progress_every_s=0).run()
    assert "error" not in res, res.get("error")
    # one per session + the final flush
    assert len(snaps) >= 2
    first, last = snaps[0], snaps[-1]
    assert first["sim_date"] == "2026-08-24" and 0 < first["pct"] <= 1
    assert last["pct"] == 1.0
    assert last["equity"] == res["equity"]
    assert last["equity_days"] == res["equity_days"]
    assert last["fills"] == 1
    assert "bought!" in last["logs_tail"]
    # snapshots must be JSON-serializable as-is (the sandbox dumps them)
    json.dumps(last)


def test_throttling_collapses_snapshots():
    snaps = []
    algo = Buyer()
    PyBacktester(algo, two_day_store(), progress_cb=snaps.append,
                 progress_every_s=3600).run()
    # throttled to (at most) the final flush
    assert len(snaps) == 1 and snaps[0]["pct"] == 1.0


def test_raising_callback_never_kills_the_run():
    def boom(_):
        raise RuntimeError("observer crashed")

    res = PyBacktester(Buyer(), two_day_store(), progress_cb=boom,
                       progress_every_s=0).run()
    assert "error" not in res and res["stats"]["fills"] == 1


def test_result_reports_actual_leverage():
    class Lev(Buyer):
        def initialize(self):
            super().initialize()
            self.securities[self.sym].set_leverage(1.33)

    res = PyBacktester(Lev(), two_day_store()).run()
    assert res["leverage"] == 1.33
    res2 = PyBacktester(Buyer(), two_day_store()).run()
    assert res2["leverage"] == 1.0
