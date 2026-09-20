"""Expression evaluator for the Strategy IR (spec §5).

Expressions are JSON trees. Evaluation happens against an EvalContext snapshot.
NOT_READY propagates: any comparison touching a NOT_READY operand is False, and
the journal records which operand was not ready.
"""
from __future__ import annotations

from typing import Any, Optional

NOT_READY = object()


class EvalContext:
    """What expressions can see: indicators, position state, sleeve state, params."""

    def __init__(self, params: dict, default_symbol: Optional[str],
                 indicator_fn, position_fn, sleeve_fn):
        self.params = params
        self.default_symbol = default_symbol
        self._indicator_fn = indicator_fn    # (spec: dict, symbol: str) -> float | NOT_READY
        self._position_fn = position_fn      # (name: str, symbol: str, kwargs: dict) -> value
        self._sleeve_fn = sleeve_fn          # (name: str) -> float
        self.trace: list[dict] = []          # journal operand trace for this evaluation

    def symbol_of(self, node: dict) -> str:
        s = node.get("symbol") or self.default_symbol
        if not s:
            raise ValueError(f"no symbol for node {node} and no default symbol")
        return s


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def evaluate(node: Any, ctx: EvalContext):
    """Evaluate an expression node. Returns bool/float/NOT_READY."""
    if _is_num(node):
        return float(node)
    if isinstance(node, bool):
        return node
    if not isinstance(node, dict):
        raise ValueError(f"bad expression node: {node!r}")

    # ---- leaf refs ----
    if "param" in node:
        return float(ctx.params[node["param"]])
    if "ind" in node:
        v = ctx._indicator_fn(node, ctx.symbol_of(node))
        ctx.trace.append({"ind": node["ind"], "symbol": ctx.symbol_of(node),
                          "value": None if v is NOT_READY else v})
        return v
    if "pos" in node:
        v = ctx._position_fn(node["pos"], ctx.symbol_of(node), node)
        ctx.trace.append({"pos": node["pos"], "symbol": ctx.symbol_of(node),
                          "value": None if v is NOT_READY else v})
        return v
    if "sleeve" in node:
        v = ctx._sleeve_fn(node["sleeve"])
        ctx.trace.append({"sleeve": node["sleeve"], "value": v})
        return v

    # ---- boolean ----
    if "all" in node:
        for sub in node["all"]:
            if not evaluate_bool(sub, ctx):
                return False
        return True
    if "any" in node:
        for sub in node["any"]:
            if evaluate_bool(sub, ctx):
                return True
        return False
    if "not" in node:
        return not evaluate_bool(node["not"], ctx)

    # ---- comparisons ----
    # "ne" exists as a first-class op (not sugar for not(eq)) so that a
    # NOT_READY operand yields False like every other comparison — wrapping
    # in "not" would flip it to True and fire rules on missing data
    for op in ("gt", "gte", "lt", "lte", "eq", "ne"):
        if op in node:
            a = evaluate(node[op][0], ctx)
            b = evaluate(node[op][1], ctx)
            if a is NOT_READY or b is NOT_READY:
                ctx.trace.append({"cmp": op, "not_ready": True})
                return False
            if op == "gt":
                return a > b
            if op == "gte":
                return a >= b
            if op == "lt":
                return a < b
            if op == "lte":
                return a <= b
            if op == "ne":
                return a != b
            return a == b

    # ---- arithmetic ----
    for op in ("add", "sub", "mul", "div"):
        if op in node:
            vals = [evaluate(sub, ctx) for sub in node[op]]
            if any(v is NOT_READY for v in vals):
                return NOT_READY
            acc = vals[0]
            for v in vals[1:]:
                if op == "add":
                    acc += v
                elif op == "sub":
                    acc -= v
                elif op == "mul":
                    acc *= v
                else:
                    # a zero denominator makes THIS evaluation not-ready
                    # (rule sits out, journal shows why) instead of killing
                    # the whole backtest with a division error
                    if v == 0:
                        ctx.trace.append({"div": "by zero", "not_ready": True})
                        return NOT_READY
                    acc /= v
            return acc

    # ---- branching ----
    if "if" in node:
        cond = evaluate_bool(node["if"], ctx)
        return evaluate(node["then"] if cond else node["else"], ctx)
    if "cases" in node:
        for case in node["cases"]:
            if evaluate_bool(case["when"], ctx):
                return evaluate(case["value"], ctx)
        return evaluate(node["default"], ctx)

    raise ValueError(f"unknown expression node: {node!r}")


def evaluate_bool(node: Any, ctx: EvalContext) -> bool:
    v = evaluate(node, ctx)
    if v is NOT_READY:
        return False
    return bool(v)
