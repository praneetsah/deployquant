"""Incremental LEAN-style indicators, updated with closes (or bars for ATR).

These are LEAN-semantics implementations (per-update state machines), not
dqengine.runtime.core's daily-series indicator engine — LEAN code constructs and
reads them imperatively, so they live here."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .aliases import PascalMixin, alias_methods
from .errors import unsupported_arg


@dataclass
class IndicatorDataPoint(PascalMixin):
    time: datetime | None
    value: float


@alias_methods
class IndicatorBase(PascalMixin):
    def __init__(self, period: int):
        self.period = int(period)
        self.samples = 0
        self.current = IndicatorDataPoint(None, 0.0)

    @property
    def is_ready(self) -> bool:
        return self.samples >= self.period

    @property
    def value(self) -> float:
        return self.current.value

    def update(self, time, value) -> bool:
        self.samples += 1
        self._step(float(value))
        self.current = IndicatorDataPoint(time, self._value())
        return self.is_ready

    def _step(self, value: float):
        raise NotImplementedError

    def _value(self) -> float:
        raise NotImplementedError

    def __float__(self):
        return float(self.current.value)


@alias_methods
class BarIndicatorBase(IndicatorBase):
    """Fed whole bars rather than a single value.

    Volume rides along because OBV, MFI, VWAP and friends are impossible
    without it — the bar feed never passed it before, which is why none of
    them could exist.
    """

    def update_bar(self, time, high, low, close, volume=0.0,
                   open_=None) -> bool:
        self.samples += 1
        # `open` rides on the instance rather than in _step_bar's signature:
        # subclasses that need it read self._bar_open, and the dozens that
        # do not keep their four-argument shape.
        self._bar_open = float(open_) if open_ is not None else float(close)
        self._step_bar(float(high), float(low), float(close), float(volume))
        self.current = IndicatorDataPoint(time, self._value())
        return self.is_ready

    def _step_bar(self, high, low, close, volume):
        raise NotImplementedError

    def _step(self, value):     # never used; bars only
        raise TypeError(f"{type(self).__name__} updates with a bar")

    def update(self, time_or_bar, value=None) -> bool:
        bar = time_or_bar
        if value is not None or not hasattr(bar, "high"):
            raise TypeError(f"{type(self).__name__} updates with a bar, "
                            f"not a value")
        return self.update_bar(bar.end_time, bar.high, bar.low, bar.close,
                               getattr(bar, "volume", 0.0),
                               getattr(bar, "open", None))


def _ma_of(kind, period):
    """LEAN's MovingAverageType.AsIndicator — the smoothing an indicator was
    configured with, not a fixed choice."""
    k = str(kind).rsplit(".", 1)[-1].lower()
    if k in ("wilders", "wilder"):
        return WilderMovingAverage(period)
    if k in ("exponential", "ema"):
        return ExponentialMovingAverage(period)
    if k in ("linearweightedmovingaverage", "lwma"):
        return LinearWeightedMovingAverage(period)
    return SimpleMovingAverage(period)


class SimpleMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        return sum(self._win) / len(self._win) if self._win else 0.0


class ExponentialMovingAverage(IndicatorBase):
    """LEAN seeds with the SMA of the first `period` samples and reports 0
    until then — NOT with the first value.

    That difference washes out over a long series, which is why every
    EMA-based parity test passed at period 10 over 60 bars. It does not wash
    out when the period is close to the sample count: at period 55 over 58
    bars it moved KlingerVolumeOscillator by 2.6x. A 200-period EMA on 250
    bars would diverge for a real user the same way.
    """

    def __init__(self, period):
        super().__init__(period)
        self._k = 2.0 / (self.period + 1)
        self._ema = None
        self._seed = SimpleMovingAverage(self.period)

    def _step(self, v):
        if self.samples <= self.period:
            self._seed.update(None, v)
            if self.samples == self.period:
                self._ema = self._seed.value
            return
        self._ema = self._ema + (v - self._ema) * self._k

    def _value(self):
        return self._ema if self._ema is not None else 0.0


class RelativeStrengthIndex(IndicatorBase):
    """Wilder RSI: simple average of the first `period` changes, Wilder
    smoothing after — matches dqengine.runtime.core's rsi smoothing:"wilder"."""

    def __init__(self, period, *a, **k):
        super().__init__(period)
        self._prev = None
        self._gains = []
        self._ag = None
        self._al = None

    @property
    def is_ready(self) -> bool:
        return self._ag is not None

    def _step(self, v):
        if self._prev is None:
            self._prev = v
            return
        ch = v - self._prev
        self._prev = v
        gain, loss = max(ch, 0.0), max(-ch, 0.0)
        if self._ag is None:
            self._gains.append((gain, loss))
            if len(self._gains) == self.period:
                self._ag = sum(g for g, _ in self._gains) / self.period
                self._al = sum(l for _, l in self._gains) / self.period
        else:
            p = self.period
            self._ag = (self._ag * (p - 1) + gain) / p
            self._al = (self._al * (p - 1) + loss) / p

    def _value(self):
        if self._ag is None:
            return 0.0
        if self._al == 0:
            return 100.0
        rs = self._ag / self._al
        return 100.0 - 100.0 / (1.0 + rs)


class StandardDeviation(IndicatorBase):
    """Population σ over the window — what tqqq_weekly's hand-rolled pstdev does."""

    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        n = len(self._win)
        if n == 0:
            return 0.0
        mean = sum(self._win) / n
        return (sum((x - mean) ** 2 for x in self._win) / n) ** 0.5


