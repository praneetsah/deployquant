"""The Wave 1 indicator library.

Expected values are computed by hand or from the textbook definition in the
test itself — never by running the implementation and pasting what it said,
which proves only that the code is deterministic.
"""
import pytest

from dqengine.runtime import indicators as I
from dqengine.runtime.algorithm import QCAlgorithm
from dqengine.runtime.errors import UnsupportedApiError


def feed(ind, values):
    for v in values:
        ind.update(None, v)
    return ind


def feed_bars(ind, bars):
    """bars: (high, low, close, volume)"""
    for h, l, c, v in bars:
        ind.update_bar(None, h, l, c, v)
    return ind


# ---------------- value-fed ----------------

def test_sum_and_identity():
    assert feed(I.Sum(3), [1, 2, 3, 4]).value == 9        # 2+3+4
    assert feed(I.Identity(), [7]).value == 7


def test_momentum_family():
    v = [10, 11, 12, 15]
    assert feed(I.Momentum(3), v).value == 5              # 15 - 10
    # LEAN's MomentumPercent is a PERCENT, not a fraction — verified against
    # real LEAN in test_lean_parity, and off by 100x before that caught it
    assert feed(I.MomentumPercent(3), v).value == 50.0    # 5/10 x 100
    assert feed(I.RateOfChange(3), v).value == 0.5        # the fraction
    assert feed(I.RateOfChangePercent(3), v).value == 50.0
    assert feed(I.RateOfChangeRatio(3), v).value == 1.5


def test_log_return():
    import math
    got = feed(I.LogReturn(1), [100, 110]).value
    assert abs(got - math.log(1.1)) < 1e-12


def test_variance_and_mad():
    # [2,4,6]: mean 4, var = (4+0+4)/3, mad = (2+0+2)/3
    assert abs(feed(I.Variance(3), [2, 4, 6]).value - 8 / 3) < 1e-12
    assert abs(feed(I.MeanAbsoluteDeviation(3), [2, 4, 6]).value - 4 / 3) < 1e-12


def test_linear_weighted_moving_average():
    # weights 1,2,3 over [1,2,3] -> (1*1 + 2*2 + 3*3)/6 = 14/6
    assert abs(feed(I.LinearWeightedMovingAverage(3), [1, 2, 3]).value
               - 14 / 6) < 1e-12


def test_wilder_moving_average_seeds_then_smooths():
    w = feed(I.WilderMovingAverage(3), [3, 6, 9])
    assert w.value == 6.0                                  # simple seed
    w.update(None, 15)                                     # 6 + (15-6)/3
    assert w.value == 9.0


def test_dema_and_tema_track_a_constant():
    """Any moving average of a constant series is that constant."""
    for cls in (I.DoubleExponentialMovingAverage,
                I.TripleExponentialMovingAverage,
                I.HullMovingAverage, I.TriangularMovingAverage,
                I.ZeroLagExponentialMovingAverage):
        got = feed(cls(5), [42.0] * 60).value
        assert abs(got - 42.0) < 1e-6, cls.__name__


def test_chande_momentum_oscillator_is_100_when_only_up():
    """Wilder-smoothed (LEAN-verified), but a purely one-sided series still
    pins at the extremes."""
    assert feed(I.ChandeMomentumOscillator(3), [1, 2, 3, 4]).value == 100.0
    assert feed(I.ChandeMomentumOscillator(3), [4, 3, 2, 1]).value == -100.0


def test_macd_is_fast_minus_slow_and_has_a_signal():
    m = feed(I.MovingAverageConvergenceDivergence(3, 6, 4), range(1, 40))
    assert abs(m.value - (m.fast.value - m.slow.value)) < 1e-12
    assert m.signal.value != 0.0
    assert abs(m.histogram.value - (m.value - m.signal.value)) < 1e-12


def test_bollinger_bands_are_symmetric_around_the_middle():
    b = feed(I.BollingerBands(5, 2), [1, 2, 3, 4, 5])
    assert b.middle_band.value == 3.0
    assert abs((b.upper_band.value - b.middle_band.value)
               - (b.middle_band.value - b.lower_band.value)) < 1e-12
    # sd of 1..5 (population) is sqrt(2)
    assert abs(b.upper_band.value - (3 + 2 * 2 ** 0.5)) < 1e-12
    assert b.upper.value == b.upper_band.value          # QC's short spelling


def test_ppo_is_apo_normalised():
    """Both use SIMPLE moving-average legs, as LEAN does — not the EMAs the
    textbook definition calls for (tools/lean_probe.py settled it)."""
    vals = list(range(1, 40))
    a = feed(I.AbsolutePriceOscillator(3, 6), vals)
    p = feed(I.PercentagePriceOscillator(3, 6), vals)
    assert abs(p.value - a.value / a.slow.value * 100.0) < 1e-9
    assert isinstance(a.fast, I.SimpleMovingAverage)


