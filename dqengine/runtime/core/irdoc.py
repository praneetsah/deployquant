"""IR-document helpers: what a strategy document REFERENCES.

Three pure functions over the JSON, with no engine in them. They lived in
the IR engine's engine.py because the Backtester was their first caller; by
the time that engine was deleted their callers were dqengine.codegen, the api's
backfill worker, the streamer's watchlist, the fill attributor and the live
router -- every one of which still needs them.

`collect_ir_symbols` is the broad answer (universe + every referenced
ticker) and is right where over-caution is right: data backfill, the UNKNOWN
set. `collect_tradeable_symbols` is the narrow one (things this IR can
HOLD), and is right where over-breadth is wrong: fill attribution, where two
deployments both looking like candidates forces a fill into `manual`.
"""
from __future__ import annotations


def expand_metrics(ir: dict) -> dict:
    """Resolve the user-defined indicator layer.

    `ir["metrics"]` is `{name: expr}` — named expressions users compose from
    the same vocabulary rules use (this is how MACD/Bollinger/… exist without
    being engine builtins). Any `{"metric": name}` node in the rules or in
    other definitions is replaced by its definition, depth-first with cycle
    detection, so the evaluator never sees a metric node. Idempotent when the
    document defines none. A `{"metric": name, "symbol": "QQQ"}` reference
    retargets every market leaf inside the definition that has no symbol of
    its own."""
    defs = ir.get("metrics") or {}
    if not defs:
        return ir

    resolving: list = []
    resolved: dict = {}

    def retarget(node, sym):
        if isinstance(node, list):
            return [retarget(x, sym) for x in node]
        if isinstance(node, dict):
            out = {k: retarget(v, sym) for k, v in node.items()}
            if ("ind" in out or "pos" in out) and "symbol" not in out:
                out["symbol"] = sym
            return out
        return node

    def expand(node):
        if isinstance(node, list):
            return [expand(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "metric" in node:
            name = node["metric"]
            if name not in defs:
                known = ", ".join(sorted(defs)) or "none defined"
                raise ValueError(
                    f'unknown indicator "{name}" — this strategy defines: '
                    f"{known}")
            if name in resolving:
                raise ValueError("indicator definitions form a loop: "
                                 + " → ".join(resolving + [name]))
            if name not in resolved:
                resolving.append(name)
                resolved[name] = expand(defs[name])
                resolving.pop()
            out = resolved[name]
            return retarget(out, node["symbol"]) if node.get("symbol") else out
        return {k: expand(v) for k, v in node.items()}

    out = dict(ir)
    out["rules"] = expand(ir.get("rules", []))
    return out


def collect_ir_symbols(ir: dict) -> list[str]:
    """Every symbol an IR references: universe, expr/action symbol overrides,
    weight-tree assets — with user-defined metrics expanded first, so a
    symbol referenced only inside a definition still gets its data. Used by
    the engine and the backfill worker."""
    ir = expand_metrics(ir)
    out = set(ir["universe"]["static"])

    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("symbol"), str):
                out.add(n["symbol"])
            if isinstance(n.get("asset"), str):
                out.add(n["asset"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(ir.get("rules", []))
    return sorted(out)


def collect_tradeable_symbols(ir: dict) -> list[str]:
    """Symbols this IR can actually HOLD: universe, action-level symbol
    overrides, and weight-tree assets. Deliberately EXCLUDES symbols
    referenced only inside expressions -- an RSI gate on TQQQ reads TQQQ,
    it does not trade it. Used for fill attribution, where over-breadth
    makes two deployments look like candidates for one fill and forces it
    into the `manual` bucket. `collect_ir_symbols` stays the right answer
    for the UNKNOWN set, where over-caution is correct."""
    from .weights import collect_weight_symbols
    ir = expand_metrics(ir)
    out = set(ir["universe"]["static"])
    for rule in ir.get("rules", []):
        action = rule.get("action")
        if not isinstance(action, dict):
            continue
        sym = action.get("symbol")
        if isinstance(sym, str):
            out.add(sym)
        collect_weight_symbols(action, out)
    return sorted(out)
