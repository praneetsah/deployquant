"""How a strategy declares its margin ceiling.

Every spelling LEAN accepts has to mean the same thing here, because the
failure mode is invisible: a run that silently drops 2x to 1x rejects the
buys it cannot afford and reports a flat, plausible-looking equity curve.
"""
from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.backtester import PyBacktester
from dqengine.runtime.enums import AccountType, BrokerageName, Resolution

from conftest_helpers import two_day_store


# ---------- declaration surface ----------

def test_set_leverage_on_the_security():
    a = QCAlgorithm()
    a.add_equity("TQQQ").set_leverage(2.0)
    assert a._effective_leverage() == 2.0


def test_add_equity_leverage_argument_keyword_and_positional():
    a = QCAlgorithm()
    a.add_equity("TQQQ", Resolution.MINUTE, leverage=2.0)
    assert a._effective_leverage() == 2.0

    b = QCAlgorithm()                       # LEAN's positional slot 5
    b.add_equity("TQQQ", Resolution.MINUTE, "usa", True, 3.0)
    assert b._effective_leverage() == 3.0


def test_add_equity_leverage_applies_to_an_existing_subscription():
    a = QCAlgorithm()
    first = a.add_equity("TQQQ")
    again = a.AddEquity("TQQQ", Resolution.MINUTE, leverage=2.0)
    assert again is first and a._effective_leverage() == 2.0


def test_margin_account_defaults_to_2x():
    a = QCAlgorithm()
    a.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage,
                        AccountType.Margin)
    a.add_equity("TQQQ")
    assert a._effective_leverage() == 2.0


def test_cash_account_stays_1x():
    a = QCAlgorithm()
    a.set_brokerage_model(BrokerageName.Webull, AccountType.Cash)
    a.add_equity("TQQQ")
    assert a._effective_leverage() == 1.0


def test_no_declaration_is_1x():
    a = QCAlgorithm()
    a.add_equity("TQQQ")
    assert a._effective_leverage() == 1.0


def test_security_initializer_callable_and_object():
    a = QCAlgorithm()
    a.set_security_initializer(lambda s: s.set_leverage(2.0))
    a.add_equity("TQQQ")
    assert a._effective_leverage() == 2.0

    class Init:
        def initialize(self, sec):
            sec.set_leverage(4.0)

    b = QCAlgorithm()
    b.SetSecurityInitializer(Init())
    b.add_equity("TQQQ")
    assert b._effective_leverage() == 4.0


def test_security_initializer_reaches_existing_subscriptions():
    a = QCAlgorithm()
    a.add_equity("TQQQ")                       # subscribed BEFORE the hook
    a.set_security_initializer(lambda s: s.set_leverage(2.0))
    assert a._effective_leverage() == 2.0


def test_per_security_leverage_beats_the_account_default():
    a = QCAlgorithm()
    a.set_brokerage_model(BrokerageName.Webull, AccountType.Margin)
    a.add_equity("TQQQ").set_leverage(4.0)
    a.add_equity("SPY")                        # inherits the account's 2x
    assert a._effective_leverage() == 4.0


# ---------- what the ceiling actually buys ----------

class LeveredBuy(QCAlgorithm):
    """$1000 of cash reaching for $1500 of stock: fills at 2x, rejected at 1x."""

    leverage = None

    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        sec = self.add_equity("TQQQ")
        if self.leverage:
            sec.set_leverage(self.leverage)
        self.sym = sec.symbol
        self.done = False

    def on_data(self, data):
        if not self.done:
            self.done = True
            self.market_order(self.sym, 15, tag="entry")   # 15 * 100 = $1500


