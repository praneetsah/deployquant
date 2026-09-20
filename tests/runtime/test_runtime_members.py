"""Wave 4 runtime members, plus the tier-1 additions."""
import pytest

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.enums import Resolution
from dqengine.runtime.errors import UnsupportedApiError

from conftest_helpers import two_day_store


def test_parameters_round_trip_and_coerce_to_the_defaults_type():
    """QC hands parameters back as strings; pasted code passes an int
    default and expects an int."""
    a = QCAlgorithm()
    a.set_parameters({"lookback": "20", "pct": "0.5", "name": "x"})
    assert a.get_parameter("lookback", 5) == 20 and isinstance(
        a.get_parameter("lookback", 5), int)
    assert a.get_parameter("pct", 0.0) == 0.5
    assert a.get_parameter("name", "") == "x"
    assert a.get_parameter("missing", 7) == 7
    assert a.get_parameter("missing") is None
    assert a.get_parameters()["lookback"] == "20"


def test_an_uncoercible_parameter_falls_back_rather_than_raising():
    a = QCAlgorithm()
    a.set_parameters({"n": "not-a-number"})
    assert a.get_parameter("n", 5) == 5


def test_dates_benchmark_and_currency_are_readable():
    a = QCAlgorithm()
    a.set_start_date(2026, 1, 5)
    a.set_end_date(2026, 6, 1)
    a.set_benchmark("SPY")
    assert a.start_date.year == 2026 and a.end_date.month == 6
    assert a.benchmark == "SPY"
    assert a.account_currency == "USD"


def test_active_securities_mirrors_the_subscriptions():
    a = QCAlgorithm()
    a.add_equity("SPY")
    assert "SPY" in a.active_securities


def test_brokerage_model_reports_the_leverage_it_actually_applies():
    a = QCAlgorithm()
    assert a.brokerage_model["account_leverage"] == 1.0
    a.set_brokerage_model("IB", "AccountType.Margin")
    assert a.brokerage_model["account_leverage"] == 2.0


def test_runtime_statistics_and_tags():
    a = QCAlgorithm()
    a.set_runtime_statistic("edge", "1.2")
    assert a.runtime_statistics == {"edge": "1.2"}
    a.add_tag("live")
    a.set_tags(["a", "b"])
    assert a.tags == ["a", "b"]


def test_notify_is_a_manager_and_says_nothing_was_delivered():
    a = QCAlgorithm()
    a.notify.email("x@y.dev", "subject", "body")
    a.notify.sms("+1", "hi")
    a.notify.web("https://x.dev")
    joined = " ".join(a._logs)
    assert joined.count("not delivered") == 3
    assert "no network" in joined


# ---------------- quit ----------------

class Quitter(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.bars = 0

    def on_data(self, data):
        self.bars += 1
        if self.bars == 2:
            self.market_order(self.sym, 1)
            self.quit("done early")


def test_quit_ends_the_run_but_keeps_what_already_filled():
    algo = Quitter()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert algo.bars == 2                      # stopped on the spot
    assert len(res["fills"]) == 1              # the fill survives
    assert any("done early" in line for line in res["logs"])
    assert algo.status == "Stopped"


def test_a_run_that_never_quits_reports_running():
    a = QCAlgorithm()
    assert a.status == "Running"
    a.set_quit(True)
    assert a.status == "Stopped"


# ---------------- current slice ----------------

class SliceReader(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.seen = []

    def on_data(self, data):
        self.seen.append(self.current_slice is data)


def test_current_slice_is_the_slice_on_data_was_given():
    algo = SliceReader()
    PyBacktester(algo, two_day_store()).run()
    assert algo.seen and all(algo.seen)


# ---------------- tier 1: warm-up, order conveniences, prices ----------------

class WarmUpUser(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 25)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.sym = self.add_equity("TQQQ").symbol
        self.ind = self.sma(self.sym, 2, Resolution.DAILY)
        self.warmed = self.warm_up_indicator(self.sym, self.ind,
                                             Resolution.DAILY)


def test_warm_up_indicator_makes_it_ready_before_the_first_bar():
    """A cold indicator trades on nothing for its whole warm-up window and
    says nothing about it — this is the fix for that."""
    algo = WarmUpUser()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert algo.ind.samples > 0


def test_indicator_history_returns_the_series_it_produced():
    from dqengine.runtime.indicators import SimpleMovingAverage

    class A(WarmUpUser):
        def initialize(self):
            self.set_start_date(2026, 8, 25)
            self.set_end_date(2026, 8, 25)
            self.set_cash(10000)
            self.sym = self.add_equity("TQQQ").symbol
            self.series = self.indicator_history(
                SimpleMovingAverage(2), self.sym, 2, Resolution.DAILY)

    algo = A()
    PyBacktester(algo, two_day_store()).run()
    assert isinstance(algo.series, list)


def test_warm_up_indicator_rejects_a_selector():
    a = QCAlgorithm()
    a.add_equity("SPY")
    ind = a.sma("SPY", 5)
    with pytest.raises(UnsupportedApiError, match="selector"):
        a.warm_up_indicator("SPY", ind, selector=lambda b: b.high)


class Conveniences(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(100000)
        self.sym = self.add_equity("TQQQ").symbol
        self.done = False

    def on_data(self, data):
        if not self.done:
            self.done = True
            self.buy(self.sym, 5)
            self.sell(self.sym, 2)
            self.order(self.sym, -1)
            self.px = self.get_last_known_price(self.sym)
            self.all_px = self.get_last_known_prices()


def test_buy_sell_and_order_are_signed_correctly():
    algo = Conveniences()
    res = PyBacktester(algo, two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert [f["qty"] for f in res["fills"]] == [5, -2, -1]


def test_last_known_price_reports_what_the_engine_has_seen():
    algo = Conveniences()
    PyBacktester(algo, two_day_store()).run()
    assert algo.px == 100.0
    assert algo.all_px == {"TQQQ": 100.0}


def test_last_known_price_is_none_before_any_bar():
    a = QCAlgorithm()
    a.add_equity("SPY")
    assert a.get_last_known_price("SPY") is None
    assert a.get_last_known_prices() == {}


def test_remove_security_drops_the_subscription_and_its_orders():
    a = QCAlgorithm()
    a.add_equity("SPY")
    a.sma("SPY", 5)
    assert a.remove_security("SPY") is True
    assert "SPY" not in a.securities and "SPY" not in a._indicators
    assert a.remove_security("SPY") is False        # already gone


def test_add_security_routes_equities_and_refuses_anything_else():
    a = QCAlgorithm()
    assert a.add_security("SPY").symbol == "SPY"
    with pytest.raises(UnsupportedApiError, match="security_type"):
        a.add_security("SecurityType.Option", "SPY")


def test_message_logs_are_readable():
    a = QCAlgorithm()
    a.log("hello")
    a.error("bad")
    assert any("hello" in m for m in a.log_messages)
    assert a.error_messages and "bad" in a.error_messages[0]


def test_set_finished_warming_up_reopens_orders():
    a = QCAlgorithm()
    a.is_warming_up = True
    a.set_finished_warming_up()
    assert a.is_warming_up is False


class StepCounter(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(10000)
        self.add_equity("TQQQ")
        self.steps = 0

    def on_end_of_time_step(self):
        self.steps += 1


def test_on_end_of_time_step_fires_once_per_session():
    algo = StepCounter()
    PyBacktester(algo, two_day_store()).run()
    assert algo.steps == 2
