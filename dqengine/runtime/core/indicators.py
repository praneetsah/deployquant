"""Incremental indicator library with readiness semantics (spec §5).

Indicators are computed on *session* (daily) closes unless noted, updated once per
day-roll, plus snapshot fields (price.last / prior_close) that read current state.
Series are cached per (symbol, indicator-key) so N strategies share one computation.

Parity notes vs the reference strategy's own python (matched deliberately):
  realized_vol(w): population stdev (pstdev) of the last w close-to-close returns.
  pct_rank(of, lookback): value v appended to trailing history FIRST, then rank test
    v >= sorted(hist)[int(q * (len(hist)-1))] semantics is left to the caller —
    here pct_rank returns the *fraction* rank r in [0,1] such that the caller can
    compare r >= 0.9. To match the script exactly we compute:
       ready when len(hist) >= min_obs (default 60)
       r = fraction of decile threshold — implemented as: v >= quantile_index value
    We expose pct_rank as a boolean-friendly float: the script's test
       v >= hist_sorted[int(0.9*(len(hist)-1))]
    is equivalent to pct_rank >= 0.9 under this index convention, so we return
       (number of hist values <= v at or below the threshold position) — concretely
    we return 1.0 if v >= hist_sorted[int(0.9*(n-1))] else 0.0-style is too coarse;
    instead we return the empirical rank r = idx_of_v / (n-1) computed with the same
    floor-index convention, which reproduces the script's decision boundary at 0.9.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Optional

from .exprs import NOT_READY


class DailySeries:
    """Per-symbol daily state updated on each completed session."""

    def __init__(self):
        self.closes: list[float] = []
        self.returns: list[float] = []      # close-to-close simple returns
        # session extremes for range indicators (ATR); callers that only know
        # the close (fill-forward days, warmup) default them to the close,
        # which degrades true range to |close - prev_close| gracefully
        self.highs: list[float] = []
        self.lows: list[float] = []

    def on_session_close(self, close: float, high: float = None,
                         low: float = None):
        if self.closes:
            prev = self.closes[-1]
            if prev > 0:
                self.returns.append(close / prev - 1.0)
        self.closes.append(close)
        self.highs.append(close if high is None else high)
        self.lows.append(close if low is None else low)


class IndicatorEngine:
    def __init__(self):
        self.daily: dict[str, DailySeries] = {}
        # snapshot state, set by the backtest loop before each evaluation
        self.last_price: dict[str, float] = {}
        self.prior_close: dict[str, float] = {}
        self._cache: dict[tuple, tuple[int, object]] = {}   # key -> (version, value)
        self._version: dict[str, int] = {}                  # symbol -> day counter

    def series(self, symbol: str) -> DailySeries:
        return self.daily.setdefault(symbol, DailySeries())

    def on_session_close(self, symbol: str, close: float, high: float = None,
                         low: float = None):
        self.series(symbol).on_session_close(close, high, low)
        self._version[symbol] = self._version.get(symbol, 0) + 1

    # ---------------- evaluation entry point ----------------

    def value(self, node: dict, symbol: str, params: dict):
        name = node["ind"]
        if name == "price":
            field = node.get("field", "last")
            if field == "last":
                return self.last_price.get(symbol, NOT_READY)
            if field == "close":
                # the last COMPLETED daily close — the honest intraday
                # counterpart to indicators computed on session closes
                v = self.prior_close.get(symbol)
                return v if v is not None else NOT_READY
            raise ValueError(
                f"price({field}) isn't available while the session is "
                "running — use price (the live price), price(close) (the "
                "last completed daily close), or highest(n)/lowest(n) over "
                "completed days")
        if name == "prior_close":
            v = self.prior_close.get(symbol)
            return v if v is not None else NOT_READY

        # Composer-mode: evaluate against closes + today's snapshot price
        # (Composer appends the rebalance-time price before computing).
        # The value moves intraday with last_price, so it is never cached.
        if node.get("include_today"):
            return self._compute(name, node, symbol, params,
                                 s=self._series_with_today(symbol))

        # daily-series indicators are cached per symbol-day
        key = (symbol, name, _freeze(node, params))
        ver = self._version.get(symbol, 0)
        hit = self._cache.get(key)
        if hit is not None and hit[0] == ver:
            return hit[1]
        v = self._compute(name, node, symbol, params)
        self._cache[key] = (ver, v)
        return v

    def _series_with_today(self, symbol: str) -> DailySeries:
        """A copy of the daily series with today's snapshot price appended
        as if the session had closed at last_price. No snapshot -> the plain
        completed-sessions series (Composer's graceful skip)."""
        s = self.series(symbol)
        lp = self.last_price.get(symbol)
        if not lp:
            return s
        t = DailySeries()
        prev = s.closes[-1] if s.closes else None
        t.closes = s.closes + [lp]
        t.returns = s.returns + ([lp / prev - 1.0] if prev else [])
        t.highs = s.highs + [lp]
        t.lows = s.lows + [lp]
        return t

    # ---------------- individual indicators ----------------

    def _compute(self, name: str, node: dict, symbol: str, params: dict,
                 s: DailySeries = None):
        # `s` override: as-of evaluation against a truncated series
        # (_expr_history) — everything else reads the live series
        if s is None:
            s = self.series(symbol)

        def arg(key, default=None):
            v = node.get(key, default)
            if isinstance(v, dict) and "param" in v:
                return params[v["param"]]
            return v

        if name == "sma":
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            return sum(s.closes[-w:]) / w

        if name == "ema":
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            k = 2.0 / (w + 1)
            ema = sum(s.closes[:w]) / w
            for c in s.closes[w:]:
                ema = c * k + ema * (1 - k)
            return ema

        if name == "rsi":
            w = int(arg("window"))
            if arg("smoothing") == "wilder":
                # Wilder RSI on price differences, seeded over the first w
                # diffs then smoothed across the full series — Composer's
                # convention (matches the ComposerWM74 LEAN port's _rsi)
                p = s.closes
                if w < 1 or len(p) < w + 1:
                    return NOT_READY
                gain = loss = 0.0
                for i in range(1, w + 1):
                    d = p[i] - p[i - 1]
                    gain += d if d > 0 else 0.0
                    loss += -d if d < 0 else 0.0
                ag, al = gain / w, loss / w
                for i in range(w + 1, len(p)):
                    d = p[i] - p[i - 1]
                    ag = (ag * (w - 1) + (d if d > 0 else 0.0)) / w
                    al = (al * (w - 1) + (-d if d < 0 else 0.0)) / w
                if al == 0:
                    return 100.0
                return 100.0 - 100.0 / (1.0 + ag / al)
            if len(s.returns) < w:
                return NOT_READY
            gains = [r for r in s.returns[-w:] if r > 0]
            losses = [-r for r in s.returns[-w:] if r < 0]
            avg_g = sum(gains) / w
            avg_l = sum(losses) / w
            if avg_l == 0:
                return 100.0
            rs = avg_g / avg_l
            return 100.0 - 100.0 / (1.0 + rs)

        if name == "realized_vol":
            w = int(arg("window"))
            if len(s.returns) < w:
                return NOT_READY
            window = s.returns[-w:]
            mean = sum(window) / w
            return math.sqrt(sum((r - mean) ** 2 for r in window) / w)   # pstdev

        if name == "pct_rank":
            inner = node["of"]
            lookback = int(arg("lookback"))
            min_obs = int(arg("min_obs", 60))
            hist = self._inner_history(inner, symbol, params, lookback)
            if hist is None or len(hist) < min_obs:
                return NOT_READY
            v = hist[-1]
            srt = sorted(hist)
            # Rank such that (rank >= q) reproduces the reference strategy's gate
            # v >= srt[floor(q*(n-1))] for any q strictly between grid points:
            # script-true <=> q < (idx+1)/(n-1), so return (idx+1)/(n-1).
            n = len(srt)
            if n <= 1:
                return 1.0
            idx = -1
            for i, x in enumerate(srt):
                if v >= x:
                    idx = i
            return min(1.0, (idx + 1) / (n - 1))

        if name == "highest":
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            return max(s.closes[-w:])

        if name == "lowest":
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            return min(s.closes[-w:])

        if name == "cum_return":
            w = int(arg("window"))
            if len(s.closes) < w + 1:
                return NOT_READY
            return s.closes[-1] / s.closes[-w - 1] - 1.0

        if name == "drawdown":
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            window = s.closes[-w:]
            peak = window[0]
            worst = 0.0
            for c in window:
                peak = max(peak, c)
                worst = min(worst, c / peak - 1.0)
            return worst

        if name == "std":
            # population stdev of the last w CLOSES (price units — Bollinger's
            # sigma), matching realized_vol's pstdev convention on returns
            w = int(arg("window"))
            if len(s.closes) < w:
                return NOT_READY
            window = s.closes[-w:]
            mean = sum(window) / w
            return math.sqrt(sum((c - mean) ** 2 for c in window) / w)

        if name == "atr":
            # SMA of true range over w sessions (not Wilder smoothing). On
            # sessions recorded without extremes (fill-forward, close-only
            # warmup) highs/lows equal the close, so TR degrades to
            # |close - prev_close| instead of poisoning the average.
            w = int(arg("window"))
            if len(s.closes) < w + 1:
                return NOT_READY
            n = len(s.closes)
            trs = []
            for i in range(n - w, n):
                pc = s.closes[i - 1]
                trs.append(max(s.highs[i] - s.lows[i],
                               abs(s.highs[i] - pc), abs(s.lows[i] - pc)))
            return sum(trs) / w

        if name in ("series_ema", "series_sma"):
            # smooth a DERIVED series: the expression in `of` evaluated as-of
            # each past session (this is what makes a MACD signal line — an
            # EMA of (ema12 - ema26) — user-composable)
            w = int(arg("window"))
            span = w * 3 + 10       # enough history to seed the average
            hist = self._expr_history(node["of"], symbol, params, span)
            if hist is None or len(hist) < w:
                return NOT_READY
            if name == "series_sma":
                return sum(hist[-w:]) / w
            k = 2.0 / (w + 1)
            ema = sum(hist[:w]) / w
            for v in hist[w:]:
                ema = v * k + ema * (1 - k)
            return ema

        raise ValueError(f"unknown indicator: {name}")

    # ---------------- derived-series history (ema_of / sma_of) ----------------

    def _asof(self, node, symbol: str, params: dict, sl: DailySeries):
        """Evaluate a market-data expression as of a truncated series. Only
        numbers, params, arithmetic, and indicator leaves are allowed — no
        position/cash state (its history isn't tracked), no cross-symbol
        references (sessions don't align across listings)."""
        if isinstance(node, bool):
            raise ValueError("ema_of/sma_of needs a number, not true/false")
        if isinstance(node, (int, float)):
            return float(node)
        if isinstance(node, dict):
            if "param" in node:
                return float(params[node["param"]])
            if "ind" in node:
                if node.get("symbol") not in (None, symbol):
                    raise ValueError(
                        "ema_of/sma_of can't reference another ticker inside "
                        "the smoothed expression")
                name = node["ind"]
                if name in ("series_ema", "series_sma", "pct_rank"):
                    # these compute over the LIVE series and would ignore the
                    # as-of slice — the result would be silently wrong, so
                    # nesting them is refused until they learn to time-travel
                    raise ValueError(
                        "ema_of/sma_of can't smooth an already-smoothed or "
                        "percentile series yet")
                if name == "price":
                    return sl.closes[-1] if sl.closes else NOT_READY
                if name == "prior_close":
                    return sl.closes[-2] if len(sl.closes) >= 2 else NOT_READY
                return self._compute(name, node, symbol, params, s=sl)
            for op in ("add", "sub", "mul", "div"):
                if op in node:
                    vals = [self._asof(x, symbol, params, sl)
                            for x in node[op]]
                    if any(v is NOT_READY for v in vals):
                        return NOT_READY
                    acc = vals[0]
                    for v in vals[1:]:
                        acc = (acc + v if op == "add" else
                               acc - v if op == "sub" else
                               acc * v if op == "mul" else acc / v)
                    return acc
            if "pos" in node or "sleeve" in node:
                raise ValueError(
                    "ema_of/sma_of can only smooth market data "
                    "(prices/indicators), not position or cash state")
        raise ValueError(f"unsupported expression inside ema_of/sma_of: "
                         f"{node!r}")

    def _expr_history(self, inner, symbol: str, params: dict, length: int):
        """The inner expression's value at each of the last `length` session
        closes (today last). Only the trailing run of ready values is kept, so
        smoothing never seeds off NOT_READY warm-up days."""
        s = self.series(symbol)
        n = len(s.closes)
        if n == 0:
            return None
        vals = []
        for upto in range(max(1, n - length + 1), n + 1):
            sl = DailySeries()
            sl.closes = s.closes[:upto]
            sl.returns = s.returns[:max(0, upto - 1)]
            sl.highs = s.highs[:upto]
            sl.lows = s.lows[:upto]
            v = self._asof(inner, symbol, params, sl)
            if v is NOT_READY:
                vals = []
                continue
            vals.append(v)
        return vals or None

    def _inner_history(self, inner_node: dict, symbol: str, params: dict,
                       lookback: int) -> Optional[list[float]]:
        """History of the inner expression's daily values over the trailing
        lookback, today's value last.

        `realized_vol` keeps its own closed form below. It is the tqqq_weekly
        gate's inner and the one the live sleeve trades on, and the closed
        form is 2.4x cheaper than re-slicing the series 252 times per
        evaluation. (Measured 2026-09-10, the two agree bit for bit on the
        same series — so this is a cost decision, not a numerical one, and
        the pinned value in tests/test_engine.py locks these numbers.)

        Everything else goes through `_expr_history`, which evaluates the
        expression as-of each past session — the same machinery
        series_ema/series_sma already use, so pct_rank over an sma, an rsi
        or a composed metric needs no new code on either engine.
        """
        if inner_node.get("ind") != "realized_vol":
            return self._expr_history(inner_node, symbol, params, lookback)
        w = inner_node.get("window")
        if isinstance(w, dict) and "param" in w:
            w = params[w["param"]]
        w = int(w)
        s = self.series(symbol)
        if len(s.returns) < w:
            return None
        vals = []
        rets = s.returns
        # exactly the last `lookback` daily vol values including today's
        # (deque(maxlen=lookback) semantics — off-by-one here flips the gate)
        start = max(w, len(rets) - lookback + 1)
        for end in range(start, len(rets) + 1):
            window = rets[end - w:end]
            mean = sum(window) / w
            vals.append(math.sqrt(sum((r - mean) ** 2 for r in window) / w))
        return vals if vals else None


def _freeze(node: dict, params: dict):
    def f(v):
        if isinstance(v, dict):
            if "param" in v:
                return ("param", v["param"], params[v["param"]])
            return tuple(sorted((k, f(x)) for k, x in v.items()))
        if isinstance(v, list):
            return tuple(f(x) for x in v)
        return v
    return f(node)
