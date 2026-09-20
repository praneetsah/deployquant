"""Unsupported arguments must be LOUD.

This runtime is a reimplementation of the QC surface, not LEAN. Every API is
one we chose to implement, so a gap is inevitable — the question is only
whether it announces itself. Silently swallowing an argument is the worst
failure mode here: the code is valid QC, it is accepted without complaint,
and it trades differently than it reads. That is exactly how a strategy
asking for 2x leverage ran at 1x for three days.
"""
import pytest

from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.enums import Resolution
from dqengine.runtime.errors import UnsupportedApiError


# ---------- add_equity ----------

def test_extended_market_hours_is_refused():
    a = QCAlgorithm()
    with pytest.raises(UnsupportedApiError, match="extended_market_hours"):
        a.add_equity("SPY", Resolution.MINUTE, extended_market_hours=True)


def test_raw_data_normalization_is_refused():
    a = QCAlgorithm()
    with pytest.raises(UnsupportedApiError, match="data_normalization_mode"):
        a.add_equity("SPY", Resolution.MINUTE,
                     data_normalization_mode="DataNormalizationMode.Raw")


def test_adjusted_normalization_is_accepted():
    """It is what we already do, so saying it explicitly must not fail."""
    a = QCAlgorithm()
    a.add_equity("SPY", Resolution.MINUTE,
                 data_normalization_mode="DataNormalizationMode.Adjusted")


def test_a_non_us_market_is_refused():
    a = QCAlgorithm()
    with pytest.raises(UnsupportedApiError, match="market"):
        a.add_equity("VOD", Resolution.MINUTE, market="Market.LSE")
    a.add_equity("SPY", Resolution.MINUTE, market="Market.USA")   # fine


def test_fill_forward_false_is_refused():
    a = QCAlgorithm()
    with pytest.raises(UnsupportedApiError, match="fill_forward"):
        a.add_equity("SPY", Resolution.MINUTE, fill_forward=False)


def test_an_argument_we_never_heard_of_is_refused():
    a = QCAlgorithm()
    with pytest.raises(UnsupportedApiError, match="future_arg"):
        a.add_equity("SPY", Resolution.MINUTE, future_arg=1)


# ---------- indicators ----------

def test_indicator_selector_is_refused_on_every_helper():
    """selector picks WHICH field feeds the indicator. Ours are fed the
    close, always — returning a close-based SMA to code that asked for a
    high-based one is wrong in a way nothing about the result reveals."""
    a = QCAlgorithm()
    a.add_equity("SPY")
    for fn in (a.sma, a.ema, a.std, a.max, a.min, a.rsi, a.atr):
        with pytest.raises(UnsupportedApiError, match="selector"):
            fn("SPY", 20, selector=lambda b: b.high)


def test_a_non_wilder_rsi_is_refused():
    a = QCAlgorithm()
    a.add_equity("SPY")
    with pytest.raises(UnsupportedApiError, match="moving_average_type"):
        a.rsi("SPY", 14, "MovingAverageType.Simple")
    a.rsi("SPY", 14, "MovingAverageType.Wilders")      # what we implement


def test_moving_average_type_is_importable_the_way_pasted_code_spells_it():
    import dqengine.runtime.algorithm_imports as ai
    a = QCAlgorithm()
    a.add_equity("SPY")
    a.rsi("SPY", 14, ai.MovingAverageType.WILDERS, ai.Resolution.DAILY)
    with pytest.raises(UnsupportedApiError, match="moving_average_type"):
        a.rsi("SPY", 14, ai.MovingAverageType.EXPONENTIAL)


def test_ema_smoothing_factor_is_refused():
    a = QCAlgorithm()
    a.add_equity("SPY")
    with pytest.raises(UnsupportedApiError, match="smoothing_factor"):
        a.ema("SPY", 20, 0.5)


def test_plain_indicator_calls_still_work():
    a = QCAlgorithm()
    a.add_equity("SPY")
    assert a.sma("SPY", 20) is not None
    assert a.rsi("SPY", 14, None, Resolution.DAILY) is not None


# ---------- liquidate ----------

class _Book:
    def __init__(self, qty):
        self.sleeve = type("S", (), {"qty": qty})()
        self.orders = []

    def open_tickets(self, s):
        return []

    def market(self, s, q, price=None, tag=""):
        self.orders.append((s, q))


def _algo_with(qty):
    a = QCAlgorithm()
    a._book = _Book(dict(qty))
    a._prices = {k: 100.0 for k in qty}
    return a


def test_liquidate_a_list_actually_liquidates_each():
    """str(["A","B"]) used to become one nonsense ticker matching nothing,
    so this silently did nothing at all."""
    a = _algo_with({"AAA": 5, "BBB": -3, "CCC": 7})
    a.liquidate(["AAA", "BBB"])
    assert sorted(a._book.orders) == [("AAA", -5), ("BBB", 3)]


def test_liquidate_accepts_qcs_other_two_spellings():
    a = _algo_with({"AAA": 5})
    a.liquidate(symbols=["AAA"])
    assert a._book.orders == [("AAA", -5)]

    b = _algo_with({"AAA": 5})
    b.liquidate(symbol_to_liquidate="AAA")
    assert b._book.orders == [("AAA", -5)]


def test_liquidate_everything_still_works():
    a = _algo_with({"AAA": 5, "BBB": -3})
    a.liquidate()
    assert sorted(a._book.orders) == [("AAA", -5), ("BBB", 3)]


# ---------- the guard ----------

def test_no_public_method_silently_swallows_arguments():
    """A regression fence. Any new *a/**k on the public surface must reject
    what it cannot honour — add reject_extra() to it, or this fails."""
    import inspect
    import re
    from dqengine.runtime import algorithm as mod

    offenders = []
    src = inspect.getsource(mod)
    tree = __import__("ast").parse(src)
    ast = __import__("ast")
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "QCAlgorithm")
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        if not (node.args.vararg or node.args.kwarg):
            continue
        body = ast.get_source_segment(src, node) or ""
        # a method may validate inline, or delegate to a named validator
        validators = ("reject_extra(", "unsupported(", "unsupported_arg(",
                      "_to_date(")
        if not any(v in body for v in validators):
            offenders.append(node.name)
    assert offenders == [], (
        f"these swallow arguments without rejecting them: {offenders}")