def test_declared_leverage_lets_the_order_fill():
    class Levered(LeveredBuy):
        leverage = 2.0

    res = PyBacktester(Levered(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert res["leverage"] == 2.0
    assert len(res["fills"]) == 1
    assert res["rejections"] == {}


def test_undeclared_leverage_rejects_and_says_so():
    res = PyBacktester(LeveredBuy(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert res["leverage"] == 1.0
    assert res["fills"] == []
    assert res["rejections"] == {"insufficient buying power": 1}


class LateLeverage(LeveredBuy):
    """set_leverage called from on_data, after initialize has long returned."""

    def on_data(self, data):
        self.securities[self.sym].set_leverage(2.0)
        super().on_data(data)


def test_leverage_declared_after_initialize_still_counts():
    res = PyBacktester(LateLeverage(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert res["leverage"] == 2.0
    assert len(res["fills"]) == 1


class MarginAccountBuy(LeveredBuy):
    def initialize(self):
        super().initialize()
        self.set_brokerage_model(BrokerageName.InteractiveBrokersBrokerage,
                                 AccountType.Margin)


def test_margin_account_alone_is_enough_to_fill():
    """The exact shape that ran at 1x: a margin account, no set_leverage."""
    res = PyBacktester(MarginAccountBuy(), two_day_store()).run()
    assert "error" not in res, res.get("error")
    assert res["leverage"] == 2.0
    assert len(res["fills"]) == 1


def test_bare_brokerage_model_does_not_imply_margin():
    """LEAN would call this a margin account; we don't. Naming a broker says
    nothing about leverage, and guessing 2x here would double real risk."""
    a = QCAlgorithm()
    a.set_brokerage_model(BrokerageName.Webull)
    a.add_equity("TQQQ")
    assert a._effective_leverage() == 1.0


# ---------- the ceiling applies to shorts too ----------

class Script(QCAlgorithm):
    """One order per bar from `orders`: an int on TQQQ, or (ticker, qty).
    The synthetic store serves the same bars for every ticker, so a second
    symbol is a clean way to hold a long and a short at once."""

    orders: list = []
    leverage = None

    def initialize(self):
        self.set_start_date(2026, 8, 24)
        self.set_end_date(2026, 8, 25)
        self.set_cash(1000)
        for t in ("TQQQ", "SPY"):
            sec = self.add_equity(t)
            if self.leverage:
                sec.set_leverage(self.leverage)
        self.sym = "TQQQ"
        self.i = 0

    def on_data(self, data):
        if self.i < len(self.orders):
            o = self.orders[self.i]
            sym, qty = o if isinstance(o, tuple) else (self.sym, o)
            self.market_order(sym, qty)
        self.i += 1


def _run(**kw):
    return PyBacktester(type("S", (Script,), kw)(), two_day_store()).run()


def test_opening_a_short_beyond_the_ceiling_is_rejected():
    """$1000 of equity cannot carry a $1500 short at 1x. This used to fill:
    sells were never margin-checked at all."""
    res = _run(orders=[-15])
    assert res["fills"] == []
    assert res["rejections"] == {"insufficient buying power": 1}


def test_a_short_inside_the_ceiling_fills():
    res = _run(orders=[-8])                     # 8 * 100 = $800 of $1000
    assert len(res["fills"]) == 1 and res["fills"][0]["qty"] == -8
    assert res["rejections"] == {}


def test_a_short_consumes_buying_power_rather_than_freeing_it():
    """A long/short book at 2x: ~$1010 long leaves ~$1010 of room, so a $909
    short fits and a $1515 one does not. Under the old net-of-shorts formula
    the short read as EXTRA headroom and both filled."""
    ok = _run(orders=[10, ("SPY", -9)], leverage=2.0)
    assert [f["qty"] for f in ok["fills"]] == [10, -9]
    assert ok["rejections"] == {}

    over = _run(orders=[10, ("SPY", -15)], leverage=2.0)
    assert [f["qty"] for f in over["fills"]] == [10]
    assert over["rejections"] == {"insufficient buying power": 1}


def test_gross_exposure_counts_both_sides():
    from dqengine.runtime.core.portfolio import Sleeve
    s = Sleeve(1000.0, margin_max=2.0)
    s.qty = {"TQQQ": 10, "SPY": -9}
    prices = {"TQQQ": 100.0, "SPY": 100.0}
    assert s.position_value(prices) == 100.0        # net, for equity
    assert s.gross_exposure(prices) == 1900.0       # what margin caps


def test_covering_a_short_always_clears():
    """Reducing exposure needs no margin, however tight the book is."""
    res = _run(orders=[-9, 9])                  # short to the 1x limit, cover
    assert [f["qty"] for f in res["fills"]] == [-9, 9]
    assert res["rejections"] == {}


def test_flipping_long_to_short_is_charged_only_the_added_exposure():
    """Sell 12 while long 5: closes 5 and opens 7, so only the extra 2
    shares' worth of gross has to fit."""
    res = _run(orders=[5, -12])
    assert [f["qty"] for f in res["fills"]] == [5, -12]