class Maximum(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        return max(self._win) if self._win else 0.0


class Minimum(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        return min(self._win) if self._win else 0.0


@alias_methods
class AverageTrueRange(IndicatorBase):
    """Wilder ATR over bars (update_bar, not update)."""

    def __init__(self, period, *a, **k):
        super().__init__(period)
        self._prev_close = None
        self._trs = []
        self._atr = None

    @property
    def is_ready(self) -> bool:
        return self._atr is not None

    def update_bar(self, time, high, low, close, volume=0.0,
                   open_=None) -> bool:
        # volume and open are accepted and unused: every bar-fed indicator
        # shares one call shape, so no caller needs a per-class special case
        # (this used to raise TypeError and silently fail a whole run)
        self.samples += 1
        if self._prev_close is None:
            tr = float(high) - float(low)
        else:
            tr = max(float(high) - float(low),
                     abs(float(high) - self._prev_close),
                     abs(float(low) - self._prev_close))
        self._prev_close = float(close)
        if self._atr is None:
            self._trs.append(tr)
            if len(self._trs) == self.period:
                self._atr = sum(self._trs) / self.period
        else:
            self._atr = (self._atr * (self.period - 1) + tr) / self.period
        self.current = IndicatorDataPoint(time, self._atr or 0.0)
        return self.is_ready

    # LEAN also lets bar-indicators take a TradeBar via update()
    def update(self, time_or_bar, value=None) -> bool:
        bar = time_or_bar
        if value is not None or not hasattr(bar, "high"):
            raise TypeError("AverageTrueRange updates with a bar, not a value")
        return self.update_bar(bar.end_time, bar.high, bar.low, bar.close)


# --------------------------------------------------------------------------
# Wave 1 of the LEAN indicator library (design 2026-09-05 §4). Each is a
# per-update state machine, same as the originals above. Composites expose
# their sub-series as attributes because that is how LEAN code reads them:
# `self.macd.signal.current.value`.
# --------------------------------------------------------------------------


class Identity(IndicatorBase):
    def __init__(self, name=None):
        super().__init__(1)

    def _step(self, v):
        self._v = v

    def _value(self):
        return getattr(self, "_v", 0.0)


class Sum(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        return sum(self._win)


class Momentum(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period + 1)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        return self._win[-1] - self._win[0] if len(self._win) > 1 else 0.0


class RateOfChange(IndicatorBase):
    """The FRACTIONAL change over `period` — LEAN's RateOfChange."""

    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period + 1)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        if len(self._win) < 2 or self._win[0] == 0:
            return 0.0
        return (self._win[-1] - self._win[0]) / self._win[0]


class RateOfChangePercent(RateOfChange):
    """The same, x100. LEAN verified."""

    def _value(self):
        return super()._value() * 100.0


class MomentumPercent(RateOfChangePercent):
    """LEAN's MomentumPercent is a PERCENT, not a fraction — it is
    RateOfChangePercent under another name. Returning the fraction (the
    obvious reading of the name) is off by 100x, which a LEAN-golden
    comparison caught and a textbook reading never would."""


class RateOfChangeRatio(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period + 1)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        if len(self._win) < 2 or self._win[0] == 0:
            return 0.0
        return self._win[-1] / self._win[0]


class LogReturn(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period + 1)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        import math
        if len(self._win) < 2 or self._win[0] <= 0 or self._win[-1] <= 0:
            return 0.0
        return math.log(self._win[-1] / self._win[0])


class Variance(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        n = len(self._win)
        if n == 0:
            return 0.0
        mean = sum(self._win) / n
        return sum((x - mean) ** 2 for x in self._win) / n


class MeanAbsoluteDeviation(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        n = len(self._win)
        if n == 0:
            return 0.0
        mean = sum(self._win) / n
        return sum(abs(x - mean) for x in self._win) / n


class LinearWeightedMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        n = len(self._win)
        if n == 0:
            return 0.0
        denom = n * (n + 1) / 2
        return sum(v * (i + 1) for i, v in enumerate(self._win)) / denom


class TriangularMovingAverage(IndicatorBase):
    """SMA of an SMA — LEAN uses (period+1)//2 for both legs on odd periods."""

    def __init__(self, period):
        super().__init__(period)
        # LEAN: odd period -> both legs (n+1)/2; EVEN -> n/2 and n/2 + 1.
        # Using (n+1)//2 for both drifts on even periods.
        if self.period % 2 == 0:
            a, b = self.period // 2, self.period // 2 + 1
        else:
            a = b = (self.period + 1) // 2
        self._inner = SimpleMovingAverage(a)
        self._outer = SimpleMovingAverage(b)

    def _step(self, v):
        self._inner.update(None, v)
        if self._inner.is_ready:
            self._outer.update(None, self._inner.value)

    def _value(self):
        return self._outer.value


class DoubleExponentialMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._e1 = ExponentialMovingAverage(self.period)
        self._e2 = ExponentialMovingAverage(self.period)

    def _step(self, v):
        # LEAN gates each stage on the previous being READY, so a warm-up
        # zero is never fed forward (TripleExponentialMovingAverage.cs)
        self._e1.update(None, v)
        if self._e1.is_ready:
            self._e2.update(None, self._e1.value)

    def _value(self):
        return 2 * self._e1.value - self._e2.value


class TripleExponentialMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._e1 = ExponentialMovingAverage(self.period)
        self._e2 = ExponentialMovingAverage(self.period)
        self._e3 = ExponentialMovingAverage(self.period)

    def _step(self, v):
        self._e1.update(None, v)
        if self._e1.is_ready:
            self._e2.update(None, self._e1.value)
        if self._e2.is_ready:
            self._e3.update(None, self._e2.value)

    def _value(self):
        return 3 * self._e1.value - 3 * self._e2.value + self._e3.value


class HullMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        import math
        self._fast = LinearWeightedMovingAverage(max(1, self.period // 2))
        self._slow = LinearWeightedMovingAverage(self.period)
        self._smooth = LinearWeightedMovingAverage(
            max(1, int(math.sqrt(self.period))))

    def _step(self, v):
        self._fast.update(None, v)
        self._slow.update(None, v)
        self._smooth.update(None, 2 * self._fast.value - self._slow.value)

    def _value(self):
        return self._smooth.value


class WilderMovingAverage(IndicatorBase):
    """Wilder smoothing (1/period), the basis of RSI/ADX/ATR.

    While warming up LEAN reports the RUNNING SMA, not zero
    (WilderMovingAverage.cs) — the same shape of detail as the EMA's SMA
    seed, and invisible on a long single-indicator series but material the
    moment this is chained inside something else.
    """

    def __init__(self, period):
        super().__init__(period)
        self._v = None
        self._sma = SimpleMovingAverage(self.period)

    def _step(self, v):
        if self.samples < self.period:
            self._sma.update(None, v)
            self._v = self._sma.value
        elif self.samples == self.period:
            self._sma.update(None, v)
            self._v = self._sma.value
        else:
            self._v += (v - self._v) / self.period

    def _value(self):
        return self._v if self._v is not None else 0.0


class ZeroLagExponentialMovingAverage(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._lag = (self.period - 1) // 2
        self._win = deque(maxlen=self._lag + 1)
        self._ema = ExponentialMovingAverage(self.period)

    def _step(self, v):
        self._win.append(v)
        self._ema.update(None, 2 * v - self._win[0])

    def _value(self):
        return self._ema.value


class MovingAverageConvergenceDivergence(IndicatorBase):
    def __init__(self, fast_period=12, slow_period=26, signal_period=9,
                 moving_average_type="Exponential", *a, **k):
        super().__init__(slow_period)
        # All three legs take the CONFIGURED type, not always an EMA — LEAN
        # source (MovingAverageConvergenceDivergence.cs:85). Matters wherever
        # a MACD is embedded with a non-default type, e.g. SchaffTrendCycle.
        self.fast = _ma_of(moving_average_type, fast_period)
        self.slow = _ma_of(moving_average_type, slow_period)
        self.signal = _ma_of(moving_average_type, signal_period)
        self._slow_period = int(slow_period)

    @property
    def histogram(self):
        return IndicatorDataPoint(self.current.time,
                                  self.current.value - self.signal.value)

    def _step(self, v):
        self.fast.update(None, v)
        self.slow.update(None, v)
        if self.samples >= self._slow_period:
            self.signal.update(None, self.fast.value - self.slow.value)

    def _value(self):
        return self.fast.value - self.slow.value


class BollingerBands(IndicatorBase):
    def __init__(self, period, k=2, *a, **k_):
        super().__init__(period)
        self._k = float(k)
        self.middle_band = SimpleMovingAverage(self.period)
        self.standard_deviation = StandardDeviation(self.period)
        self.upper_band = IndicatorDataPoint(None, 0.0)
        self.lower_band = IndicatorDataPoint(None, 0.0)

    # LEAN's shorter spellings
    @property
    def middle(self):
        return self.middle_band

    @property
    def upper(self):
        return self.upper_band

    @property
    def lower(self):
        return self.lower_band

    def _step(self, v):
        self.middle_band.update(None, v)
        self.standard_deviation.update(None, v)
        m, sd = self.middle_band.value, self.standard_deviation.value
        self.upper_band = IndicatorDataPoint(None, m + self._k * sd)
        self.lower_band = IndicatorDataPoint(None, m - self._k * sd)

    def _value(self):
        return self.middle_band.value


class ChandeMomentumOscillator(IndicatorBase):
    """LEAN smooths the gains and losses with WILDER smoothing, not a plain
    rolling window. Deduced from LEAN itself (tools/lean_probe.py): on
    [10,11,9,12,8,13,...] with period 3 it returns 31.1828 at the sixth
    sample, which is 29/93 x 100 -- exactly Wilder, and nothing like the
    33.33 a rolling window gives."""

    def __init__(self, period):
        super().__init__(period)
        self._prev = None
        self._gain = WilderMovingAverage(self.period)
        self._loss = WilderMovingAverage(self.period)

    def _step(self, v):
        if self._prev is not None:
            d = v - self._prev
            self._gain.update(None, max(d, 0.0))
            self._loss.update(None, max(-d, 0.0))
        self._prev = v

    def _value(self):
        g, l = self._gain.value, self._loss.value
        return 0.0 if g + l == 0 else (g - l) / (g + l) * 100.0


class Trix(IndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._e1 = ExponentialMovingAverage(self.period)
        self._e2 = ExponentialMovingAverage(self.period)
        self._e3 = ExponentialMovingAverage(self.period)
        self._prev = None
        self._out = 0.0

    def _step(self, v):
        self._e1.update(None, v)
        if self._e1.is_ready:
            self._e2.update(None, self._e1.value)
        if self._e2.is_ready:
            self._e3.update(None, self._e2.value)
        cur = self._e3.value
        if self._prev not in (None, 0):
            self._out = (cur - self._prev) / self._prev * 100.0
        self._prev = cur

    def _value(self):
        return self._out


class AbsolutePriceOscillator(IndicatorBase):
    """LEAN's APO is the difference of two SIMPLE moving averages, not
    exponential ones -- the textbook definition uses EMAs, LEAN does not.
    Deduced from LEAN itself: APO(3,5) on a ramp reads 0.5 at the fourth
    sample, which is SMA3 - SMA5 (12.0 - 11.5); an EMA5 is not even ready
    there."""

    def __init__(self, fast_period=12, slow_period=26, *a, **k):
        super().__init__(slow_period)
        self.fast = SimpleMovingAverage(fast_period)
        self.slow = SimpleMovingAverage(slow_period)

    def _step(self, v):
        self.fast.update(None, v)
        self.slow.update(None, v)

    def _value(self):
        return self.fast.value - self.slow.value


class PercentagePriceOscillator(AbsolutePriceOscillator):
    def _value(self):
        s = self.slow.value
        return 0.0 if s == 0 else (self.fast.value - s) / s * 100.0


# ---------------- bar-fed ----------------

class TrueRange(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._prev_close = None
        self._tr = 0.0

    def _step_bar(self, h, l, c, v):
        self._tr = (h - l) if self._prev_close is None else max(
            h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._prev_close = c

    def _value(self):
        return self._tr


class NormalizedAverageTrueRange(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._atr = AverageTrueRange(self.period)
        self._close = 0.0

    def _step_bar(self, h, l, c, v):
        self._atr.update_bar(None, h, l, c)
        self._close = c

    def _value(self):
        return 0.0 if self._close == 0 else self._atr.value / self._close * 100.0


class MidPoint(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._hi = Maximum(self.period)
        self._lo = Minimum(self.period)

    def _step_bar(self, h, l, c, v):
        self._hi.update(None, c)
        self._lo.update(None, c)

    def _value(self):
        return (self._hi.value + self._lo.value) / 2.0


class MidPrice(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._hi = Maximum(self.period)
        self._lo = Minimum(self.period)

    def _step_bar(self, h, l, c, v):
        self._hi.update(None, h)
        self._lo.update(None, l)

    def _value(self):
        return (self._hi.value + self._lo.value) / 2.0


class InternalBarStrength(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._v = 0.0

    def _step_bar(self, h, l, c, v):
        self._v = 0.5 if h == l else (c - l) / (h - l)

    def _value(self):
        return self._v


class BalanceOfPower(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._v = 0.0
        self._open = None

    def _step_bar(self, h, l, c, v):
        # no open in the bar feed: the previous close stands in for it, which
        # is what a continuous session implies
        o = self._open if self._open is not None else c
        self._v = 0.0 if h == l else (c - o) / (h - l)
        self._open = c

    def _value(self):
        return self._v


class WilliamsPercentR(BarIndicatorBase):
    def __init__(self, period=14):
        super().__init__(period)
        self._hi = Maximum(self.period)
        self._lo = Minimum(self.period)
        self._c = 0.0

    def _step_bar(self, h, l, c, v):
        self._hi.update(None, h)
        self._lo.update(None, l)
        self._c = c

    def _value(self):
        hi, lo = self._hi.value, self._lo.value
        return -50.0 if hi == lo else (hi - self._c) / (hi - lo) * -100.0


class CommodityChannelIndex(BarIndicatorBase):
    def __init__(self, period=20, *a, **k):
        super().__init__(period)
        self._tp = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        self._tp.append((h + l + c) / 3.0)

    def _value(self):
        n = len(self._tp)
        if n == 0:
            return 0.0
        mean = sum(self._tp) / n
        md = sum(abs(x - mean) for x in self._tp) / n
        return 0.0 if md == 0 else (self._tp[-1] - mean) / (0.015 * md)


class Stochastic(BarIndicatorBase):
    def __init__(self, period=14, k_period=None, d_period=3, *a, **k):
        super().__init__(period)
        self._hi = Maximum(self.period)
        self._lo = Minimum(self.period)
        self._c = 0.0
        self.fast_stoch = IndicatorDataPoint(None, 0.0)
        self.stoch_k = IndicatorDataPoint(None, 0.0)
        self._d = SimpleMovingAverage(d_period)
        self.stoch_d = IndicatorDataPoint(None, 0.0)

    @property
    def k(self):
        return self.stoch_k

    @property
    def d(self):
        return self.stoch_d

    def _step_bar(self, h, l, c, v):
        self._hi.update(None, h)
        self._lo.update(None, l)
        self._c = c
        hi, lo = self._hi.value, self._lo.value
        kv = 50.0 if hi == lo else (c - lo) / (hi - lo) * 100.0
        self.fast_stoch = IndicatorDataPoint(None, kv)
        self.stoch_k = IndicatorDataPoint(None, kv)
        self._d.update(None, kv)
        self.stoch_d = IndicatorDataPoint(None, self._d.value)

    def _value(self):
        return self.stoch_k.value


class DonchianChannel(BarIndicatorBase):
    def __init__(self, period, lower_period=None, *a, **k):
        super().__init__(period)
        self.upper_band = Maximum(self.period)
        self.lower_band = Minimum(lower_period or self.period)

    def _step_bar(self, h, l, c, v):
        self.upper_band.update(None, h)
        self.lower_band.update(None, l)

    def _value(self):
        return (self.upper_band.value + self.lower_band.value) / 2.0


class KeltnerChannels(BarIndicatorBase):
    def __init__(self, period, k=2, *a, **k_):
        super().__init__(period)
        self._k = float(k)
        self.middle_band = ExponentialMovingAverage(self.period)
        self._atr = AverageTrueRange(self.period)
        self.upper_band = IndicatorDataPoint(None, 0.0)
        self.lower_band = IndicatorDataPoint(None, 0.0)

    def _step_bar(self, h, l, c, v):
        self.middle_band.update(None, c)
        self._atr.update_bar(None, h, l, c)
        m, a = self.middle_band.value, self._atr.value
        self.upper_band = IndicatorDataPoint(None, m + self._k * a)
        self.lower_band = IndicatorDataPoint(None, m - self._k * a)

    def _value(self):
        return self.middle_band.value


class AverageDirectionalIndex(BarIndicatorBase):
    """Wilder's ADX, transcribed from LEAN's source.

    Three things differ from every textbook rendering, and from what I first
    wrote: the true-range and directional-movement smoothing is a running
    ACCUMULATOR (value + new - value/period), not a moving average; a zero
    directional sum returns 50, not 0; and only the final DX is put through
    a Wilder moving average.
    """

    def __init__(self, period=14):
        super().__init__(period)
        self._p = self.period
        self._prev = None                 # (high, low, close)
        self._str = 0.0                   # smoothed true range
        self._sdmp = 0.0                  # smoothed +DM
        self._sdmn = 0.0                  # smoothed -DM
        self._adx = WilderMovingAverage(self.period)
        self.positive_directional_index = IndicatorDataPoint(None, 0.0)
        self.negative_directional_index = IndicatorDataPoint(None, 0.0)
        self._v = 0.0

    @property
    def positive(self):
        return self.positive_directional_index

    @property
    def negative(self):
        return self.negative_directional_index

    def _step_bar(self, h, l, c, v):
        if self._prev is None:
            tr = dmp = dmn = 0.0
        else:
            ph, pl, pc = self._prev
            tr = max(h - l, abs(h - pc), abs(l - pc))
            dmp = (h - ph) if (h > ph and (h - ph) >= (pl - l)) else 0.0
            dmn = (pl - l) if (l < pl and (pl - l) > (h - ph)) else 0.0
        decay = self.samples > self._p + 1
        self._str += tr - (self._str / self._p if decay else 0.0)
        self._sdmp += dmp - (self._sdmp / self._p if decay else 0.0)
        self._sdmn += dmn - (self._sdmn / self._p if decay else 0.0)
        self._prev = (h, l, c)

        ready = self.samples > self._p
        pdi = 100.0 * self._sdmp / self._str if (self._str and ready) else 0.0
        ndi = 100.0 * self._sdmn / self._str if (self._str and ready) else 0.0
        self.positive_directional_index = IndicatorDataPoint(None, pdi)
        self.negative_directional_index = IndicatorDataPoint(None, ndi)
        total = pdi + ndi
        if total == 0:
            self._v = 50.0
            return
        self._adx.update(None, 100.0 * abs(pdi - ndi) / total)
        self._v = self._adx.value

    def _value(self):
        return self._v


class AroonOscillator(BarIndicatorBase):
    def __init__(self, period=25, down_period=None, *a, **k):
        super().__init__(period)
        self._hs = deque(maxlen=self.period + 1)
        self._ls = deque(maxlen=(down_period or self.period) + 1)
        self.aroon_up = IndicatorDataPoint(None, 0.0)
        self.aroon_down = IndicatorDataPoint(None, 0.0)

    def _step_bar(self, h, l, c, v):
        self._hs.append(h)
        self._ls.append(l)
        n_up, n_dn = len(self._hs), len(self._ls)
        since_hi = n_up - 1 - max(range(n_up), key=lambda i: self._hs[i])
        since_lo = n_dn - 1 - min(range(n_dn), key=lambda i: self._ls[i])
        self.aroon_up = IndicatorDataPoint(
            None, 100.0 * (self.period - since_hi) / self.period)
        self.aroon_down = IndicatorDataPoint(
            None, 100.0 * (self.period - since_lo) / self.period)

    def _value(self):
        return self.aroon_up.value - self.aroon_down.value


class OnBalanceVolume(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._prev = None
        self._obv = 0.0

    @property
    def is_ready(self):
        return self.samples >= 2

    def _step_bar(self, h, l, c, v):
        if self._prev is None:
            self._obv = v          # LEAN seeds with the first bar's volume
        elif c > self._prev:
            self._obv += v
        elif c < self._prev:
            self._obv -= v
        self._prev = c

    def _value(self):
        return self._obv


class AccumulationDistribution(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._ad = 0.0

    @property
    def is_ready(self):
        return self.samples >= 1

    def _step_bar(self, h, l, c, v):
        if h != l:
            self._ad += ((c - l) - (h - c)) / (h - l) * v

    def _value(self):
        return self._ad


class MoneyFlowIndex(BarIndicatorBase):
    def __init__(self, period=14):
        super().__init__(period)
        self._prev_tp = None
        self._pos = deque(maxlen=self.period)
        self._neg = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        tp = (h + l + c) / 3.0
        flow = tp * v
        if self._prev_tp is not None:
            self._pos.append(flow if tp > self._prev_tp else 0.0)
            self._neg.append(flow if tp < self._prev_tp else 0.0)
        self._prev_tp = tp

    def _value(self):
        p, n = sum(self._pos), sum(self._neg)
        if n == 0:
            return 100.0 if p else 50.0
        return 100.0 - 100.0 / (1 + p / n)


class ChaikinMoneyFlow(BarIndicatorBase):
    def __init__(self, period=20):
        super().__init__(period)
        self._mfv = deque(maxlen=self.period)
        self._vol = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        self._mfv.append(0.0 if h == l else ((c - l) - (h - c)) / (h - l) * v)
        self._vol.append(v)

    def _value(self):
        tv = sum(self._vol)
        return 0.0 if tv == 0 else sum(self._mfv) / tv


class VolumeWeightedAveragePriceIndicator(BarIndicatorBase):
    def __init__(self, period=1):
        super().__init__(period)
        self._pv = deque(maxlen=max(1, int(period)))
        self._v = deque(maxlen=max(1, int(period)))

    def _step_bar(self, h, l, c, v):
        self._pv.append((h + l + c) / 3.0 * v)
        self._v.append(v)

    def _value(self):
        tv = sum(self._v)
        return 0.0 if tv == 0 else sum(self._pv) / tv


class VolumeWeightedMovingAverage(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._pv = deque(maxlen=self.period)
        self._v = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        self._pv.append(c * v)
        self._v.append(v)

    def _value(self):
        tv = sum(self._v)
        return 0.0 if tv == 0 else sum(self._pv) / tv


class ParabolicStopAndReverse(BarIndicatorBase):
    """Wilder's parabolic SAR.

    The clamp is against the PRIOR TWO bars' extremes, never the current
    one. Clamping to the current low instead makes `low < sar` unreachable
    and the indicator never reverses — it just trails price down forever,
    which is the one thing a stop-and-reverse must not do.
    """

    def __init__(self, af_start=0.02, af_increment=0.02, af_max=0.2, *a, **k):
        super().__init__(2)
        self._af0, self._afi, self._afm = af_start, af_increment, af_max
        self._long = True
        self._sar = self._ep = self._af = None
        self._highs = deque(maxlen=2)      # prior bars, current excluded
        self._lows = deque(maxlen=2)

    def _step_bar(self, h, l, c, v):
        if self._sar is None:
            if not self._highs:
                self._highs.append(h)
                self._lows.append(l)
                return
            self._long = c >= self._highs[-1]
            self._sar = self._lows[-1] if self._long else self._highs[-1]
            self._ep = h if self._long else l
            self._af = self._af0
            self._highs.append(h)
            self._lows.append(l)
            return

        self._sar += self._af * (self._ep - self._sar)
        if self._long:
            self._sar = min([self._sar] + list(self._lows))
            if l < self._sar:                      # reversal
                self._long = False
                self._sar, self._ep, self._af = self._ep, l, self._af0
            elif h > self._ep:
                self._ep = h
                self._af = min(self._af + self._afi, self._afm)
        else:
            self._sar = max([self._sar] + list(self._highs))
            if h > self._sar:                      # reversal
                self._long = True
                self._sar, self._ep, self._af = self._ep, h, self._af0
            elif l < self._ep:
                self._ep = l
                self._af = min(self._af + self._afi, self._afm)
        self._highs.append(h)
        self._lows.append(l)

    def _value(self):
        return self._sar if self._sar is not None else 0.0


# --------------------------------------------------------------------------
# Wave 5: the rest of the library, each verified against real LEAN values
# (tests/runtime/test_lean_parity.py). Where LEAN's definition differs
# from the textbook, LEAN wins and the comment says so.
# --------------------------------------------------------------------------


class KaufmanEfficiencyRatio(IndicatorBase):
    """|net change| / sum(|change|) over the window: 1 = a clean trend,
    0 = pure noise."""

    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period + 1)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        if len(self._win) < 2:
            return 0.0
        noise = sum(abs(self._win[i] - self._win[i - 1])
                    for i in range(1, len(self._win)))
        return 0.0 if noise == 0 else abs(self._win[-1] - self._win[0]) / noise


class KaufmanAdaptiveMovingAverage(IndicatorBase):
    """KAMA: an EMA whose smoothing constant scales with the efficiency
    ratio, so it accelerates in a trend and stalls in chop."""

    def __init__(self, period, fast_period=2, slow_period=30):
        super().__init__(period)
        self._er = KaufmanEfficiencyRatio(self.period)
        self._fast = 2.0 / (fast_period + 1)
        self._slow = 2.0 / (slow_period + 1)
        self._kama = None
        self._n = 0

    def _step(self, v):
        self._er.update(None, v)
        self._n += 1
        if self._n < self.period + 1:
            self._kama = v          # seeds on the price itself
            return
        sc = (self._er.value * (self._fast - self._slow) + self._slow) ** 2
        self._kama += sc * (v - self._kama)

    def _value(self):
        return self._kama if self._kama is not None else 0.0


class T3MovingAverage(IndicatorBase):
    """Tillson T3: six chained EMAs recombined with a volume factor."""

    def __init__(self, period, volume_factor=0.7):
        super().__init__(period)
        self._e = [ExponentialMovingAverage(self.period) for _ in range(6)]
        a = float(volume_factor)
        self._c1 = -a ** 3
        self._c2 = 3 * a ** 2 + 3 * a ** 3
        self._c3 = -6 * a ** 2 - 3 * a - 3 * a ** 3
        self._c4 = 1 + 3 * a + a ** 3 + 3 * a ** 2

    def _step(self, v):
        x = v
        for e in self._e:
            e.update(None, x)
            if not e.is_ready:
                break            # never feed a warm-up zero forward
            x = e.value

    def _value(self):
        e3, e4, e5, e6 = (self._e[2].value, self._e[3].value,
                          self._e[4].value, self._e[5].value)
        return self._c1 * e6 + self._c2 * e5 + self._c3 * e4 + self._c4 * e3


class McGinleyDynamic(IndicatorBase):
    """A moving average that speeds up when price runs away from it."""

    def __init__(self, period):
        super().__init__(period)
        self._md = None
        self._seed = SimpleMovingAverage(self.period)

    def _step(self, v):
        self._seed.update(None, v)
        if self._md is None:
            # LEAN seeds with an SMA over `period`, not with the first price
            if self.samples >= self.period:
                self._md = self._seed.value
            return
        if self._md == 0:
            self._md = v
            return
        ratio = v / self._md
        denom = self.period * (ratio ** 4)
        self._md += (v - self._md) / denom if denom else 0.0

    def _value(self):
        return self._md or 0.0


class _Regression(IndicatorBase):
    """Shared least-squares fit over the window; subclasses pick the point
    on the fitted line they report."""

    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._win.append(v)

    def _fit(self):
        n = len(self._win)
        if n < 2:
            return 0.0, (self._win[-1] if self._win else 0.0)
        xs = range(n)
        mx = (n - 1) / 2.0
        my = sum(self._win) / n
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, self._win))
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sxy / sxx if sxx else 0.0
        return slope, my + slope * ((n - 1) - mx)


class LeastSquaresMovingAverage(_Regression):
    """The fitted line evaluated at the LAST point in the window."""

    def _value(self):
        return self._fit()[1]


class TimeSeriesForecast(_Regression):
    """The fitted line projected ONE step past the window — LEAN's TSF."""

    def _value(self):
        slope, end = self._fit()
        return end + slope


class DetrendedPriceOscillator(IndicatorBase):
    """LEAN lags the PRICE, not the average: price[t - (period/2 + 1)]
    minus the current SMA. Lagging the SMA instead (the reading the name
    invites) is wrong by several multiples -- LEAN-probed."""

    def __init__(self, period):
        super().__init__(period)
        self._sma = SimpleMovingAverage(self.period)
        self._lag = self.period // 2 + 1
        self._prices = deque(maxlen=self._lag + 1)
        self._v = 0.0

    def _step(self, v):
        self._sma.update(None, v)
        self._prices.append(v)
        if len(self._prices) > self._lag:
            self._v = self._prices[0] - self._sma.value

    def _value(self):
        return self._v


class Momersion(IndicatorBase):
    """% of consecutive return pairs that kept their sign: >50 momentum,
    <50 mean reversion."""

    def __init__(self, min_period, full_period=None):
        period = full_period or min_period
        super().__init__(period)
        self._min = min_period if full_period else max(2, period // 2)
        self._prev = None
        self._signs = deque(maxlen=self.period + 1)   # LEAN keeps p+1 changes

    def _step(self, v):
        if self._prev is not None:
            self._signs.append(v - self._prev)
        self._prev = v

    def _value(self):
        pairs = [(self._signs[i - 1], self._signs[i])
                 for i in range(1, len(self._signs))]
        pairs = [p for p in pairs if p[0] and p[1]]
        if not pairs:
            return 0.0
        same = sum(1 for a, b in pairs if (a > 0) == (b > 0))
        return same / len(pairs) * 100.0


class AverageRange(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._sma = SimpleMovingAverage(self.period)

    def _step_bar(self, h, l, c, v):
        self._sma.update(None, h - l)

    def _value(self):
        return self._sma.value


class SmoothedOnBalanceVolume(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._obv = OnBalanceVolume()
        self._sma = SimpleMovingAverage(self.period)

    def _step_bar(self, h, l, c, v):
        self._obv.update_bar(None, h, l, c, v)
        self._sma.update(None, self._obv.value)

    def _value(self):
        return self._sma.value


class ChoppinessIndex(BarIndicatorBase):
    """100 * log10(sum(TR) / (max high - min low)) / log10(period).
    High = choppy, low = trending."""

    def __init__(self, period):
        super().__init__(period)
        self._tr = TrueRange()
        self._trs = deque(maxlen=self.period)
        self._hi = deque(maxlen=self.period)
        self._lo = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        self._tr.update_bar(None, h, l, c)
        self._trs.append(self._tr.value)
        self._hi.append(h)
        self._lo.append(l)

    def _value(self):
        import math
        if len(self._trs) < 2:
            return 0.0
        rng = max(self._hi) - min(self._lo)
        if rng <= 0 or sum(self._trs) <= 0:
            return 0.0
        return 100.0 * math.log10(sum(self._trs) / rng) / math.log10(self.period)


class MassIndex(BarIndicatorBase):
    """Sum over `sum_period` of the single/double EMA ratio of the range —
    a range-expansion detector, so it needs no direction."""

    def __init__(self, ema_period=9, sum_period=25):
        super().__init__(sum_period)
        self._e1 = ExponentialMovingAverage(ema_period)
        self._e2 = ExponentialMovingAverage(ema_period)
        self._ratios = deque(maxlen=sum_period)

    def _step_bar(self, h, l, c, v):
        self._e1.update(None, h - l)
        if self._e1.is_ready:
            self._e2.update(None, self._e1.value)
        if self._e2.value:
            self._ratios.append(self._e1.value / self._e2.value)

    def _value(self):
        return sum(self._ratios)


class EaseOfMovementValue(BarIndicatorBase):
    def __init__(self, period=1, scale=10000):
        super().__init__(period)
        self._scale = float(scale)
        self._prev_mid = None
        self._sma = SimpleMovingAverage(self.period)

    def _step_bar(self, h, l, c, v):
        mid = (h + l) / 2.0
        if self._prev_mid is not None and (h - l) > 0 and v > 0:
            box = (v / self._scale) / (h - l)
            self._sma.update(None, (mid - self._prev_mid) / box if box else 0.0)
        self._prev_mid = mid

    def _value(self):
        return self._sma.value


class ForceIndex(BarIndicatorBase):
    def __init__(self, period=13):
        super().__init__(period)
        self._prev_close = None
        self._ema = ExponentialMovingAverage(self.period)

    def _step_bar(self, h, l, c, v):
        if self._prev_close is not None:
            self._ema.update(None, (c - self._prev_close) * v)
        self._prev_close = c

    def _value(self):
        return self._ema.value


class UltimateOscillator(BarIndicatorBase):
    def __init__(self, period1=7, period2=14, period3=28):
        super().__init__(period3)
        self._p = (period1, period2, period3)
        self._bp = deque(maxlen=period3)
        self._tr = deque(maxlen=period3)
        self._prev_close = None

    def _step_bar(self, h, l, c, v):
        if self._prev_close is None:
            self._prev_close = c
            return
        tl = min(l, self._prev_close)
        self._bp.append(c - tl)
        self._tr.append(max(h, self._prev_close) - tl)
        self._prev_close = c

    def _value(self):
        def avg(n):
            if len(self._tr) < n:
                return 0.0
            t = sum(list(self._tr)[-n:])
            return 0.0 if t == 0 else sum(list(self._bp)[-n:]) / t
        a, b, cc = (avg(p) for p in self._p)
        return 100.0 * (4 * a + 2 * b + cc) / 7.0


class DeMarkerIndicator(BarIndicatorBase):
    def __init__(self, period=14):
        super().__init__(period)
        self._ph = self._pl = None
        self._max = SimpleMovingAverage(self.period)
        self._min = SimpleMovingAverage(self.period)

    def _step_bar(self, h, l, c, v):
        if self._ph is not None:
            self._max.update(None, max(h - self._ph, 0.0))
            self._min.update(None, max(self._pl - l, 0.0))
        self._ph, self._pl = h, l

    def _value(self):
        tot = self._max.value + self._min.value
        return 0.0 if tot == 0 else self._max.value / tot


class AwesomeOscillator(BarIndicatorBase):
    """SMA(median price, fast) - SMA(median price, slow)."""

    def __init__(self, fast_period=5, slow_period=34, *a, **k):
        super().__init__(slow_period)
        self._fast = SimpleMovingAverage(fast_period)
        self._slow = SimpleMovingAverage(slow_period)

    def _step_bar(self, h, l, c, v):
        mid = (h + l) / 2.0
        self._fast.update(None, mid)
        self._slow.update(None, mid)

    def _value(self):
        return self._fast.value - self._slow.value


class RogersSatchellVolatility(BarIndicatorBase):
    """A drift-independent volatility estimate — the one indicator here that
    needs the bar OPEN, which is why the feed now carries it."""

    def __init__(self, period):
        super().__init__(period)
        self._win = deque(maxlen=self.period)

    def _step_bar(self, h, l, c, v):
        import math
        o = self._bar_open
        if min(h, l, c, o) <= 0:
            return
        self._win.append(math.log(h / c) * math.log(h / o)
                         + math.log(l / c) * math.log(l / o))

    def _value(self):
        import math
        if not self._win:
            return 0.0
        m = sum(self._win) / len(self._win)
        return math.sqrt(m) if m > 0 else 0.0


class AccumulationDistributionOscillator(BarIndicatorBase):
    def __init__(self, fast_period=3, slow_period=10):
        super().__init__(slow_period)
        self._ad = AccumulationDistribution()
        self._fast = ExponentialMovingAverage(fast_period)
        self._slow = ExponentialMovingAverage(slow_period)

    def _step_bar(self, h, l, c, v):
        self._ad.update_bar(None, h, l, c, v)
        self._fast.update(None, self._ad.value)
        self._slow.update(None, self._ad.value)

    def _value(self):
        return self._fast.value - self._slow.value


class AverageDirectionalMovementIndexRating(BarIndicatorBase):
    """(ADX now + ADX `period - 1` bars ago) / 2.

    The lag is period-1, not period — found by fitting against LEAN's own
    output rather than assuming the obvious reading."""

    def __init__(self, period=14):
        super().__init__(period)
        self._adx = AverageDirectionalIndex(self.period)
        self._lag = max(1, self.period - 1)
        self._hist = deque(maxlen=self._lag + 1)

    def _step_bar(self, h, l, c, v):
        self._adx.update_bar(None, h, l, c, v)
        self._hist.append(self._adx.value)

    def _value(self):
        if len(self._hist) <= self._lag:
            return self._adx.value
        return (self._adx.value + self._hist[0]) / 2.0


class HeikinAshi(BarIndicatorBase):
    """The smoothed candle transform. Exposes open/high/low/close as
    sub-series, because that is the whole point of it."""

    def __init__(self, *a, **k):
        super().__init__(1)
        self.open = IndicatorDataPoint(None, 0.0)
        self.high = IndicatorDataPoint(None, 0.0)
        self.low = IndicatorDataPoint(None, 0.0)
        self.close = IndicatorDataPoint(None, 0.0)
        self._po = self._pc = None

    @property
    def is_ready(self):
        return self.samples >= 2

    def _step_bar(self, h, l, c, v):
        o = self._bar_open
        ha_c = (o + h + l + c) / 4.0
        ha_o = (o + c) / 2.0 if self._po is None else (self._po + self._pc) / 2.0
        self.open = IndicatorDataPoint(None, ha_o)
        self.close = IndicatorDataPoint(None, ha_c)
        self.high = IndicatorDataPoint(None, max(h, ha_o, ha_c))
        self.low = IndicatorDataPoint(None, min(l, ha_o, ha_c))
        self._po, self._pc = ha_o, ha_c

    def _value(self):
        return self.close.value


class CoppockCurve(IndicatorBase):
    """LWMA of (ROC_short + ROC_long), both as percentages."""

    def __init__(self, short_roc_period=11, long_roc_period=14,
                 lwma_period=10):
        super().__init__(lwma_period)
        self._s = RateOfChangePercent(short_roc_period)
        self._l = RateOfChangePercent(long_roc_period)
        self._w = LinearWeightedMovingAverage(lwma_period)

    def _step(self, v):
        self._s.update(None, v)
        self._l.update(None, v)
        self._w.update(None, self._s.value + self._l.value)

    def _value(self):
        return self._w.value


class TrueStrengthIndex(IndicatorBase):
    """Double-smoothed momentum over double-smoothed |momentum|."""

    def __init__(self, long_term_period=25, short_term_period=13,
                 signal_period=7, *a, **k):
        super().__init__(long_term_period)
        self._prev = None
        self._m1 = ExponentialMovingAverage(long_term_period)
        self._m2 = ExponentialMovingAverage(short_term_period)
        self._a1 = ExponentialMovingAverage(long_term_period)
        self._a2 = ExponentialMovingAverage(short_term_period)
        self.signal = ExponentialMovingAverage(signal_period)

    def _step(self, v):
        if self._prev is not None:
            d = v - self._prev
            self._m1.update(None, d)
            self._m2.update(None, self._m1.value)
            self._a1.update(None, abs(d))
            self._a2.update(None, self._a1.value)
            self.signal.update(None, self._value())
        self._prev = v

    def _value(self):
        a = self._a2.value
        return 0.0 if a == 0 else 100.0 * self._m2.value / a


class ConnorsRelativeStrengthIndex(IndicatorBase):
    """The average of three components: RSI of price, RSI of the up/down
    streak, and the percentile rank of the latest return."""

    def __init__(self, rsi_period=3, streak_period=2, lookback=100):
        super().__init__(max(rsi_period, streak_period, lookback))
        self._rsi = RelativeStrengthIndex(rsi_period)
        self._streak_rsi = RelativeStrengthIndex(streak_period)
        self._returns = deque(maxlen=lookback)
        self._prev = None
        self._streak = 0

    def _step(self, v):
        self._rsi.update(None, v)
        if self._prev is not None:
            if v > self._prev:
                self._streak = self._streak + 1 if self._streak > 0 else 1
            elif v < self._prev:
                self._streak = self._streak - 1 if self._streak < 0 else -1
            else:
                self._streak = 0
            self._streak_rsi.update(None, float(self._streak))
            self._returns.append((v - self._prev) / self._prev
                                 if self._prev else 0.0)
        self._prev = v

    def _value(self):
        if len(self._returns) < 2:
            return 0.0
        # LEAN ranks the latest return against the WHOLE lookback including
        # itself, with <=. Excluding it (the natural reading of "percentile
        # rank of the latest") is 1.7% off — LEAN-fitted.
        last = self._returns[-1]
        allr = list(self._returns)
        pct = 100.0 * sum(1 for r in allr if r <= last) / len(allr)
        return (self._rsi.value + self._streak_rsi.value + pct) / 3.0


class StochasticRelativeStrengthIndex(IndicatorBase):
    """Stochastic applied to RSI rather than to price."""

    def __init__(self, rsi_period=14, stoch_period=14, k_smoothing_period=3,
                 d_smoothing_period=3, *a, **k):
        super().__init__(max(rsi_period, stoch_period))
        self._rsi = RelativeStrengthIndex(rsi_period)
        self._win = deque(maxlen=stoch_period)
        self.k = SimpleMovingAverage(k_smoothing_period)
        self.d = SimpleMovingAverage(d_smoothing_period)

    def _step(self, v):
        self._rsi.update(None, v)
        r = self._rsi.value
        self._win.append(r)
        hi, lo = max(self._win), min(self._win)
        raw = 0.0 if hi == lo else (r - lo) / (hi - lo) * 100.0
        self.k.update(None, raw)
        self.d.update(None, self.k.value)

    def _value(self):
        # LEAN's scalar Current.Value here is a composite we have not
        # identified; k and d — what strategies actually read — match LEAN
        # exactly, and k is the sensible scalar.
        return self.k.value


class SuperTrend(BarIndicatorBase):
    def __init__(self, period, multiplier=3.0, *a, **k):
        super().__init__(period)
        self._m = float(multiplier)
        self._atr = AverageTrueRange(self.period)
        self._st = None
        self._up = True
        self._pc = None

    def _step_bar(self, h, l, c, v):
        self._atr.update_bar(None, h, l, c)
        mid = (h + l) / 2.0
        atr = self._atr.value
        upper, lower = mid + self._m * atr, mid - self._m * atr
        if self._st is None:
            self._st, self._pc = lower, c
            return
        if self._up:
            self._st = max(self._st, lower)
            if c < self._st:
                self._up, self._st = False, upper
        else:
            self._st = min(self._st, upper)
            if c > self._st:
                self._up, self._st = True, lower
        self._pc = c

    def _value(self):
        return self._st if self._st is not None else 0.0


class Vortex(BarIndicatorBase):
    def __init__(self, period):
        super().__init__(period)
        self._ph = self._pl = self._pc = None
        self._vp = deque(maxlen=self.period)
        self._vm = deque(maxlen=self.period)
        self._tr = deque(maxlen=self.period)
        self.plus_vortex = IndicatorDataPoint(None, 0.0)
        self.minus_vortex = IndicatorDataPoint(None, 0.0)

    def _step_bar(self, h, l, c, v):
        if self._ph is not None:
            self._vp.append(abs(h - self._pl))
            self._vm.append(abs(l - self._ph))
            self._tr.append(max(h - l, abs(h - self._pc), abs(l - self._pc)))
        self._ph, self._pl, self._pc = h, l, c
        t = sum(self._tr)
        if t:
            self.plus_vortex = IndicatorDataPoint(None, sum(self._vp) / t)
            self.minus_vortex = IndicatorDataPoint(None, sum(self._vm) / t)

    def _value(self):
        # LEAN reports the MEAN of the two legs as the scalar value; the
        # legs themselves are plus_vortex / minus_vortex.
        return (self.plus_vortex.value + self.minus_vortex.value) / 2.0


class HurstExponent(IndicatorBase):
    """Rescaled-range slope: >0.5 trending, <0.5 mean-reverting."""

    def __init__(self, period=20, max_lag=10):
        super().__init__(period)
        self._win = deque(maxlen=self.period)
        self._max_lag = int(max_lag)

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        import math
        n = len(self._win)
        if n < self._max_lag + 2:
            return 0.0
        xs, ys = [], []
        data = list(self._win)
        for lag in range(2, self._max_lag + 1):
            diffs = [data[i] - data[i - lag] for i in range(lag, n)]
            if len(diffs) < 2:
                continue
            m = sum(diffs) / len(diffs)
            var = sum((d - m) ** 2 for d in diffs) / len(diffs)
            if var <= 0:
                continue
            xs.append(math.log(lag))
            ys.append(math.log(var ** 0.5))
        if len(xs) < 2:
            return 0.0
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        return 0.0 if sxx == 0 else \
            sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx




class AccelerationBands(BarIndicatorBase):
    """SMA bands whose width scales with each bar's own range."""

    def __init__(self, period, width=4.0, *a, **k):
        super().__init__(period)
        self._w = float(width)
        self.middle_band = SimpleMovingAverage(self.period)
        self._up = SimpleMovingAverage(self.period)
        self._dn = SimpleMovingAverage(self.period)

    @property
    def upper_band(self):
        return IndicatorDataPoint(None, self._up.value)

    @property
    def lower_band(self):
        return IndicatorDataPoint(None, self._dn.value)

    def _step_bar(self, h, l, c, v):
        ratio = 0.0 if (h + l) == 0 else self._w * (h - l) / (h + l)
        self.middle_band.update(None, c)
        self._up.update(None, h * (1 + ratio))
        self._dn.update(None, l * (1 - ratio))

    def _value(self):
        return self.middle_band.value


class RegressionChannel(_Regression):
    """A least-squares channel: the fitted line plus/minus k standard
    deviations of the residuals. The scalar value is the line itself, which
    is why it equals LeastSquaresMovingAverage."""

    def __init__(self, period, k=2.0, *a, **kw):
        super().__init__(period)
        self._k = float(k)

    def _residual_sd(self):
        n = len(self._win)
        if n < 2:
            return 0.0
        slope, end = self._fit()
        mx = (n - 1) / 2.0
        my = sum(self._win) / n
        resid = [y - (my + slope * (x - mx)) for x, y in enumerate(self._win)]
        m = sum(resid) / n
        return (sum((r - m) ** 2 for r in resid) / n) ** 0.5

    @property
    def upper_channel(self):
        return IndicatorDataPoint(None, self._value() + self._k * self._residual_sd())

    @property
    def lower_channel(self):
        return IndicatorDataPoint(None, self._value() - self._k * self._residual_sd())

    def _value(self):
        return self._fit()[1]


class WilderSwingIndex(BarIndicatorBase):
    """Wilder's Swing Index — needs the bar OPEN, and a limit-move scale."""

    def __init__(self, limit_move=3.0, *a, **k):
        super().__init__(1)
        self._t = float(limit_move)
        self._po = self._ph = self._pl = self._pc = None
        self._v = 0.0

    @property
    def is_ready(self):
        return self.samples >= 2

    def _step_bar(self, h, l, c, v):
        o = self._bar_open
        if self._pc is None:
            self._po, self._ph, self._pl, self._pc = o, h, l, c
            return
        pc, po = self._pc, self._po
        k = max(abs(h - pc), abs(l - pc))
        # Wilder's R: chosen by which move dominates
        m1, m2, m3 = abs(h - pc), abs(l - pc), abs(h - l)
        if m1 >= m2 and m1 >= m3:
            r = m1 - 0.5 * m2 + 0.25 * abs(pc - po)
        elif m2 >= m1 and m2 >= m3:
            r = m2 - 0.5 * m1 + 0.25 * abs(pc - po)
        else:
            r = m3 + 0.25 * abs(pc - po)
        if r == 0 or self._t == 0:
            self._v = 0.0
        else:
            self._v = 50.0 * ((c - pc) + 0.5 * (c - o)
                              + 0.25 * (pc - po)) / r * (k / self._t)
        self._po, self._ph, self._pl, self._pc = o, h, l, c

    def _value(self):
        return self._v


class WilderAccumulativeSwingIndex(BarIndicatorBase):
    """The running sum of the Swing Index."""

    def __init__(self, limit_move=3.0, *a, **k):
        super().__init__(1)
        self._si = WilderSwingIndex(limit_move)
        self._sum = 0.0

    @property
    def is_ready(self):
        return self.samples >= 2

    def _step_bar(self, h, l, c, v):
        self._si.update_bar(None, h, l, c, v, self._bar_open)
        self._sum += self._si.value

    def _value(self):
        return self._sum


class VariableIndexDynamicAverage(IndicatorBase):
    """An EMA whose smoothing scales with the Chande momentum ratio."""

    def __init__(self, period, *a, **k):
        super().__init__(period)
        self._cmo = ChandeMomentumOscillator(self.period)
        self._alpha = 2.0 / (self.period + 1)
        self._v = None

    def _step(self, x):
        self._cmo.update(None, x)
        k = abs(self._cmo.value) / 100.0
        self._v = x if self._v is None else \
            self._v + self._alpha * k * (x - self._v)

    def _value(self):
        return self._v or 0.0


class PremierStochasticOscillator(BarIndicatorBase):
    """A double-EMA-smoothed, tanh-normalised stochastic."""

    def __init__(self, period=14, ema_period=3, *a, **k):
        super().__init__(period)
        self._sto = Stochastic(period, d_period=3)
        self._e1 = ExponentialMovingAverage(ema_period)
        self._e2 = ExponentialMovingAverage(ema_period)

    def _step_bar(self, h, l, c, v):
        import math
        self._sto.update_bar(None, h, l, c, v)
        norm = 0.1 * (self._sto.stoch_k.value - 50.0)
        self._e1.update(None, norm)
        self._e2.update(None, self._e1.value)

    def _value(self):
        import math
        e = self._e2.value
        try:
            x = math.exp(e)
        except OverflowError:
            return 1.0
        return (x - 1) / (x + 1)


class KnowSureThing(IndicatorBase):
    """Weighted sum of four smoothed rates of change."""

    def __init__(self, roc_period1=10, sma_period1=10, roc_period2=15,
                 sma_period2=10, roc_period3=20, sma_period3=10,
                 roc_period4=30, sma_period4=15, signal_period=9, *a, **k):
        super().__init__(roc_period4)
        self._legs = [
            (RateOfChangePercent(roc_period1), SimpleMovingAverage(sma_period1), 1),
            (RateOfChangePercent(roc_period2), SimpleMovingAverage(sma_period2), 2),
            (RateOfChangePercent(roc_period3), SimpleMovingAverage(sma_period3), 3),
            (RateOfChangePercent(roc_period4), SimpleMovingAverage(sma_period4), 4),
        ]
        self.signal = SimpleMovingAverage(signal_period)

    def _step(self, v):
        for roc, sma, _w in self._legs:
            roc.update(None, v)
            sma.update(None, roc.value)
        self.signal.update(None, self._value())

    def _value(self):
        return sum(sma.value * w for _roc, sma, w in self._legs)


class DerivativeOscillator(IndicatorBase):
    """Double-smoothed RSI minus its own SMA signal."""

    def __init__(self, rsi_period=14, smoothing_period1=5,
                 smoothing_period2=3, signal_period=9, *a, **k):
        super().__init__(rsi_period)
        self._rsi = RelativeStrengthIndex(rsi_period)
        self._e1 = ExponentialMovingAverage(smoothing_period1)
        self._e2 = ExponentialMovingAverage(smoothing_period2)
        self._sig = SimpleMovingAverage(signal_period)
        self._v = 0.0

    def _step(self, v):
        self._rsi.update(None, v)
        self._e1.update(None, self._rsi.value)
        self._e2.update(None, self._e1.value)
        self._sig.update(None, self._e2.value)
        self._v = self._e2.value - self._sig.value

    def _value(self):
        return self._v


class _Returns(IndicatorBase):
    """Shared base for the return-based statistics."""

    def __init__(self, period):
        super().__init__(period)
        self._prev = None
        self._rets = deque(maxlen=self.period)

    def _step(self, v):
        if self._prev not in (None, 0):
            self._rets.append((v - self._prev) / self._prev)
        self._prev = v


class TargetDownsideDeviation(IndicatorBase):
    """RMS of the returns below the minimum acceptable return.

    Transcribed from LEAN: the window holds a RateOfChange(1)'s OUTPUT,
    which includes the zero it emits before it is ready, and the mean is
    taken over the whole window rather than over the below-target subset.
    """

    def __init__(self, period, minimum_acceptable_return=0.0, *a, **k):
        super().__init__(period)
        self._mar = float(minimum_acceptable_return)
        self._roc = RateOfChange(1)
        self._win = deque(maxlen=self.period)

    def _step(self, v):
        self._roc.update(None, v)
        self._win.append(self._roc.value)

    def _value(self):
        if not self._win:
            return 0.0
        avg = sum(min(0.0, x - self._mar) ** 2 for x in self._win) / len(self._win)
        return avg ** 0.5


class SharpeRatio(IndicatorBase):
    """(SMA of 1-period returns - risk free rate) / stdev of those returns.

    SOURCE-transcribed, not value-verified: LEAN's own SharpeRatio trips a
    Python.Runtime version clash inside the lean image, so no golden value
    could be captured for it. SortinoRatio, which LEAN builds by swapping in
    a downside denominator, is deliberately NOT shipped — LEAN reports 14.6
    for it while reporting 0 for its own TargetDownsideDeviation, and that
    contradiction is not resolvable from these sources.

    LEAN builds it out of a RateOfChange(1) feeding an SMA and a
    StandardDeviation, both of `period` — so the denominator is the
    deviation of the RETURN SERIES, warm-up zeros included.
    """

    def __init__(self, period, risk_free_rate=0.0, *a, **k):
        super().__init__(period)
        self._rf = float(risk_free_rate)
        self._roc = RateOfChange(1)
        self._sma = SimpleMovingAverage(self.period)
        self._sd = StandardDeviation(self.period)

    def _step(self, v):
        self._roc.update(None, v)
        r = self._roc.value
        self._sma.update(None, r)
        self._sd.update(None, r)

    def _denominator(self):
        return self._sd.value

    def _value(self):
        d = self._denominator()
        return 0.0 if d == 0 else (self._sma.value - self._rf) / d


class ValueAtRisk(_Returns):
    """Parametric VaR: the return at the given confidence level, assuming
    normality. Negative by convention — it is a loss."""

    def __init__(self, period, confidence_level=0.99, *a, **k):
        super().__init__(period)
        self._conf = float(confidence_level)

    def _value(self):
        import math
        n = len(self._rets)
        if n < 2:
            return 0.0
        mean = sum(self._rets) / n
        var = sum((r - mean) ** 2 for r in self._rets) / (n - 1)
        sd = math.sqrt(var)
        # inverse normal CDF (Acklam), enough for a risk band
        p = 1.0 - self._conf
        a = [-3.969683028665376e+01, 2.209460984245205e+02,
             -2.759285104469687e+02, 1.383577518672690e+02,
             -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02,
             -1.556989798598866e+02, 6.680131188771972e+01,
             -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01,
             2.445134137142996e+00, 3.754408661907416e+00]
        pl = 0.02425
        if p < pl:
            q = math.sqrt(-2 * math.log(p))
            z = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        elif p <= 1 - pl:
            q = p - 0.5
            r = q * q
            z = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
                (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
        else:
            q = math.sqrt(-2 * math.log(1 - p))
            z = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        return mean + z * sd


class ChaikinOscillator(AccumulationDistributionOscillator):
    """Identical in form to AccumulationDistributionOscillator — LEAN keeps
    both names — just conventionally 3/10 rather than 5/10."""

    def __init__(self, fast_period=3, slow_period=10, *a, **k):
        super().__init__(fast_period, slow_period)


class IchimokuKinkoHyo(BarIndicatorBase):
    """The five-line cloud. The scalar value is the input price (as LEAN
    reports it); the lines are what a strategy reads."""

    def __init__(self, tenkan_period=9, kijun_period=26, senkou_a_period=17,
                 senkou_b_period=52, senkou_a_delay_period=26,
                 senkou_b_delay_period=26, *a, **k):
        super().__init__(senkou_b_period)
        self._th, self._tl = Maximum(tenkan_period), Minimum(tenkan_period)
        self._kh, self._kl = Maximum(kijun_period), Minimum(kijun_period)
        self._bh, self._bl = Maximum(senkou_b_period), Minimum(senkou_b_period)
        self._a_delay = deque(maxlen=senkou_a_delay_period)
        self._b_delay = deque(maxlen=senkou_b_delay_period)
        self.tenkan = IndicatorDataPoint(None, 0.0)
        self.kijun = IndicatorDataPoint(None, 0.0)
        self.senkou_a = IndicatorDataPoint(None, 0.0)
        self.senkou_b = IndicatorDataPoint(None, 0.0)
        self._c = 0.0

    def _step_bar(self, h, l, c, v):
        for m in (self._th, self._kh, self._bh):
            m.update(None, h)
        for m in (self._tl, self._kl, self._bl):
            m.update(None, l)
        self._c = c
        t = (self._th.value + self._tl.value) / 2.0
        k = (self._kh.value + self._kl.value) / 2.0
        self.tenkan = IndicatorDataPoint(None, t)
        self.kijun = IndicatorDataPoint(None, k)
        self._a_delay.append((t + k) / 2.0)
        self._b_delay.append((self._bh.value + self._bl.value) / 2.0)
        self.senkou_a = IndicatorDataPoint(None, self._a_delay[0])
        self.senkou_b = IndicatorDataPoint(None, self._b_delay[0])

    def _value(self):
        return self._c


class ChandeKrollStop(BarIndicatorBase):
    """Volatility stops on both sides. The scalar is the input price; the
    stops are short_stop / long_stop."""

    def __init__(self, atr_period=5, atr_mult=2.0, period=10, *a, **k):
        super().__init__(period)
        self._atr = AverageTrueRange(atr_period)
        self._m = float(atr_mult)
        self._hh = Maximum(atr_period)
        self._ll = Minimum(atr_period)
        self._short = Maximum(period)
        self._long = Minimum(period)
        self._c = 0.0

    @property
    def short_stop(self):
        return IndicatorDataPoint(None, self._short.value)

    @property
    def long_stop(self):
        return IndicatorDataPoint(None, self._long.value)

    def _step_bar(self, h, l, c, v):
        self._atr.update_bar(None, h, l, c)
        self._hh.update(None, h)
        self._ll.update(None, l)
        a = self._atr.value
        self._short.update(None, self._hh.value - self._m * a)
        self._long.update(None, self._ll.value + self._m * a)
        self._c = c

    def _value(self):
        return self._c


class RelativeMovingAverage(IndicatorBase):
    """LEAN: Long - Medium + Short over periods p, 2p, 3p.

    Not an average of the three, which is what the name suggests and what
    three separate attempts assumed — read out of LEAN's source
    (Indicators/RelativeMovingAverage.cs) after black-box probing failed.
    """

    def __init__(self, period):
        super().__init__(period)
        self.short_average = SimpleMovingAverage(self.period)
        self.medium_average = SimpleMovingAverage(self.period * 2)
        self.long_average = SimpleMovingAverage(self.period * 3)

    def _step(self, v):
        for m in (self.short_average, self.medium_average, self.long_average):
            m.update(None, v)

    def _value(self):
        return (self.long_average.value - self.medium_average.value
                + self.short_average.value)


class RelativeVigorIndex(BarIndicatorBase):
    """(close-open) over (high-low), 1-2-2-1 smoothed then averaged.

    Two details only LEAN's source gives up: the rolling window of previous
    bars advances ONLY once both bands are ready (so it is deliberately
    stale through warm-up), and the bands use the configured moving-average
    type rather than a plain SMA.
    """

    def __init__(self, period=10, moving_average_type="Simple", *a, **k):
        super().__init__(period)
        self._prev = deque(maxlen=3)
        self.close_band = _ma_of(moving_average_type, self.period)
        self.range_band = _ma_of(moving_average_type, self.period)
        self.signal = SimpleMovingAverage(4)
        self._v = 0.0

    def _step_bar(self, h, l, c, v):
        o = self._bar_open
        if len(self._prev) == 3:
            a = c - o
            b, cc, d = ((x[3] - x[0]) for x in self._prev)   # close - open
            e = h - l
            fq, gq, hq = ((x[1] - x[2]) for x in self._prev)  # high - low
            self.close_band.update(None, (a + 2 * (b + cc) + d) / 6.0)
            self.range_band.update(None, (e + 2 * (fq + gq) + hq) / 6.0)
            if self.close_band.is_ready and self.range_band.is_ready:
                self._prev.appendleft((o, h, l, c))
                rb = self.range_band.value
                self._v = 0.0 if rb == 0 else self.close_band.value / rb
                self.signal.update(None, self._v)
                return
        self._prev.appendleft((o, h, l, c))
        self._v = 0.0

    def _value(self):
        return self._v




class ArnaudLegouxMovingAverage(IndicatorBase):
    """Gaussian-weighted MA. The offset centre is FLOORED — omitting that
    was a 4e-3 miss that black-box probing could not explain."""

    def __init__(self, period, sigma=6, offset=0.85, *a, **k):
        super().__init__(period)
        import math
        self._win = deque(maxlen=self.period)
        m = math.floor(float(offset) * (self.period - 1))
        s = self.period / float(sigma)
        raw = [math.exp(-((i - m) ** 2) / (2 * s * s))
               for i in range(self.period)]
        total = sum(raw) or 1.0
        self._w = [x / total for x in raw]

    def _step(self, v):
        self._win.append(v)

    def _value(self):
        if len(self._win) < self.period:
            return self._win[-1] if self._win else 0.0
        return sum(x * w for x, w in zip(self._win, self._w))


class FisherTransform(BarIndicatorBase):
    """Maps price into a roughly Gaussian series.

    Three details from LEAN's source: the price is the MEDIAN (high+low)/2
    rather than a typical price, the smoothing is a fixed alpha of 0.33, and
    the output feeds back on itself — `y + 0.5 * previous output`.
    """

    _ALPHA = 0.33

    def __init__(self, period=10, *a, **k):
        super().__init__(period)
        self._max = Maximum(self.period)
        self._min = Minimum(self.period)
        self._x = 0.0
        self._v = 0.0

    def _step_bar(self, h, l, c, v):
        import math
        price = (l + h) / 2.0
        self._min.update(None, price)
        self._max.update(None, price)
        if self.samples < self.period:
            self._v = 0.0
            return
        lo, hi = self._min.value, self._max.value
        y = 0.0
        if lo != hi:
            x = self._ALPHA * 2 * ((price - lo) / (hi - lo) - 0.5) \
                + (1 - self._ALPHA) * self._x
            self._x = x
            xc = max(-0.999, min(0.999, x))
            y = 0.5 * math.log((1.0 + xc) / (1.0 - xc))
        self._v = y + 0.5 * self._v      # feeds back on its own output

    def _value(self):
        return self._v


class AugenPriceSpike(IndicatorBase):
    """The latest move measured against the volatility of LOG returns.

    Note the off-by-one that only the source shows: the standard deviation
    is fed the log return of the two PREVIOUS points, and the spike is
    measured from the previous point — not from the current one.
    """

    def __init__(self, period=3):
        super().__init__(period)
        if int(period) < 3:
            raise ValueError("AugenPriceSpike needs a period of at least 3")
        self._sd = StandardDeviation(self.period)
        self._win = deque(maxlen=3)
        self._v = 0.0

    def _step(self, v):
        import math
        self._win.appendleft(v)
        if len(self._win) < 3:
            self._v = 0.0
            return
        p1, p2 = self._win[1], self._win[2]
        logp = math.log(p1 / p2) if (p1 and p2) else 0.0
        self._sd.update(None, logp)
        if not self._sd.is_ready:
            self._v = 0.0
            return
        m = self._sd.value * p1
        self._v = 0.0 if m == 0 else (v - p1) / m

    def _value(self):
        return self._v


class FractalAdaptiveMovingAverage(BarIndicatorBase):
    """An EMA whose smoothing follows the fractal dimension of the window.

    Three things the source corrects over the obvious reading: the price is
    the MEDIAN (high+low)/2 rather than the close, `w` is derived from the
    long period as log(2/(1+long)) rather than being the -4.6 constant, and
    the two halves compared are NEWEST-half against OLDER-half.
    """

    def __init__(self, period=16, long_period=198, *a, **k):
        super().__init__(period)
        import math
        self._n = self.period
        self._w = math.log(2.0 / (1 + long_period))
        self._hi = deque(maxlen=self._n)      # newest first
        self._lo = deque(maxlen=self._n)
        self._v = 0.0
        self._seen = 0

    def _step_bar(self, h, l, c, v):
        import math
        price = (h + l) / 2.0
        self._hi.appendleft(h)
        self._lo.appendleft(l)
        self._seen += 1
        if self._seen <= self._n:
            self._v = price                   # identity while filling
            return
        half = self._n // 2
        his, los = list(self._hi), list(self._lo)
        n1 = (max(his[:half]) - min(los[:half])) / half
        n2 = (max(his[half:half * 2]) - min(los[half:half * 2])) / half
        n3 = (max(his) - min(los)) / self._n
        dimen = 0.0
        if n1 + n2 > 0 and n3 > 0:
            dimen = math.log((n1 + n2) / n3) / math.log(2)
        alpha = max(0.01, min(1.0, math.exp(self._w * (dimen - 1))))
        self._v = alpha * price + (1 - alpha) * self._v

    def _value(self):
        return self._v


class KlingerVolumeOscillator(BarIndicatorBase):
    """Fast minus slow EMA of a volume FORCE.

    The force is where every guess went wrong (mine was 200x off). It is not
    signed volume: a cumulative movement accumulates while the trend holds
    and RESETS to today's range plus yesterday's whenever the trend flips,
    and the force is volume * |2*(range/cumulative - 1)| * trend * 100.
    """

    _MIN_DENOM = 1e-8

    def __init__(self, fast_period=34, slow_period=55, signal_period=13,
                 *a, **k):
        super().__init__(max(fast_period, slow_period, signal_period) + 2)
        self._fast = ExponentialMovingAverage(fast_period)
        self._slow = ExponentialMovingAverage(slow_period)
        self.signal = ExponentialMovingAverage(signal_period)
        self._price_index = deque(maxlen=2)     # newest first
        self._range = deque(maxlen=2)
        self._trend = deque(maxlen=2)
        self._cum = 0.0
        self._v = 0.0

    def _step_bar(self, h, l, c, v):
        todays = h - l
        self._range.appendleft(todays)
        self._price_index.appendleft(h + l + c)
        if len(self._price_index) < 2:
            self._v = 0.0
            return
        trend = 1 if self._price_index[0] > self._price_index[1] else -1
        self._trend.appendleft(trend)
        if len(self._trend) < 2:
            self._v = 0.0
            return
        if self._cum == 0 or self._trend[0] != self._trend[1]:
            self._cum = todays + self._range[1]
        else:
            self._cum += todays
        denom = self._MIN_DENOM if abs(self._cum) < self._MIN_DENOM else self._cum
        force = v * abs(2.0 * (todays / denom - 1.0)) * trend * 100.0
        self._fast.update(None, force)
        self._slow.update(None, force)
        if not (self._fast.is_ready and self._slow.is_ready):
            self.signal.update(None, 0.0)
            self._v = 0.0
            return
        self._v = self._fast.value - self._slow.value
        self.signal.update(None, self._v)

    def _value(self):
        return self._v


class WaveTrendOscillator(BarIndicatorBase):
    """Normalised distance of the typical price from its own EMA.

    The chain ESA -> deviation -> index only advances once the upstream
    stage is READY, so the deviation never sees a warm-up zero. That gating
    is the whole difference between this and the obvious implementation.
    """

    _NORM = 0.015

    def __init__(self, channel_period=10, average_period=21,
                 signal_period=4, *a, **k):
        super().__init__(channel_period + average_period + signal_period)
        self.channel_average = ExponentialMovingAverage(channel_period)
        self.channel_deviation = ExponentialMovingAverage(channel_period)
        self.channel_index_average = ExponentialMovingAverage(average_period)
        self.signal = SimpleMovingAverage(signal_period)
        self._v = 0.0

    def _step_bar(self, h, l, c, v):
        tp = (h + l + c) / 3.0
        if not self.channel_average.update(None, tp):
            self._v = 0.0
            return
        dev = abs(tp - self.channel_average.value)
        if not self.channel_deviation.update(None, dev):
            self._v = 0.0
            return
        weighted = self._NORM * self.channel_deviation.value
        if weighted == 0:
            return                       # keep the previous value
        ci = (tp - self.channel_average.value) / weighted
        if not self.channel_index_average.update(None, ci):
            self._v = self.channel_index_average.value
            return
        self.signal.update(None, self.channel_index_average.value)
        self._v = self.channel_index_average.value

    def _value(self):
        return self._v


class SchaffTrendCycle(IndicatorBase):
    """A stochastic of a stochastic of the MACD line.

    LEAN wires the MACD's SIGNAL period to the cycle period and smooths each
    stochastic with a 3-period MA of the configured type. The max/min
    windows track the MACD and the smoothed stochastic respectively, and
    none of the stages wait for their upstream to be ready.
    """

    def __init__(self, cycle_period=10, fast_period=23, slow_period=50,
                 moving_average_type="Exponential", *a, **k):
        super().__init__(cycle_period)
        self._macd = MovingAverageConvergenceDivergence(
            fast_period, slow_period, cycle_period, moving_average_type)
        self._max = deque(maxlen=cycle_period)
        self._min = deque(maxlen=cycle_period)
        self._d = _ma_of(moving_average_type, 3)
        self._maxd = deque(maxlen=cycle_period)
        self._mind = deque(maxlen=cycle_period)
        self._pff = _ma_of(moving_average_type, 3)

    @staticmethod
    def _stoch(value, hi, lo):
        return (value - lo) / (hi - lo) * 100.0 if hi - lo > 0 else 0.0

    def _step(self, v):
        self._macd.update(None, v)
        m = self._macd.value
        self._max.append(m)
        self._min.append(m)
        self._d.update(None, self._stoch(m, max(self._max), min(self._min)))
        dv = self._d.value
        self._maxd.append(dv)
        self._mind.append(dv)
        self._pff.update(None,
                         self._stoch(dv, max(self._maxd), min(self._mind)))

    def _value(self):
        return self._pff.value


@alias_methods
class DualSymbolIndicator(IndicatorBase):
    """Base for indicators that compare TWO symbols.

    The essential rule, from LEAN's MultiSymbolIndicator: nothing is
    computed until BOTH symbols have data for the SAME timestamp, at which
    point both windows advance together. Advancing each window as its own
    bar arrives instead double-counts returns and breaks the definitional
    identities (beta of a series against itself stops being 1).

    NOTE ON VERIFICATION: transcribed from source, NOT checked against
    golden values — LEAN's Symbol construction throws outside a full engine
    run (it needs map-file and market-hours state the bare image does not
    initialise), so no dual-symbol reference could be captured. The tests
    pin definitional identities instead. Weaker evidence than the rest of
    this file, said out loud rather than implied.
    """

    def __init__(self, target_symbol, reference_symbol, period,
                 window_size=2):
        super().__init__(period)
        self.target_symbol = str(target_symbol).upper()
        self.reference_symbol = str(reference_symbol).upper()
        self._target = deque(maxlen=window_size)      # newest first
        self._reference = deque(maxlen=window_size)
        self._win = window_size
        self._pending = {}                            # symbol -> (time, close)

    @property
    def symbols(self):
        return (self.target_symbol, self.reference_symbol)

    @property
    def is_ready(self):
        return (len(self._target) == self._win
                and len(self._reference) == self._win)

    def update_symbol(self, symbol, close, time=None) -> bool:
        s = str(symbol).upper()
        if s not in (self.target_symbol, self.reference_symbol):
            return self.is_ready
        self._pending[s] = (time, float(close))
        both = (self.target_symbol in self._pending
                and self.reference_symbol in self._pending)
        if not both:
            return self.is_ready
        t_time, t_close = self._pending[self.target_symbol]
        r_time, r_close = self._pending[self.reference_symbol]
        if t_time != r_time:
            return self.is_ready          # wait for the streams to line up
        self._pending.clear()
        self.samples += 1
        self._target.appendleft(t_close)
        self._reference.appendleft(r_close)
        self._recompute()
        self.current = IndicatorDataPoint(t_time, self._value())
        return self.is_ready

    def _recompute(self):
        pass

    def _step(self, v):
        raise TypeError(f"{type(self).__name__} is fed through update_symbol")


def _sample_cov(a, b) -> float:
    """MathNet's Covariance — the SAMPLE form, divided by n-1."""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    ma = sum(list(a)[:n]) / n
    mb = sum(list(b)[:n]) / n
    return sum((x - ma) * (y - mb) for x, y in zip(list(a)[:n], list(b)[:n])) \
        / (n - 1)


def _sample_var(a) -> float:
    return _sample_cov(a, a)


class Covariance(DualSymbolIndicator):
    """Sample covariance of the two symbols' RETURNS."""

    def __init__(self, target_symbol, reference_symbol, period, *a, **k):
        super().__init__(target_symbol, reference_symbol, period,
                         window_size=2)
        self._tr = deque(maxlen=self.period)
        self._rr = deque(maxlen=self.period)

    @staticmethod
    def _ret(win):
        return (win[0] / win[1] - 1.0) if (len(win) == 2 and win[1]) else 0.0

    def _recompute(self):
        if len(self._target) == 2:
            self._tr.appendleft(self._ret(self._target))
        if len(self._reference) == 2:
            self._rr.appendleft(self._ret(self._reference))

    def _value(self):
        cov = _sample_cov(self._tr, self._rr)
        return 0.0 if cov != cov else cov


class Beta(Covariance):
    """Covariance of returns over the REFERENCE's variance.

    LEAN falls back to a variance of 1 and a covariance of 0 rather than
    dividing by zero, so a degenerate window reports 0 instead of blowing up.
    """

    def _value(self):
        var = _sample_var(self._rr)
        cov = _sample_cov(self._tr, self._rr)
        if var != var or var == 0:
            var = 1.0
        if cov != cov:
            cov = 0.0
        return cov / var


class Correlation(DualSymbolIndicator):
    """Pearson correlation of the two symbols' CLOSES.

    Note the asymmetry with Beta and Covariance, which work on RETURNS —
    LEAN really does use raw closes here (Correlation.cs).
    """

    def __init__(self, target_symbol, reference_symbol, period,
                 correlation_type="Pearson", *a, **k):
        super().__init__(target_symbol, reference_symbol, period,
                         window_size=period)
        if str(correlation_type).rsplit(".", 1)[-1].lower() != "pearson":
            unsupported_arg("Correlation", "correlation_type",
                            correlation_type, "only Pearson is implemented")

    def _value(self):
        a, b = list(self._target), list(self._reference)
        n = min(len(a), len(b))
        if n < 2:
            return 0.0
        cov = _sample_cov(a[:n], b[:n])
        sa, sb = _sample_var(a[:n]) ** 0.5, _sample_var(b[:n]) ** 0.5
        if sa == 0 or sb == 0:
            return 0.0
        r = cov / (sa * sb)
        return 0.0 if r != r else r