# ---------------- bar-fed ----------------

def test_true_range_uses_the_previous_close():
    tr = I.TrueRange()
    tr.update_bar(None, 10, 8, 9, 0)
    assert tr.value == 2.0                    # first bar: high - low
    tr.update_bar(None, 12, 11, 11, 0)        # gap up from 9
    assert tr.value == 3.0                    # |12 - 9|


def test_williams_percent_r_bounds():
    w = feed_bars(I.WilliamsPercentR(3), [(10, 0, 10, 1)] * 3)
    assert w.value == 0.0                     # close at the high
    w2 = feed_bars(I.WilliamsPercentR(3), [(10, 0, 0, 1)] * 3)
    assert w2.value == -100.0                 # close at the low


def test_internal_bar_strength():
    assert feed_bars(I.InternalBarStrength(), [(10, 0, 7.5, 1)]).value == 0.75


def test_stochastic_k_and_d():
    s = feed_bars(I.Stochastic(3, d_period=2), [(10, 0, 5, 1)] * 4)
    assert s.k.value == 50.0
    assert s.d.value == 50.0
    assert s.value == s.stoch_k.value


def test_donchian_channel_tracks_extremes():
    d = feed_bars(I.DonchianChannel(3),
                  [(10, 2, 5, 1), (12, 4, 6, 1), (11, 1, 7, 1)])
    assert d.upper_band.value == 12 and d.lower_band.value == 1
    assert d.value == 6.5


def test_midprice_vs_midpoint():
    bars = [(10, 2, 6, 1), (12, 4, 8, 1)]
    assert feed_bars(I.MidPrice(2), bars).value == (12 + 2) / 2
    assert feed_bars(I.MidPoint(2), bars).value == (8 + 6) / 2


def test_on_balance_volume_follows_close_direction():
    """LEAN SEEDS with the first bar's volume rather than starting at zero
    (test_lean_parity caught this); then +up, -down."""
    obv = feed_bars(I.OnBalanceVolume(),
                    [(1, 1, 10, 100), (1, 1, 11, 50), (1, 1, 9, 30)])
    assert obv.value == 120.0                 # 100 seed, +50, -30


def test_accumulation_distribution_at_the_extremes():
    assert feed_bars(I.AccumulationDistribution(),
                     [(10, 0, 10, 100)]).value == 100.0
    assert feed_bars(I.AccumulationDistribution(),
                     [(10, 0, 0, 100)]).value == -100.0


def test_money_flow_index_all_up_is_100():
    bars = [(i + 1, i, i + 0.5, 10) for i in range(1, 8)]
    assert feed_bars(I.MoneyFlowIndex(5), bars).value == 100.0


def test_chaikin_money_flow_at_the_high():
    assert feed_bars(I.ChaikinMoneyFlow(2), [(10, 0, 10, 5)] * 2).value == 1.0


def test_vwap_is_volume_weighted():
    # typical prices 6 and 9, volumes 1 and 3 -> (6 + 27)/4
    v = feed_bars(I.VolumeWeightedAveragePriceIndicator(2),
                  [(6, 6, 6, 1), (9, 9, 9, 3)])
    assert abs(v.value - 33 / 4) < 1e-12


def test_vwma_weights_closes_by_volume():
    v = feed_bars(I.VolumeWeightedMovingAverage(2),
                  [(1, 1, 10, 1), (1, 1, 20, 3)])
    assert abs(v.value - (10 * 1 + 20 * 3) / 4) < 1e-12


def test_adx_is_fifty_on_a_flat_tape_and_rises_on_a_trend():
    """LEAN returns 50 — not 0 — when the directional sum is zero, i.e. when
    there is no directional movement at all (AverageDirectionalIndex.cs).
    This test asserted 0 until the source said otherwise."""
    flat = feed_bars(I.AverageDirectionalIndex(3), [(10, 9, 9.5, 1)] * 20)
    assert flat.value == 50.0
    up = feed_bars(I.AverageDirectionalIndex(3),
                   [(10 + i, 9 + i, 9.5 + i, 1) for i in range(20)])
    assert up.value > 50.0
    assert up.positive.value > up.negative.value


def test_aroon_up_is_100_on_a_fresh_high():
    a = feed_bars(I.AroonOscillator(3),
                  [(10 + i, 9 + i, 9.5 + i, 1) for i in range(6)])
    assert a.aroon_up.value == 100.0
    assert a.value > 0


def test_psar_flips_side_on_a_reversal():
    up = [(10 + i, 9 + i, 9.5 + i, 1) for i in range(12)]
    down = [(21 - i, 20 - i, 20.5 - i, 1) for i in range(12)]
    p = feed_bars(I.ParabolicStopAndReverse(), up)
    below = p.value < up[-1][2]
    feed_bars(p, down)
    assert below and p.value > down[-1][2]     # was under price, now over


