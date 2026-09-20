"""Weight-expression trees for set_weights (spec §13.2).

Evaluates a tree of asset / equal / weighted / inverse_vol / if / best nodes to
a {symbol: fraction} vector. `best` over sub-strategies ranks children on their
SHADOW RETURN series — each child's vector is evaluated daily and marked
against realized per-symbol returns (the WM74 node_ret/node_prevw mechanism
that reproduced Composer's strategy-rank selector).

Composer-truth details kept deliberately:
- an asset with no price history yet contributes {} (graceful skip);
- asset metrics include TODAY's price as the latest close (Composer appends the
  rebalance-time price before evaluating);
- a condition touching NOT_READY data is false -> else branch.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import date

from .exprs import EvalContext, evaluate_bool

SHADOW_MAXLEN = 300


def _metric_from_closes(metric: str, closes: list[float], window: int):
    """Metric on a daily close series (today's price already appended)."""
    n = int(window)
    if metric == "cum_return":
        if len(closes) < n + 1 or closes[-n - 1] == 0:
            return None
        return closes[-1] / closes[-n - 1] - 1.0
    if metric == "drawdown":
        w = closes[-(n + 1):]
        if len(w) < 2:
            return None
        peak, worst = w[0], 0.0
        for c in w:
            peak = max(peak, c)
            if peak > 0:
                worst = min(worst, c / peak - 1.0)
        return worst
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes)) if closes[i - 1]]
    return _metric_from_returns(metric, rets, n)


def _metric_from_returns(metric: str, rets, window: int):
    r = list(rets)[-int(window):]
    if metric == "cum_return":
        if not r:
            return None
        acc = 1.0
        for x in r:
            acc *= 1.0 + x
        return acc - 1.0
    if metric == "drawdown":
        if not r:
            return None
        eq = peak = 1.0
        worst = 0.0
        for x in r:
            eq *= 1.0 + x
            peak = max(peak, eq)
            if peak > 0:
                worst = min(worst, eq / peak - 1.0)
        return worst
    if metric == "mean_return":
        return sum(r) / len(r) if r else None
    if metric == "realized_vol":
        if len(r) < 2:
            return None
        m = sum(r) / len(r)
        return math.sqrt(sum((x - m) ** 2 for x in r) / len(r))
    raise ValueError(f"unknown weight metric {metric}")


