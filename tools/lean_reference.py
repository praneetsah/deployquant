"""Generate golden indicator values from REAL LEAN.

Run inside the quantconnect/lean image; writes JSON that the dqengine.runtime
indicator tests assert against. This is what turns "we implemented the
textbook definition" into "we match LEAN", which matters because a subtly
different definition is the silent-divergence bug this runtime exists to
avoid.

    docker run --rm -v <repo>/platform/engine/tools:/t \
      --entrypoint bash quantconnect/lean:latest -c \
      "pip install -q clr_loader pythonnet && cd /Lean/Launcher/bin/Debug && \
       python /t/lean_reference.py > /t/lean_reference.json"
"""
import json
import math
import sys
from datetime import datetime, timedelta

from clr_loader import get_coreclr
from pythonnet import set_runtime

set_runtime(get_coreclr(runtime_config="QuantConnect.Lean.Launcher.runtimeconfig.json"))
import clr                                                    # noqa: E402

sys.path.append(".")
clr.AddReference("QuantConnect.Indicators")
clr.AddReference("QuantConnect.Common")

import System                                                  # noqa: E402
from System import Activator, DateTime, Decimal                # noqa: E402
from QuantConnect.Indicators import IndicatorDataPoint         # noqa: E402
def safe_types(asm):
    """GetTypes() throws a PARTIAL ReflectionTypeLoadException here — some
    types reference a Python.Runtime version the image does not have. The
    ones that did load are still on the exception, and they include
    everything we need."""
    try:
        return [t for t in asm.GetTypes() if t is not None]
    except Exception as e:
        types = getattr(e, "Types", None)
        if types is None:
            raise
        return [t for t in types if t is not None]



# `import QuantConnect.Indicators as QI` trips a Python.Runtime version
# clash in this image, and Activator.CreateInstance loses the boxing on
# int arguments. `from ... import X` works for both, so every class is
# named explicitly.
from QuantConnect.Indicators import (  # noqa: E402
    AbsolutePriceOscillator, AccelerationBands, AccumulationDistribution,
    AccumulationDistributionOscillator, ArnaudLegouxMovingAverage,
    AroonOscillator, AugenPriceSpike, AverageDirectionalIndex,
    AverageDirectionalMovementIndexRating, AverageRange,
    AverageTrueRange, AwesomeOscillator, BalanceOfPower, BollingerBands,
    ChaikinMoneyFlow, ChandeKrollStop, ChandeMomentumOscillator,
    ChoppinessIndex, CommodityChannelIndex, ConnorsRelativeStrengthIndex,
    CoppockCurve, DeMarkerIndicator, DerivativeOscillator,
    DetrendedPriceOscillator, DonchianChannel,
    DoubleExponentialMovingAverage, EaseOfMovementValue,
    ExponentialMovingAverage, FisherTransform, ForceIndex,
    FractalAdaptiveMovingAverage, HeikinAshi, HullMovingAverage,
    HurstExponent, IchimokuKinkoHyo, InternalBarStrength,
    KaufmanAdaptiveMovingAverage, KaufmanEfficiencyRatio,
    KeltnerChannels, KlingerVolumeOscillator, KnowSureThing,
    LeastSquaresMovingAverage, LinearWeightedMovingAverage, LogReturn,
    MassIndex, Maximum, McGinleyDynamic, MeanAbsoluteDeviation, MidPoint,
    MidPrice, Minimum, Momentum, MomentumPercent, Momersion,
    MoneyFlowIndex, MovingAverageConvergenceDivergence,
    NormalizedAverageTrueRange, OnBalanceVolume, ParabolicStopAndReverse,
    PercentagePriceOscillator, PivotPointsHighLow,
    PremierStochasticOscillator, RateOfChange, RateOfChangePercent,
    RateOfChangeRatio, RegressionChannel, RelativeDailyVolume,
    RelativeMovingAverage, RelativeStrengthIndex, RelativeVigorIndex,
    RogersSatchellVolatility, SchaffTrendCycle, SimpleMovingAverage,
    SmoothedOnBalanceVolume, SqueezeMomentum, StandardDeviation,
    Stochastic, StochasticRelativeStrengthIndex, Sum, SuperTrend,
    T3MovingAverage, TimeSeriesForecast, TriangularMovingAverage,
    TripleExponentialMovingAverage, Trix, TrueRange, TrueStrengthIndex,
    UltimateOscillator, VariableIndexDynamicAverage, Variance,
    VolumeWeightedAveragePriceIndicator, VolumeWeightedMovingAverage,
    Vortex, WaveTrendOscillator, WilderAccumulativeSwingIndex,
    WilderMovingAverage, WilderSwingIndex, WilliamsPercentR,
    ZeroLagExponentialMovingAverage, ZigZag)