def test_keltner_channels_widen_with_range():
    k = feed_bars(I.KeltnerChannels(3, 2), [(11, 9, 10, 1)] * 10)
    assert k.upper_band.value > k.middle_band.value > k.lower_band.value


def test_cci_is_zero_on_a_flat_tape():
    assert feed_bars(I.CommodityChannelIndex(5), [(10, 8, 9, 1)] * 5).value == 0.0


# ---------------- wiring ----------------

def test_every_registered_indicator_is_reachable_and_rejects_a_selector():
    a = QCAlgorithm()
    a.add_equity("SPY")
    names = (list(QCAlgorithm._IND_VALUE) + list(QCAlgorithm._IND_BAR)
             + list(QCAlgorithm._IND_MULTI))
    assert len(names) >= 40
    for n in names:
        assert getattr(a, n)("SPY", 14) is not None, n
        with pytest.raises(UnsupportedApiError, match="selector"):
            getattr(a, n)("SPY", 14, selector=lambda b: b.high)


def test_pascal_aliases_exist():
    a = QCAlgorithm()
    a.add_equity("SPY")
    assert a.MACD("SPY", 12, 26, 9) is not None
    assert a.BB("SPY", 20) is not None
    assert a.ADX("SPY", 14) is not None


# ---------------- dual-symbol statistics ----------------

def feed_pair(ind, target, reference):
    """Both streams share a timestamp per step — LEAN only advances when
    every symbol has data for the SAME time."""
    for i, (a, b) in enumerate(zip(target, reference)):
        ind.update_symbol("SPY", a, i)
        ind.update_symbol("QQQ", b, i)
    return ind


def test_beta_of_a_series_against_itself_is_one():
    """The definitional anchor: cov(x,x)/var(x) == 1."""
    xs = [100 + i * 0.5 + 3 * (i % 7) for i in range(40)]
    b = feed_pair(I.Beta("SPY", "QQQ", 20), xs, xs)
    assert b.value == pytest.approx(1.0, rel=1e-9)


def test_correlation_of_a_series_against_itself_is_one():
    xs = [100 + i * 0.5 + 3 * (i % 7) for i in range(40)]
    c = feed_pair(I.Correlation("SPY", "QQQ", 20), xs, xs)
    assert c.value == pytest.approx(1.0, rel=1e-9)


def test_correlation_of_an_inverted_series_is_minus_one():
    xs = [100 + i * 0.5 + 3 * (i % 7) for i in range(40)]
    ys = [-x for x in xs]
    c = feed_pair(I.Correlation("SPY", "QQQ", 20), xs, ys)
    assert c.value == pytest.approx(-1.0, rel=1e-9)


def test_covariance_of_a_series_against_itself_is_its_variance():
    xs = [100 + i * 0.5 + 3 * (i % 7) for i in range(40)]
    cov = feed_pair(I.Covariance("SPY", "QQQ", 20), xs, xs)
    from dqengine.runtime.indicators import _sample_var
    assert cov.value == pytest.approx(_sample_var(cov._tr), rel=1e-12)


def test_beta_is_double_when_the_target_moves_twice_as_much():
    """Returns scaled 2x give beta 2 — the property the name promises."""
    base = [100.0]
    for i in range(1, 60):
        base.append(base[-1] * (1 + 0.01 * ((i % 5) - 2)))
    ref = base
    tgt = [100.0]
    for i in range(1, 60):
        r = base[i] / base[i - 1] - 1
        tgt.append(tgt[-1] * (1 + 2 * r))
    b = feed_pair(I.Beta("SPY", "QQQ", 30), tgt, ref)
    assert b.value == pytest.approx(2.0, rel=1e-6)


def test_a_dual_indicator_ignores_a_symbol_it_does_not_track():
    b = I.Beta("SPY", "QQQ", 5)
    before = b.samples
    b.update_symbol("IWM", 123.0)
    assert b.samples == before


def test_correlation_refuses_spearman():
    with pytest.raises(UnsupportedApiError, match="correlation_type"):
        I.Correlation("SPY", "QQQ", 10, "CorrelationType.Spearman")


def test_dual_indicators_are_reachable_from_the_algorithm():
    a = QCAlgorithm()
    a.add_equity("SPY")
    a.add_equity("QQQ")
    for name in ("b", "c", "cov"):
        ind = getattr(a, name)("SPY", "QQQ", 10)
        assert ind is not None
        # registered against BOTH streams
        assert any(i is ind for _r, i in a._indicators["SPY"])
        assert any(i is ind for _r, i in a._indicators["QQQ"])