class WeightEngine:
    """Owns shadow state across the backtest. One instance per Backtester."""

    def __init__(self, ind):
        self.ind = ind                       # IndicatorEngine (daily closes, prices)
        self.node_ret: dict[str, deque] = {}     # node path -> shadow daily returns
        self.node_prevw: dict[str, dict] = {}    # node path -> yesterday's vector
        self._marked_day: dict[str, date] = {}   # node path -> last shadow-mark day

    # ---- daily returns at evaluation time (today vs prior close)

    def _today_ret(self, sym: str):
        prior = self.ind.prior_close.get(sym)
        last = self.ind.last_price.get(sym)
        if not prior or not last:
            return None
        return last / prior - 1.0

    def _asset_closes(self, sym: str):
        closes = list(self.ind.series(sym).closes)
        last = self.ind.last_price.get(sym)
        if last:
            closes = closes + [last]
        return closes

    # ---- evaluation

    def evaluate(self, node: dict, ctx: EvalContext, day: date,
                 path: str = "w") -> dict[str, float]:
        if "asset" in node:
            sym = node["asset"]
            if self.ind.series(sym).closes or self.ind.last_price.get(sym):
                return {sym: 1.0}
            return {}
        if "equal" in node:
            return self._equal([(f"{path}.{i}", c)
                                for i, c in enumerate(node["equal"])], ctx, day)
        if "weighted" in node:
            out: dict[str, float] = {}
            for i, item in enumerate(node["weighted"]):
                f = item["w"]
                if isinstance(f, dict):
                    f = float(ctx.params[f["param"]])
                if not f:
                    continue
                for s, x in self.evaluate(item["of"], ctx, day,
                                          f"{path}.{i}").items():
                    out[s] = out.get(s, 0.0) + float(f) * x
            return out
        if "if" in node:
            branch, tag = ((node.get("then") or [], "t")
                           if evaluate_bool(node["if"], ctx)
                           else (node.get("else") or [], "e"))
            return self._equal([(f"{path}.{tag}{i}", c)
                                for i, c in enumerate(branch)], ctx, day)
        if "inverse_vol" in node:
            spec = node["inverse_vol"]
            window = int(self._num(spec.get("window", 20), ctx))
            kids = [(f"{path}.{i}", c) for i, c in enumerate(spec["of"])]
            vols, vecs = {}, {}
            for p, c in kids:
                if "asset" in c:
                    vecs[p] = self.evaluate(c, ctx, day, p)
                else:
                    # strategy-shaped child: keep its shadow series warm so
                    # _child_metric has a return history to take the vol of
                    vecs[p] = self._mark_shadow(p, c, ctx, day)
                vols[p] = self._child_metric(c, p, "realized_vol", window, day, ctx)
            if any(not v for v in vols.values()):
                # Composer: ANY child without a readable vol -> equal blend
                # (not "weight the ready ones")
                return self._equal(kids, ctx, day, pre=vecs)
            inv = {p: 1.0 / v for p, v in vols.items()}
            total = sum(inv.values())
            out: dict[str, float] = {}
            for p, w in inv.items():
                for s, x in vecs[p].items():
                    out[s] = out.get(s, 0.0) + (w / total) * x
            return out
        if "best" in node:
            return self._best(node["best"], ctx, day, path)
        raise ValueError(f"unknown weight node: {node!r}")

    def _num(self, v, ctx):
        if isinstance(v, dict) and "param" in v:
            return float(ctx.params[v["param"]])
        return float(v)

    def _equal(self, kids, ctx, day, pre=None):
        if not kids:
            return {}
        out: dict[str, float] = {}
        w = 1.0 / len(kids)
        for p, c in kids:
            vec = pre[p] if pre is not None else self.evaluate(c, ctx, day, p)
            for s, x in vec.items():
                out[s] = out.get(s, 0.0) + w * x
        return out

    # ---- shadow marking: called once per day per strategy-child

    def _mark_shadow(self, path: str, child: dict, ctx: EvalContext,
                     day: date) -> dict[str, float]:
        vec = self.evaluate(child, ctx, day, path)
        if self._marked_day.get(path) != day:
            self._marked_day[path] = day
            prevw = self.node_prevw.get(path)
            if prevw:
                r = 0.0
                for s, w in prevw.items():
                    tr = self._today_ret(s)
                    if tr is not None:
                        r += w * tr
                self.node_ret.setdefault(path, deque(maxlen=SHADOW_MAXLEN)).append(r)
            else:
                self.node_ret.setdefault(path, deque(maxlen=SHADOW_MAXLEN))
            self.node_prevw[path] = dict(vec)
        return vec

    def _child_metric(self, child: dict, path: str, metric: str, window: int,
                      day: date, ctx: EvalContext):
        if "asset" in child:
            closes = self._asset_closes(child["asset"])
            if len(closes) < 2:
                return None
            return _metric_from_closes(metric, closes, window)
        rets = self.node_ret.get(path)
        if not rets:
            return None
        return _metric_from_returns(metric, rets, window)

    def _best(self, spec: dict, ctx: EvalContext, day: date,
              path: str) -> dict[str, float]:
        by = spec.get("by") or {}
        metric = by.get("metric", "cum_return")
        window = int(self._num(by.get("window", 60), ctx))
        order = spec.get("order", "top")
        k = int(spec.get("n", 1))
        kids = [(f"{path}.{i}", c) for i, c in enumerate(spec["of"])]

        vecs, scores = {}, {}
        for p, c in kids:
            if "asset" in c:
                vecs[p] = self.evaluate(c, ctx, day, p)
            else:
                vecs[p] = self._mark_shadow(p, c, ctx, day)   # keeps shadows warm
            scores[p] = self._child_metric(c, p, metric, window, day, ctx)

        ranked = [p for p in vecs if scores[p] is not None and vecs[p]]
        if not ranked:
            return {}
        ranked.sort(key=lambda p: scores[p], reverse=(order == "top"))
        chosen = ranked[:max(1, k)]
        out: dict[str, float] = {}
        w = 1.0 / len(chosen)
        for p in chosen:
            for s, x in vecs[p].items():
                out[s] = out.get(s, 0.0) + w * x
        return out


def collect_weight_symbols(node, out: set):
    """All symbols an IR weight tree references (for backfill)."""
    if isinstance(node, dict):
        if "asset" in node and isinstance(node["asset"], str):
            out.add(node["asset"])
        for v in node.values():
            collect_weight_symbols(v, out)
    elif isinstance(node, list):
        for x in node:
            collect_weight_symbols(x, out)