class _QI:
    """Attribute access over the explicitly-imported names."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name)


QI = _QI()

# The QuantConnect.Data namespace does not expose itself to pythonnet even
# with its assembly referenced, so TradeBar is reached by reflection and
# populated through its property setters. Fiddly, but it is the difference
# between guessing at LEAN's numbers and reading them.
_COMMON = [a for a in System.AppDomain.CurrentDomain.GetAssemblies()
           if "QuantConnect.Common" in str(a)][0]
_TB = [t for t in safe_types(_COMMON)
       if t.FullName == "QuantConnect.Data.Market.TradeBar"][0]
_SET = {n: _TB.GetProperty(n) for n in
        ("Time", "EndTime", "Open", "High", "Low", "Close", "Volume")}


def make_bar(t, o, h, lo, c, v):
    bar = Activator.CreateInstance(_TB)
    _SET["Time"].SetValue(bar, DateTime(t.year, t.month, t.day, t.hour,
                                        t.minute, 0))
    for name, x in (("Open", o), ("High", h), ("Low", lo), ("Close", c),
                    ("Volume", v)):
        _SET[name].SetValue(bar, System.Convert.ToDecimal(float(x)))
    return bar


N = 60


def series():
    """A deterministic, non-degenerate OHLCV series: a trend with a wobble,
    so trend/momentum/volatility indicators all have something to bite on."""
    out = []
    t = datetime(2024, 1, 2, 9, 31)
    for i in range(N):
        base = 100 + i * 0.5 + 3 * math.sin(i / 3.0)
        o = base
        h = base + 1 + (i % 3) * 0.25
        lo = base - 1 - (i % 5) * 0.2
        c = base + math.sin(i / 2.0) * 0.75
        v = 1000 + (i % 7) * 100
        out.append((t + timedelta(minutes=i), o, h, lo, c, v))
    return out


BARS = series()


def feed_value(ind):
    for t, o, h, lo, c, v in BARS:
        ind.Update(IndicatorDataPoint(DateTime(t.year, t.month, t.day,
                                               t.hour, t.minute, 0),
                                      System.Convert.ToDecimal(float(c))))
    return ind


def feed_bar(ind):
    for row in BARS:
        ind.Update(make_bar(*row))
    return ind


# name -> (constructor, how to feed, extra sub-series to record)
SPEC = {
    "SimpleMovingAverage": (lambda: QI.SimpleMovingAverage(10), feed_value, {}),
    "ExponentialMovingAverage": (lambda: QI.ExponentialMovingAverage(10), feed_value, {}),
    "RelativeStrengthIndex": (lambda: QI.RelativeStrengthIndex(14), feed_value, {}),
    "StandardDeviation": (lambda: QI.StandardDeviation(10), feed_value, {}),
    "Maximum": (lambda: QI.Maximum(10), feed_value, {}),
    "Minimum": (lambda: QI.Minimum(10), feed_value, {}),
    "Sum": (lambda: QI.Sum(10), feed_value, {}),
    "Momentum": (lambda: QI.Momentum(10), feed_value, {}),
    "MomentumPercent": (lambda: QI.MomentumPercent(10), feed_value, {}),
    "RateOfChange": (lambda: QI.RateOfChange(10), feed_value, {}),
    "RateOfChangePercent": (lambda: QI.RateOfChangePercent(10), feed_value, {}),
    "RateOfChangeRatio": (lambda: QI.RateOfChangeRatio(10), feed_value, {}),
    "LogReturn": (lambda: QI.LogReturn(10), feed_value, {}),
    "Variance": (lambda: QI.Variance(10), feed_value, {}),
    "MeanAbsoluteDeviation": (lambda: QI.MeanAbsoluteDeviation(10), feed_value, {}),
    "LinearWeightedMovingAverage": (lambda: QI.LinearWeightedMovingAverage(10), feed_value, {}),
    "TriangularMovingAverage": (lambda: QI.TriangularMovingAverage(10), feed_value, {}),
    "DoubleExponentialMovingAverage": (lambda: QI.DoubleExponentialMovingAverage(10), feed_value, {}),
    "TripleExponentialMovingAverage": (lambda: QI.TripleExponentialMovingAverage(10), feed_value, {}),
    "HullMovingAverage": (lambda: QI.HullMovingAverage(10), feed_value, {}),
    "WilderMovingAverage": (lambda: QI.WilderMovingAverage(10), feed_value, {}),
    "ZeroLagExponentialMovingAverage": (lambda: QI.ZeroLagExponentialMovingAverage(10), feed_value, {}),
    "ChandeMomentumOscillator": (lambda: QI.ChandeMomentumOscillator(10), feed_value, {}),
    "Trix": (lambda: QI.Trix(10), feed_value, {}),
    "AbsolutePriceOscillator": (lambda: QI.AbsolutePriceOscillator(5, 10), feed_value, {}),
    "PercentagePriceOscillator": (lambda: QI.PercentagePriceOscillator(5, 10), feed_value, {}),
    "MovingAverageConvergenceDivergence": (
        lambda: QI.MovingAverageConvergenceDivergence(5, 10, 4), feed_value,
        {"signal": "Signal", "fast": "Fast", "slow": "Slow"}),
    "BollingerBands": (lambda: QI.BollingerBands(10, D(2)), feed_value,
                       {"upper": "UpperBand", "middle": "MiddleBand",
                        "lower": "LowerBand"}),
    "KaufmanAdaptiveMovingAverage": (lambda: QI.KaufmanAdaptiveMovingAverage(10), feed_value, {}),
    "KaufmanEfficiencyRatio": (lambda: QI.KaufmanEfficiencyRatio(10), feed_value, {}),
    "T3MovingAverage": (lambda: QI.T3MovingAverage(10), feed_value, {}),
    "McGinleyDynamic": (lambda: QI.McGinleyDynamic(10), feed_value, {}),
    "DetrendedPriceOscillator": (lambda: QI.DetrendedPriceOscillator(10), feed_value, {}),
    "LeastSquaresMovingAverage": (lambda: QI.LeastSquaresMovingAverage(10), feed_value, {}),
    "TimeSeriesForecast": (lambda: QI.TimeSeriesForecast(10), feed_value, {}),
    "RelativeMovingAverage": (lambda: QI.RelativeMovingAverage(10), feed_value, {}),
    "Momersion": (lambda: QI.Momersion(10, 20), feed_value, {}),
    "TrueStrengthIndex": (lambda: QI.TrueStrengthIndex(10, 5, 4), feed_value, {}),
    "CoppockCurve": (lambda: QI.CoppockCurve(5, 8, 6), feed_value, {}),
    "ConnorsRelativeStrengthIndex": (lambda: QI.ConnorsRelativeStrengthIndex(3, 2, 10), feed_value, {}),
    "SchaffTrendCycle": (lambda: QI.SchaffTrendCycle(10, 12, 26), feed_value, {}),
    "PremierStochasticOscillator": (lambda: QI.PremierStochasticOscillator(10, 5), feed_bar, {}),
    "StochasticRelativeStrengthIndex": (lambda: QI.StochasticRelativeStrengthIndex(14, 14, 3, 3), feed_value,
                                        {"k": "K", "d": "D"}),
    "FisherTransform": (lambda: QI.FisherTransform(10), feed_bar, {}),
    "VariableIndexDynamicAverage": (lambda: QI.VariableIndexDynamicAverage(10), feed_value, {}),
    "ArnaudLegouxMovingAverage": (lambda: QI.ArnaudLegouxMovingAverage(10), feed_value, {}),

    # bar-fed
    "AverageTrueRange": (lambda: QI.AverageTrueRange(10), feed_bar, {}),
    "TrueRange": (lambda: QI.TrueRange(), feed_bar, {}),
    "NormalizedAverageTrueRange": (lambda: QI.NormalizedAverageTrueRange(10), feed_bar, {}),
    "AverageRange": (lambda: QI.AverageRange(10), feed_bar, {}),
    "MidPoint": (lambda: QI.MidPoint(10), feed_value, {}),
    "MidPrice": (lambda: QI.MidPrice(10), feed_bar, {}),
    "InternalBarStrength": (lambda: QI.InternalBarStrength(), feed_bar, {}),
    "BalanceOfPower": (lambda: QI.BalanceOfPower(), feed_bar, {}),
    "WilliamsPercentR": (lambda: QI.WilliamsPercentR(14), feed_bar, {}),
    "CommodityChannelIndex": (lambda: QI.CommodityChannelIndex(20), feed_bar, {}),
    "Stochastic": (lambda: QI.Stochastic(14, 14, 3), feed_bar,
                   {"k": "StochK", "d": "StochD", "fast": "FastStoch"}),
    "DonchianChannel": (lambda: QI.DonchianChannel(10), feed_bar,
                        {"upper": "UpperBand", "lower": "LowerBand"}),
    "KeltnerChannels": (lambda: QI.KeltnerChannels(10, D(2)), feed_bar,
                        {"upper": "UpperBand", "middle": "MiddleBand",
                         "lower": "LowerBand"}),
    "AverageDirectionalIndex": (lambda: QI.AverageDirectionalIndex(14), feed_bar,
                                {"pdi": "PositiveDirectionalIndex",
                                 "ndi": "NegativeDirectionalIndex"}),
    "AverageDirectionalMovementIndexRating": (
        lambda: QI.AverageDirectionalMovementIndexRating(14), feed_bar, {}),
    "AroonOscillator": (lambda: QI.AroonOscillator(10, 10), feed_bar,
                        {"up": "AroonUp", "down": "AroonDown"}),
    "OnBalanceVolume": (lambda: QI.OnBalanceVolume(), feed_bar, {}),
    "SmoothedOnBalanceVolume": (lambda: QI.SmoothedOnBalanceVolume(10), feed_bar, {}),
    "AccumulationDistribution": (lambda: QI.AccumulationDistribution(), feed_bar, {}),
    "AccumulationDistributionOscillator": (
        lambda: QI.AccumulationDistributionOscillator(5, 10), feed_bar, {}),
    "ChaikinMoneyFlow": (lambda: QI.ChaikinMoneyFlow(20), feed_bar, {}),
    "MoneyFlowIndex": (lambda: QI.MoneyFlowIndex(14), feed_bar, {}),
    "VolumeWeightedAveragePriceIndicator": (
        lambda: QI.VolumeWeightedAveragePriceIndicator(10), feed_bar, {}),
    "VolumeWeightedMovingAverage": (lambda: QI.VolumeWeightedMovingAverage(10), feed_bar, {}),
    "ParabolicStopAndReverse": (lambda: QI.ParabolicStopAndReverse(), feed_bar, {}),
    "SuperTrend": (lambda: QI.SuperTrend(10, D(3)), feed_bar, {}),
    "Vortex": (lambda: QI.Vortex(10), feed_bar,
               {"plus": "PlusVortex", "minus": "MinusVortex"}),
    "ChoppinessIndex": (lambda: QI.ChoppinessIndex(14), feed_bar, {}),
    "MassIndex": (lambda: QI.MassIndex(9, 25), feed_bar, {}),
    "EaseOfMovementValue": (lambda: QI.EaseOfMovementValue(10, 10000), feed_bar, {}),
    "ForceIndex": (lambda: QI.ForceIndex(10), feed_bar, {}),
    "KlingerVolumeOscillator": (lambda: QI.KlingerVolumeOscillator(5, 10), feed_bar, {}),
    "RelativeVigorIndex": (lambda: QI.RelativeVigorIndex(10), feed_bar, {}),
    "UltimateOscillator": (lambda: QI.UltimateOscillator(7, 14, 28), feed_bar, {}),
    "DeMarkerIndicator": (lambda: QI.DeMarkerIndicator(14), feed_bar, {}),
    "AccelerationBands": (lambda: QI.AccelerationBands(20, D(4)), feed_bar,
                          {"upper": "UpperBand", "middle": "MiddleBand",
                           "lower": "LowerBand"}),
    "HeikinAshi": (lambda: QI.HeikinAshi(), feed_bar,
                   {"open": "Open", "high": "High", "low": "Low",
                    "close": "Close"}),
    "IchimokuKinkoHyo": (lambda: QI.IchimokuKinkoHyo(9, 26, 17, 52, 26, 26), feed_bar,
                         {"tenkan": "Tenkan", "kijun": "Kijun",
                          "senkou_a": "SenkouA", "senkou_b": "SenkouB"}),
    "AwesomeOscillator": (lambda: QI.AwesomeOscillator(5, 34), feed_bar, {}),
    "WilderSwingIndex": (lambda: QI.WilderSwingIndex(D(3)), feed_bar, {}),
    "WilderAccumulativeSwingIndex": (
        lambda: QI.WilderAccumulativeSwingIndex(D(3)), feed_bar, {}),
    "RogersSatchellVolatility": (lambda: QI.RogersSatchellVolatility(10), feed_bar, {}),
    "RelativeDailyVolume": (lambda: QI.RelativeDailyVolume(5), feed_bar, {}),
    "ChandeKrollStop": (lambda: QI.ChandeKrollStop(5, 2, 10), feed_bar,
                        {"short_stop": "ShortStop", "long_stop": "LongStop"}),
    "SqueezeMomentum": (lambda: QI.SqueezeMomentum(20, 2, 20, 1.5), feed_bar, {}),
    "WaveTrendOscillator": (lambda: QI.WaveTrendOscillator(10, 21), feed_bar, {}),
    "AugenPriceSpike": (lambda: QI.AugenPriceSpike(20), feed_value, {}),
    "HurstExponent": (lambda: QI.HurstExponent(20, 5), feed_value, {}),
    "FractalAdaptiveMovingAverage": (lambda: QI.FractalAdaptiveMovingAverage(16), feed_bar, {}),
    "PivotPointsHighLow": (lambda: QI.PivotPointsHighLow(5, 5), feed_bar, {}),
    "ZigZag": (lambda: QI.ZigZag(D(0.05), 1), feed_bar, {}),
    "DerivativeOscillator": (lambda: QI.DerivativeOscillator(14, 5, 3, 9), feed_value, {}),
    "KnowSureThing": (lambda: QI.KnowSureThing(5, 5, 8, 5, 11, 5, 14, 8, 9), feed_value, {}),
    "RegressionChannel": (lambda: QI.RegressionChannel(10, D(2)), feed_value,
                          {"upper": "UpperChannel", "lower": "LowerChannel"}),
}


def D(x):
    """C# decimal parameters need an explicit conversion; a Python float
    matches no overload."""
    return System.Convert.ToDecimal(float(x))


def val(x):
    """C# decimal does not convert to a Python float directly."""
    try:
        return round(float(System.Convert.ToDouble(x)), 8)
    except Exception:
        try:
            return round(float(str(x)), 8)
        except Exception:
            return None


out, errors = {}, {}
for name, (make, feed, subs) in SPEC.items():
    try:
        ind = feed(make())
        rec = {"value": val(ind.Current.Value), "is_ready": bool(ind.IsReady)}
        for key, attr in subs.items():
            try:
                rec[key] = val(getattr(ind, attr).Current.Value)
            except Exception as e:
                rec[key] = None
        out[name] = rec
    except Exception as e:
        errors[name] = f"{type(e).__name__}: {e}"

print(json.dumps({"bars": [[t.isoformat(), o, h, lo, c, v]
                           for t, o, h, lo, c, v in BARS],
                  "values": out, "errors": errors}, indent=1))
