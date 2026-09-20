"""IR -> LEAN-style Python algorithm generator.

Powers "view code" and "Eject to Python" for block strategies. The generated
algorithm runs on dqengine.runtime — the same engine every block strategy
backtests and ticks live on, with no second implementation left to compare
against. Correctness is pinned by 66 golden fixtures (fills and equity to
the penny, each frozen when this generator's output for that case was last
proven fill-for-fill against the IR engine, before it was deleted) and by
the LEAN bench (`tools/bench_vs_lean.py`, 520 fills, $13,603.38). The
generated patterns deliberately mirror the hand-written
`dqengine/examples/tqqq_weekly.py`, which is itself bit-exact against LEAN.

The generated code does NOT reimplement the indicator layer. Every
market-data leaf goes through `dqengine.runtime.blocks.BlockContext`, which
drives dqengine.runtime's own `dqengine.runtime.core.indicators.IndicatorEngine` and
`dqengine.runtime.core.weights.WeightEngine` directly (the daily-series engine
block strategies have always used — distinct from the incremental,
per-update indicators `dqengine.runtime.indicators` gives a hand-written
QCAlgorithm) — so an sma, an rsi, an atr, a cross-symbol leaf and a
pct_rank are ONE computation, not arithmetic the generator re-derives and
keeps equal by hand.

Supported: rule strategies over any number of symbols, at minute or second
resolution — session_open / before_close / at_time / at_close triggers with
day selectors, once_per guards, market_order (buy and sell;
pct_equity/dollars/shares/pct_position, ref last/session_open/prior_close),
managed_target (limit), at_close_order (MOC/LOC), liquidate, set_weights,
python capsules; exprs over params, pos:*, sleeve:cash|equity, custom
`metrics` definitions and every indicator the IndicatorEngine knows,
if/cases/all/any/not/comparisons/math. `set_weights` is an ORDINARY rule
action: any number of allocation rules, on any trigger, with day selectors,
conditions and guards, mixed freely with per-symbol rules — all sharing the
one BlockContext and its one WeightEngine.

Anything else raises CodegenUnsupported with the block named.
"""
from __future__ import annotations

import json
import keyword as _kw
import re as _re
import textwrap


class CodegenUnsupported(Exception):
    pass


def _code_lines(snippet: str) -> list[str]:
    """User capsule code, dedented, ready to splice at any indent."""
    lines = textwrap.dedent(snippet).strip("\n").splitlines()
    return lines or ["pass"]


def _u(what: str):
    raise CodegenUnsupported(
        f"{what} can't be ejected to Python — a backtest of this strategy "
        f"fails, and so does a live tick, until the block changes")


def _py_literal(value):
    """A Python source literal for JSON-shaped data (dict/list/str/number/
    bool/None). Round-trips through json first so anything JSON could not
    carry (tuples, custom objects, NaN) is rejected here rather than
    emitted as source that only works by accident."""
    return repr(json.loads(json.dumps(value, allow_nan=False)))


# --------------------------------------------------------------- expressions

# Expression leaves that read MARKET DATA all go through BlockContext, which
# drives the IR engine's own IndicatorEngine. There is no second
# implementation of sma/rsi/atr/pct_rank/series_ema on this side any more —
# that is what makes cross-symbol leaves, custom metrics and derived series
# parity by construction instead of by test.


class _ExprCompiler:
    """IR expression -> python expression string.

    Symbol resolution is IR truth and easy to get wrong: `Backtester._ctx`
    builds EVERY EvalContext with `self.default_symbol` (= universe[0]),
    whatever the firing rule's action symbol is. So a bare `{"ind": "sma"}`
    inside a rule that trades TLT reads SPY's SMA when SPY heads the
    universe. Bind bare leaves to `default_symbol`, never to the action.
    """

    def __init__(self, params: dict, default_symbol: str):
        self.params = params
        self.default_symbol = default_symbol
        self.use_open = False       # set per rule: assess='session_open'

    # ---- helpers

    def _sym(self, node) -> str:
        return str(node.get("symbol") or self.default_symbol).upper()

    def _param(self, v):
        if isinstance(v, dict) and "param" in v:
            name = v["param"]
            if name not in self.params:
                _u(f'unknown param "{name}"')
            return f"self.{name}"
        if isinstance(v, (int, float)):
            return repr(v)
        _u(f"expression argument {v!r}")

    def compile(self, node) -> str:                     # noqa: C901
        if isinstance(node, bool):
            return repr(node)
        if isinstance(node, (int, float)):
            return repr(node)
        if not isinstance(node, dict):
            _u(f"expression {node!r}")
        if "param" in node:
            return self._param(node)
        if "metric" in node:
            # generate_python runs dqengine.runtime.core.expand_metrics before
            # compiling, so a metric node reaching here means the document
            # references one it does not define — say so, do not emit.
            _u(f'unknown custom indicator "{node["metric"]}"')
        if "sleeve" in node:
            if node["sleeve"] == "cash":
                return "self.portfolio.cash"
            if node["sleeve"] == "equity":
                return "self.portfolio.total_portfolio_value"
            _u(f'sleeve field {node["sleeve"]!r}')
        if "pos" in node:
            return self._position(node)
        if "ind" in node:
            return self._indicator(node)
        for op, sym_ in (("add", "+"), ("sub", "-"), ("mul", "*")):
            if op in node:
                parts = [f"({self.compile(a)})" for a in node[op]]
                return "(" + f" {sym_} ".join(parts) + ")"
        if "div" in node:
            parts = [f"({self.compile(a)})" for a in node["div"]]
            return "self._div(" + ", ".join(parts) + ")"
        for op, py in (("gt", ">"), ("gte", ">="), ("lt", "<"),
                       ("lte", "<="), ("eq", "=="), ("ne", "!=")):
            if op in node:
                # IR semantics: a comparison touching a not-ready operand is
                # False (never an error, never a skip)
                a, b = node[op]
                return (f"self._cmp(lambda: "
                        f"({self.compile(a)}) {py} ({self.compile(b)}))")
        if "all" in node:
            return "(" + " and ".join(f"({self.compile(a)})"
                                      for a in node["all"]) + ")"
        if "any" in node:
            return "(" + " or ".join(f"({self.compile(a)})"
                                     for a in node["any"]) + ")"
        if "not" in node:
            return f"(not ({self.compile(node['not'])}))"
        if "if" in node:
            return (f"(({self.compile(node['then'])}) if "
                    f"self._cmp(lambda: ({self.compile(node['if'])})) else "
                    f"({self.compile(node.get('else', 0))}))")
        if "cases" in node:
            out = f"({self.compile(node.get('default', 0))})"
            for case in reversed(node["cases"]):
                out = (f"(({self.compile(case['value'])}) if "
                       f"self._cmp(lambda: ({self.compile(case['when'])})) "
                       f"else {out})")
            return out
        _u(f"expression {json.dumps(node)[:80]}")

    def _position(self, node) -> str:
        p = node["pos"]
        sym = self._sym(node)
        if p == "invested":
            return f"self.portfolio[{sym!r}].invested"
        if p == "qty":
            return f"self.portfolio[{sym!r}].quantity"
        if p == "entry_price":
            return f"self._bc.entry_price({sym!r})"
        if p == "days_held":
            return f"self._bc.days_held({sym!r})"
        if p == "pnl_pct":
            basis = node.get("basis", "last")
            return (f"self._bc.pnl_pct({sym!r}, {basis!r}, "
                    f"use_open={self.use_open!r})")
        _u(f"position field {p!r}")

    def _indicator(self, node) -> str:
        """Every indicator leaf, including price/prior_close, goes to the
        shared IndicatorEngine. The node is emitted VERBATIM as a python
        literal so the two engines evaluate the identical spec — including
        `include_today`, `smoothing`, `field`, `min_obs` and any param
        reference inside a window."""
        # The node ships verbatim, so a param reference inside it (a window,
        # a lookback) never passes through _param. Validate it here or an
        # unknown name surfaces only as a KeyError mid-backtest.
        _walk(node, lambda n: self._param(n) if "param" in n else None)
        name = node["ind"]
        sym = self._sym(node)
        if name == "price" and node.get("field", "last") == "last":
            # the hot leaf, and the one with no cache: read it directly
            return f"self._bc.price({sym!r})"
        return f"self._bc.indicator({_py_literal(node)}, {sym!r})"


# ------------------------------------------------------------------ triggers

_DAY_CHECKS = {
    "all": None,
    "first_of_week": "self._is_first_of_week()",
    "last_of_week": "self._is_last_trading_day()",
    "day_before_last_of_week": "self._tomorrow_is_last_trading_day()",
    "first_of_month": "self._is_first_of_month()",
    "last_of_month": "self._is_last_of_month()",
}


def _day_check(days: str) -> str | None:
    if days not in _DAY_CHECKS:
        _u(f"day selector {days!r}")
    return _DAY_CHECKS[days]


def _size(ec: "_ExprCompiler", size: dict) -> tuple:
    """(kind, python-expression, ref) for an order size.

    Mirrors Backtester._sized_qty, which is side-agnostic: it returns a
    POSITIVE share count for both buys and sells, and `ref` only applies to
    pct_equity (dollars divides by the live price; shares and pct_position
    never touch a price at all)."""
    ref = size.get("ref", "last")
    if "pct_equity" in size:
        return ("pct_equity", ec.compile(size["pct_equity"]), ref)
    if "dollars" in size:
        return ("dollars", ec.compile(size["dollars"]), "last")
    if "shares" in size:
        return ("shares", ec.compile(size["shares"]), None)
    if "pct_position" in size:
        return ("pct_position", ec.compile(size["pct_position"]), None)
    _u(f"order size {json.dumps(size)[:60]}")


def _at_close_order(ec: "_ExprCompiler", a: dict) -> dict:
    """MOC/LOC. The IR engine only permits it under before_close(1) or
    at_close (Backtester._validate_at_close_order_triggers), and fills it
    at THAT bar's close — the same fill price liquidate gets there."""
    qraw = a.get("qty", "all")
    return {
        "ac_side": -1 if a.get("side", "buy") == "sell" else 1,
        "ac_qty": None if qraw == "all" else ec.compile(qraw),
        "ac_limit": (ec.compile(a["price"])
                     if a.get("price") is not None else None),
    }


def _order_qty(kind: str, size_expr: str, ref: str | None, sym: str) -> str:
    """The share count, mirroring Backtester._sized_qty exactly — including
    that it is POSITIVE for both sides and truncates with int()."""
    if kind == "pct_equity":
        ref_px = {"last": f"self._bc.price({sym!r})",
                  "session_open": f"self._bc.session_open({sym!r})",
                  "prior_close": (f"self._bc.indicator("
                                  f"{{'ind': 'prior_close'}}, {sym!r})")}.get(ref)
        if ref_px is None:
            _u(f"order price reference {ref!r}")
        return (f"ref_price = {ref_px}\n"
                f"qty = int(float(self.portfolio.total_portfolio_value)"
                f" * ({size_expr}) / ref_price)")
    if kind == "dollars":
        return (f"ref_price = self._bc.price({sym!r})\n"
                f"qty = int(({size_expr}) / ref_price)")
    if kind == "shares":
        return f"qty = int({size_expr})"
    return (f"qty = int(abs(self.portfolio[{sym!r}].quantity) "
            f"* ({size_expr}))")


def _at_close_body(item: dict, sym: str, tag: str, gcons: str | None) -> str:
    """MOC/LOC, filled at the before_close(1) decision bar — the bar the IR
    engine hands this action as `fill_price`, and the bar a market order
    placed from this handler fills against. Order of operations is IR's:
    size, price, limit, CONSUME THE GUARD, marketability, submit. An
    unmarketable LOC expires without retrying today."""
    side = item["ac_side"]
    lines = []
    if item["ac_qty"] is None:
        lines.append(f"qty = abs(int(self.portfolio[{sym!r}].quantity))")
    else:
        lines.append(f"qty = int(round({item['ac_qty']}))")
    lines += ["if qty <= 0:", "    return",
              f"close_px = self._bc.price({sym!r})"]
    if item["ac_limit"] is not None:
        lines.append(f"limit_px = round(float({item['ac_limit']}), 2)")
    if gcons:
        lines.append(gcons)
    if item["ac_limit"] is not None:
        test = "close_px <= limit_px" if side > 0 else "close_px >= limit_px"
        lines += [f"if not ({test}):",
                  "    return                      # LOC expires unfilled"]
    lines.append(f"self.market_order({sym!r}, {side} * qty, tag={tag})")
    return "\n".join(lines)


# ---------------------------------------------------------------- the writer

class _W:
    def __init__(self):
        self.lines: list[str] = []

    def w(self, line: str = "", indent: int = 0):
        self.lines.append(("    " * indent + line).rstrip())

    def block(self, text: str, indent: int = 0):
        for ln in text.splitlines():
            self.w(ln, indent)

    def code(self) -> str:
        return "\n".join(self.lines).rstrip() + "\n"


# ------------------------------------------------------------------ analysis

def _walk(node, fn):
    """Visit every dict in an IR fragment."""
    if isinstance(node, dict):
        fn(node)
        for v in node.values():
            _walk(v, fn)
    elif isinstance(node, list):
        for x in node:
            _walk(x, fn)


def _uses(ir: dict, names: set) -> bool:
    """True if any indicator leaf anywhere in the rules is one of `names`
    — including inside pct_rank's `of`, series_ema/sma's `of`, and weight
    trees, which is why this walks rather than scans the top level."""
    hit = []
    _walk(ir.get("rules") or [], lambda n: hit.append(True)
          if n.get("ind") in names else None)
    return bool(hit)


def generate_python(ir: dict, margin_max: float | None = None) -> str:
    """The generator. margin_max: the leverage the block strategy runs with
    (the platform's run-config field) — baked in as set_leverage so the
    ejected code is self-contained.

    Custom indicators are expanded FIRST (dqengine.runtime.core.expand_metrics),
    so no {"metric": ...} node ever reaches the compiler and a definition
    referencing another ticker still gets that ticker subscribed."""
    from dqengine.runtime.core import collect_ir_symbols, expand_metrics
    ir = expand_metrics(ir)
    meta = ir.get("meta") or {}
    resolution = (meta.get("resolution") or "minute")
    if resolution not in ("minute", "second"):
        _u(f"{resolution}-resolution strategies")

    universe = [s.upper() for s in (ir.get("universe") or {}).get("static") or []]
    if not universe:
        _u("a strategy with no universe")
    # Every symbol the IR references — universe, action overrides, expr
    # symbols, weight-tree assets. A symbol the generated code never
    # subscribes has no price, so its indicator reads NOT_READY forever and
    # the gate it guards silently never fires.
    syms = list(universe)
    for extra in collect_ir_symbols(ir):
        if extra.upper() not in syms:
            syms.append(extra.upper())
    primary = universe[0]

    rules = ir.get("rules") or []
    caps = ir.get("capsules") or {}
    params = dict(ir.get("params") or {})
    # A param becomes a CLASS ATTRIBUTE of the generated algorithm (`NAME =
    # value`) and `self.NAME` at every use site, so a name that is not a
    # Python name emits a module that does not parse — `my param = 10`,
    # `2X = 3`. The editor's param field stripped everything outside
    # [A-Z0-9_] but allowed a leading digit until this was found, and an
    # imported or AI-authored document never goes through that field at
    # all. Refuse it here, named: a SyntaxError raised at import time names
    # a line of generated source, not the strategy or the block behind it.
    for k in params:
        if not isinstance(k, str) or not k.isidentifier() or _kw.iskeyword(k):
            raise CodegenUnsupported(
                f"a param named {k!r} can't be ejected to Python — param "
                f"names become Python attributes on the generated "
                f"algorithm, so rename it to letters, digits and "
                f"underscores, not starting with a digit")
    margin = float(margin_max or meta.get("default_margin") or 1.0)

    # The IR engine warms 260 sessions on its MULTI path and not at all on
    # its single-symbol path (_run_single starts cold at cfg.start). Warm
    # here under exactly the same condition, or a single-symbol strategy's
    # indicators are ready on day one where the oracle is still warming.
    uses_weights = any((r.get("action") or {}).get("type") == "set_weights"
                       for r in rules)
    # TWO names, deliberately, for what is today one predicate.
    #
    # `multi` is a SEMANTIC fact about the strategy: with more than one
    # tradeable symbol (or a weight tree) a liquidate closes the whole
    # sleeve, not just the firing rule's symbol. `warm` is an OPERATIONAL
    # choice: how many pre-start sessions BlockContext replays so the
    # indicators are ready on day one. They have the same value because the
    # IR engine happened to warm on exactly its multi path -- not because
    # one implies the other.
    #
    # While they were one variable, changing warm-up depth would have
    # silently changed what a liquidation closes: a money-path change
    # arriving through a performance tweak. Splitting them costs nothing
    # (the emitted source is byte-identical, which the 66 goldens prove)
    # and makes that impossible.
    multi = uses_weights or len(collect_ir_symbols(ir)) > 1
    warm = multi
    # atr is the only indicator that reads highs and lows; tracking them
    # needs on_data, which forfeits the quiet-bar fast path for the whole
    # run. Pay it only when the strategy actually asks for atr.
    track_ohlc = uses_weights or _uses(ir, {"atr"})

    ec = _ExprCompiler(params, primary)
    compiled: list[dict] = []
    for r in rules:
        t = r["trigger"]
        a = r["action"]
        atype = a["type"]
        sym = str(a.get("symbol") or primary).upper()
        # assess='session_open' pins this rule's pnl basis to the morning
        # snapshot; it is compiled in, not swapped at runtime.
        ec.use_open = (t.get("assess") == "session_open")
        item = {
            "id": r["id"],
            "method": "_rule_" + _re.sub(r"\W", "_", r["id"]),
            "sym": sym,
            "trigger": t["type"],
            "trigger_raw": t,
            "days": _day_check(t.get("days", "all")),
            "guard": (r.get("guard") or {}).get("once_per"),
            "when": ec.compile(r["when"]) if r.get("when") else None,
            "action": a,
        }
        if atype == "market_order":
            # A market_order with a non-market order_type is a RESTING entry
            # in the IR engine (a breakout stop, a pullback limit): it waits
            # at the broker and only fills on a breach. Emitting
            # self.market_order() for it would buy IMMEDIATELY when the rule
            # fires -- and the two engines would then disagree about whether
            # a position exists at all. On the python engine that is a
            # wrong-way trade in BOTH directions. Refuse.
            if a.get("order_type", "market") != "market":
                _u(f'resting {a.get("order_type")} entries '
                   f'(market_order with order_type)')
            item["side"] = -1 if a.get("side", "buy") == "sell" else 1
            item["size"] = _size(ec, a.get("size") or {})
        elif atype == "managed_target":
            if a.get("order_type", "limit") != "limit":
                _u(f'{a.get("order_type")} managed targets')
            # qty is accepted and ignored: the IR engine never reads
            # managed_target["qty"] and always sells the whole position
            # (_check_targets). Honouring it here would trade differently
            # from the oracle; refusing it would reject saved strategies
            # that set a field the engine has always ignored.
            item["target_px"] = ec.compile(a["price"])
            item["target_qty_note"] = str(a.get("qty", "all"))
        elif atype == "at_close_order":
            item.update(_at_close_order(ec, a))
        elif atype == "liquidate":
            if a.get("order_type", "market") != "market":
                _u(f'{a.get("order_type")} liquidation')
        elif atype == "set_weights":
            item["weights"] = a["weights"]
        elif atype == "python":
            code_str = (a.get("code") or "")
            if not code_str.strip():
                _u("an empty python block")
            item["python"] = code_str
            for pname, pval in (a.get("params") or {}).items():
                if not _re.match(r"^[A-Z_][A-Z0-9_]*$", str(pname)):
                    _u(f"python-block param name {pname!r} (needs "
                       f"uppercase A-Z, 0-9 and underscore, not starting "
                       f"with a digit — it becomes a Python attribute on "
                       f"the generated algorithm)")
                if not isinstance(pval, (int, float)):
                    _u(f"python-block param {pname!r} must be a number")
                params.setdefault(pname, pval)
        else:
            _u(f"the {atype} action")
        if item["guard"] not in (None, "day", "week", "month", "position",
                                 "forever"):
            _u(f'the once_per {item["guard"]!r} guard')
        compiled.append(item)
    ec.use_open = False

    by_trigger: dict[str, list[dict]] = {}
    at_times: list[str] = []
    before_close_min: set[int] = set()
    for item in compiled:
        t = item["trigger"]
        if t == "session_open":
            by_trigger.setdefault("open", []).append(item)
        elif t == "before_close":
            m = int(item["trigger_raw"].get("minutes", 1))
            before_close_min.add(m)
            by_trigger.setdefault(f"bc{m}", []).append(item)
        elif t == "at_time":
            if resolution == "second":
                # The IR engine fires an at_time rule only on a bar whose
                # END is exactly the wall time (`end_ms == hhmm`); the
                # runtime's schedule fires on the first bar ending AT OR
                # AFTER it. At minute resolution those are the same bar. At
                # second resolution a symbol that did not print in that one
                # second has no such bar, and the oracle then skips the
                # rule for the whole session while the generated code fires
                # it later at a different price — measured 2026-09-10:
                # 0 fills against 4. Refuse rather than mostly match.
                _u("an at_time trigger on a second-resolution strategy")
            hh, mm = item["trigger_raw"]["time"].split(":")
            key = f"at_{int(hh):02d}{int(mm):02d}"
            if key not in at_times:
                at_times.append(key)
            by_trigger.setdefault(key, []).append(item)
        elif t == "at_close":
            # The IR engine fires at_close rules in the before-close(1)
            # BATCH, interleaved with before_close(1) rules in document
            # order (_run_session step 3 / _fire_before_close_batch_multi).
            # assess='session_open' only pins the pnl basis, which the
            # compiler has already baked into pnl_pct(use_open=True).
            if item["trigger_raw"].get("assess", "session_open") \
                    != "session_open":
                _u("at_close with an assess mode other than session_open")
            before_close_min.add(1)
            by_trigger.setdefault("bc1", []).append(item)
        else:
            _u(f"the {t!r} trigger")
    # document order inside the before-close batch, across both types
    order = {r["id"]: i for i, r in enumerate(rules)}
    if "bc1" in by_trigger:
        by_trigger["bc1"].sort(key=lambda it: order[it["id"]])

    name = meta.get("name") or "Ejected Strategy"
    cls = "".join(p for p in "".join(
        c if c.isalnum() else " " for c in name).title().split()) or "Ejected"
    res_enum = "Resolution.SECOND" if resolution == "second" \
        else "Resolution.MINUTE"
    # The IR engine fires its session_open batch on the session's FIRST bar
    # — `min(start_ms) + bar_ms`, i.e. that bar's END. A scheduled event
    # fires on the first bar ending at or after its wall time, so at minute
    # resolution `after_market_open(sym, 1)` names exactly that bar (09:31)
    # and is what an ejected file should carry to LEAN. At second
    # resolution the first bar ends at 09:30:01 and a 1-minute offset would
    # land SIXTY bars into the session, at a different price: the whole
    # open batch would trade off the wrong print (2026-09-10, sec_rules).
    open_offset = 1 if resolution == "minute" else 0

    o = _W()
    o.w("from AlgorithmImports import *")
    o.w("from dqengine.runtime.blocks import BlockContext, NotReady")
    if any(i["action"]["type"] == "set_weights" for i in compiled):
        o.w("from dqengine.runtime.allocation import Allocator")
    o.w()
    o.w("# platform-generated: permits the reserved `ir:` order-tag prefix,")
    o.w("# which carries each order's IR rule id as its live identity")
    o.w("__STRATEGY_LAB_GENERATED__ = True")
    o.w()
    # repr, not json.dumps: this is JSON-shaped data going INTO Python
    # source. JSON spells True/False/None as true/false/null, and a Composer
    # import carries `include_today: true` on every indicator -- json.dumps
    # here produced a module that NameErrors on import (2026-09-08).
    o.w(f"PARAMS = {_py_literal(params)}")
    o.w(f"SYMS = {_py_literal(syms)}")
    o.w(f"UNIVERSE = {_py_literal(universe)}")
    o.w(f"PRIMARY = {_py_literal(primary)}")
    # once_per=position guards are released per SYMBOL: the IR engine's
    # _on_flat clears the guards of the rules bound to the symbol that went
    # flat (single-symbol: all of them, which is the same set).
    rules_by_sym: dict[str, list[str]] = {}
    for item in compiled:
        rules_by_sym.setdefault(item["sym"], []).append(item["id"])
    o.w(f"RULES_BY_SYMBOL = {_py_literal(rules_by_sym)}")
    for item in compiled:
        if item["action"]["type"] == "set_weights":
            o.w(f"WEIGHTS_{item['method'][6:].upper()} = "
                f"{_py_literal(item['weights'])}")
    o.w()
    o.w()
    o.w(f"class {cls}(QCAlgorithm):")
    o.w(f'"""{name} — ejected from the block editor. Runs the SAME', 1)
    o.w('indicator engine the block strategy runs, so the two agree by', 1)
    o.w('construction rather than by two implementations matching."""', 1)
    o.w()
    for k, v in params.items():
        o.w(f"{k} = {_py_literal(v)}", 1)
    if params:
        o.w()

    o.w("def initialize(self):", 1)
    o.w("self.set_start_date(2021, 1, 4)   # the platform run overrides these", 2)
    o.w("self.set_end_date(2026, 6, 9)", 2)
    o.w("self.set_cash(10000)", 2)
    o.w("for t in SYMS:", 2)
    if margin != 1.0:
        o.w(f"sec = self.add_equity(t, {res_enum})", 3)
        o.w(f"sec.set_leverage({margin})", 3)
    else:
        o.w(f"self.add_equity(t, {res_enum})", 3)
    o.w("self.sym = self.securities[PRIMARY].symbol", 2)
    o.w("self._security = self.securities[PRIMARY]", 2)
    o.w("self.symbols = [self.securities[t].symbol for t in SYMS]", 2)
    o.w("# ONE indicator engine, shared with every rule and every weight", 2)
    o.w("# tree below. NOT warmed here: the platform applies the real", 2)
    o.w("# start/end AFTER initialize returns, so warming now would read", 2)
    o.w("# history relative to the placeholder dates above.", 2)
    o.w("self._bc = BlockContext(self, PARAMS, SYMS, universe=UNIVERSE,", 2)
    o.w(f"                        warm={warm!r}, track_ohlc={track_ohlc!r})", 2)
    for item in compiled:
        if item["action"]["type"] == "set_weights":
            o.w(f"self._alloc_{item['method'][6:]} = Allocator("
                f"self, WEIGHTS_{item['method'][6:].upper()}, PARAMS, SYMS, "
                f"ctx=self._bc)", 2)
    o.w("self._current_day = None", 2)
    o.w("self._prev_session_day = None", 2)
    guards = {i["guard"] for i in compiled if i["guard"]}
    if "week" in guards:
        o.w("self._guard_week = {}", 2)
    if "month" in guards:
        o.w("self._guard_month = {}", 2)
    if "day" in guards:
        o.w("self._guard_day = {}", 2)
    if "position" in guards or "forever" in guards:
        o.w("self._guard_flag = {}", 2)
    # session bookkeeping runs off the open bell + end-of-day, NOT on_data
    # (unless the strategy reads atr) — leaving on_data unoverridden keeps
    # the ejected algorithm eligible for the runtime's quiet-bar fast path.
    # Registered FIRST so the roll happens before any strategy handler that
    # fires on the same bar.
    o.w("self.schedule.on(self.date_rules.every_day(self.sym),", 2)
    o.w("                 self.time_rules.after_market_open(self.sym, 0),", 2)
    o.w("                 self._session_start)", 2)
    o.w("self.schedule.on(self.date_rules.every_day(self.sym),", 2)
    o.w(f"                 self.time_rules.after_market_open(self.sym, "
        f"{open_offset}),", 2)
    o.w("                 self._at_open)", 2)
    if caps.get("setup"):
        o.w("# ---- python block: setup (runs at the end of initialize)", 2)
        for line in _code_lines(caps["setup"]):
            o.w(line, 2 if line.strip() else 0)
    for m in sorted(before_close_min):
        o.w("self.schedule.on(self.date_rules.every_day(self.sym),", 2)
        o.w(f"                 self.time_rules.before_market_close(self.sym, {m}),", 2)
        o.w(f"                 self._before_close_{m})", 2)
    for key in at_times:
        hh, mm = int(key[3:5]), int(key[5:7])
        o.w("self.schedule.on(self.date_rules.every_day(self.sym),", 2)
        o.w(f"                 self.time_rules.at({hh}, {mm}),", 2)
        o.w(f"                 self._{key})", 2)
    o.w()

    o.block("""def _session_start(self):
    day = self.time.date()
    if day != self._current_day:
        self._prev_session_day = self._current_day
        self._current_day = day
    self._bc.start_session(day)

def on_end_of_day(self, symbol=None):
    day = self.time.date()
    if day != self._current_day:
        # warm-up session: no events fired, but the daily series still
        # has to advance or every rolling window is one day short
        self._prev_session_day = self._current_day
        self._current_day = day
    self._bc.close_session(day)""", 1)
    o.w()
    if track_ohlc or caps.get("on_data"):
        o.w("def on_data(self, data):", 1)
        o.w("# per-session OHLC for the daily series (atr reads highs and", 2)
        o.w("# lows). Overriding on_data disables the quiet-bar fast path,", 2)
        o.w("# by design.", 2)
        if track_ohlc:
            o.w("self._bc.on_bar(data)", 2)
        if caps.get("on_data"):
            o.w("# python block: on_data (every bar)", 2)
            for line in _code_lines(caps["on_data"]):
                o.w(line, 2 if line.strip() else 0)
        o.w()

    o.block("""def _div(self, a, b):
    if b == 0:
        raise NotReady
    return a / b

def _cmp(self, f):
    # IR semantics: comparisons and branch conditions over a not-ready
    # value are simply False
    try:
        return bool(f())
    except NotReady:
        return False

def _week_key(self, d):
    iso = d.isocalendar()
    return iso[0] * 100 + iso[1]

def _prior_session(self):
    # The session BEFORE today on the calendar -- the calendar reaches back
    # through the store's history, exactly what the IR engine consults.
    # Reading "no previous session in this RUN" as "first of week/month"
    # made a deployment started on a Wednesday fire its weekly entry on
    # day one (live 2026-08-20, adoption gate refusal 2026-09-06).
    if self._prev_session_day is not None:
        return self._prev_session_day
    cal = getattr(self, "_calendar", None)
    days = getattr(cal, "days", None)
    if days:
        from bisect import bisect_left
        i = bisect_left(days, self._current_day)
        if i > 0:
            return days[i - 1]
    return None

def _is_first_of_week(self):
    prev = self._prior_session()
    if prev is None:
        cal = getattr(self, "_calendar", None)
        if cal is not None and self._current_day in getattr(cal, "_index", {}):
            return cal.is_first_of_week(self._current_day)   # rule-based at the edge
        return True
    return self._week_key(prev) != self._week_key(self._current_day)

def _is_first_of_month(self):
    prev = self._prior_session()
    if prev is None:
        cal = getattr(self, "_calendar", None)
        if cal is not None and self._current_day in getattr(cal, "_index", {}):
            return cal.is_first_of_month(self._current_day)
        return True
    return (prev.year, prev.month) != \\
        (self._current_day.year, self._current_day.month)

def _next_trading_day(self, d):
    nxt = self._security.exchange.hours.get_next_trading_day(d)
    return nxt.date() if hasattr(nxt, "date") else nxt

def _is_last_trading_day(self):
    nxt = self._next_trading_day(self._current_day)
    return self._week_key(self._current_day) != self._week_key(nxt)

def _tomorrow_is_last_trading_day(self):
    nxt = self._next_trading_day(self._current_day)
    nxt2 = self._next_trading_day(nxt)
    return self._week_key(nxt) != self._week_key(nxt2)

def _is_last_of_month(self):
    nxt = self._next_trading_day(self._current_day)
    return (nxt.year, nxt.month) != \\
        (self._current_day.year, self._current_day.month)

def _place_or_update_target(self, sym, qty, limit_px, tag=""):
    # reconcile the resting GTC sell limit in place (price/qty), never
    # cancel-and-resubmit -- one order, updated each morning
    tickets = [t for t in self.transactions.get_open_order_tickets(sym)
               if t.order_type == OrderType.LIMIT and t.quantity < 0]
    if tickets:
        fields = UpdateOrderFields()
        fields.limit_price = limit_px
        fields.quantity = -qty
        tickets[0].update(fields)
        for extra in tickets[1:]:
            extra.cancel()
    else:
        self.limit_order(sym, -qty, limit_px, tag=tag)""", 1)
    o.w()
    if multi:
        # IR truth on the MULTI path (_fire_rule_inner's liquidate branch):
        # a liquidate cancels every resting target and closes the WHOLE
        # sleeve, not just the firing rule's own symbol. Walking the
        # sleeve's own order submits the exits in the oracle's sequence.
        o.block("""def _liquidate_all(self, sym, tag):
    # `sym` is the firing rule's symbol; a liquidate closes everything.
    self.transactions.cancel_open_orders()
    for s in self._bc.held_symbols():
        qty = int(self.portfolio[s].quantity)
        if qty > 0:
            self.market_order(s, -qty, tag=tag)
            if not self.portfolio[s].invested:
                self._on_flat(s)""", 1)
    else:
        o.block("""def _liquidate_all(self, sym, tag):
    self.transactions.cancel_open_orders(sym)
    qty = int(self.portfolio[sym].quantity)
    if qty > 0:
        self.market_order(sym, -qty, tag=tag)
        if not self.portfolio[sym].invested:
            self._on_flat(sym)""", 1)
    o.w()
    if "position" in guards:
        o.block("""def _on_flat(self, sym):
    # IR `_on_flat`: a position ending releases the once_per=position
    # guards of the rules bound to THAT symbol. Called from exactly three
    # places, as the IR engine calls it: a liquidate, a managed-target
    # fill, and a rebalance that took a symbol to zero. A plain market
    # sell that flattens does NOT release them.
    for rid in RULES_BY_SYMBOL.get(sym, ()):
        self._guard_flag.pop("position:" + rid, None)""", 1)
    else:
        o.block("""def _on_flat(self, sym):
    # no once_per=position guard in this strategy: nothing to release
    pass""", 1)
    o.w()
    # KNOWN GAP (Phase 3 Hazard 6, pinned by
    # test_a_carried_day_rebalance_sell_does_not_release_the_position_guard
    # in tests/runtime/test_codegen_allocation_shapes.py): the emitted
    # `on_order_event` below releases position guards only for a LIMIT
    # ticket. A rebalance sell submitted on a CARRIED (data-less) session
    # rests as SUBMITTED instead of filling, so Allocator.rebalance's own
    # on_flat is correctly skipped; when check_resting fills it later the
    # ticket is a MARKET one and nothing releases the guard. Net: a
    # once_per=position rule whose position was closed that way stays
    # blocked for the rest of the run. Safe direction, silent failure.
    # Phase 4 owns the fix (release for a MARKET ticket too when the fill
    # flattened the symbol AND the ticket carries a rebalance tag).
    #
    # Deliberately a comment on the GENERATOR, not on the emitted source:
    # every byte inside the o.block below is frozen in 66 golden fixtures,
    # and this phase's contract is that not one of them moves.
    o.block("""def on_order_event(self, e):
    # ANY fill that leaves this symbol flat tears down its resting orders.
    # Not housekeeping: the IR engine deletes a managed target the moment
    # sleeve.qty <= 0 (_check_targets), while dqengine.runtime's check_resting
    # fills a resting LIMIT sell whenever the price crosses, position or
    # no position. So a market sell (or a sell at_close_order) that
    # flattens under a live target leaves a ticket that SHORTS the sleeve
    # where the oracle simply has nothing left to fill.
    if e.status != OrderStatus.FILLED:
        return
    if self.portfolio[e.symbol].invested:
        return
    self.transactions.cancel_open_orders(e.symbol)
    ticket = self.transactions.get_order_by_id(e.order_id)
    if ticket is not None and ticket.order_type == OrderType.LIMIT:
        # the guard release, on the other hand, is NOT for every exit: a
        # managed-target fill is one of the IR engine's three _on_flat
        # sites, a plain market sell is not.
        self._on_flat(e.symbol)""", 1)
    o.w()

    def guard_check(item) -> str | None:
        g, rid = item["guard"], item["id"]
        if g == "week":
            return (f'self._guard_week.get({rid!r}) != '
                    f"self._week_key(self._current_day)")
        if g == "month":
            return (f'self._guard_month.get({rid!r}) != '
                    f"(self._current_day.year, self._current_day.month)")
        if g == "day":
            return f'self._guard_day.get({rid!r}) != self._current_day'
        if g in ("position", "forever"):
            return f'not self._guard_flag.get("{g}:{rid}")'
        return None

    def guard_consume(item) -> str | None:
        g, rid = item["guard"], item["id"]
        if g == "week":
            return (f'self._guard_week[{rid!r}] = '
                    f"self._week_key(self._current_day)")
        if g == "month":
            return (f'self._guard_month[{rid!r}] = '
                    f"(self._current_day.year, self._current_day.month)")
        if g == "day":
            return f'self._guard_day[{rid!r}] = self._current_day'
        if g in ("position", "forever"):
            return f'self._guard_flag["{g}:{rid}"] = True'
        return None

    for item in compiled:
        a = item["action"]
        atype = a["type"]
        sym = item["sym"]
        tag = f'"ir:{item["id"]}"'
        o.w(f"def {item['method']}(self):", 1)
        o.w(f"# rule: {item['id']}  ({atype} on {sym})", 2)
        ind = 2
        if item["days"]:
            o.w(f"if not {item['days']}:", ind)
            o.w("return", ind + 1)
        gc = guard_check(item)
        if gc:
            o.w(f"if not ({gc}):", ind)
            o.w("return", ind + 1)
        if item["when"]:
            o.w(f"if not ({item['when']}):", ind)
            o.w("return", ind + 1)
        gcons = guard_consume(item)
        if atype == "python":
            o.w("# python block — runs verbatim; params are the class", ind)
            o.w("# attributes above (self.NAME)", ind)
            for line in _code_lines(item["python"]):
                o.w(line, ind if line.strip() else 0)
            if gcons:
                o.w(gcons, ind)
        elif atype == "market_order":
            kind, size_expr, ref = item["size"]
            o.block(_order_qty(kind, size_expr, ref, sym), ind)
            o.w("if qty > 0:", ind)
            # tagged with the IR rule id under the reserved prefix: the
            # ledger then matches this order to the broker rows the IR
            # engine matches (rule-tagged rows first, pool order after).
            o.w(f"self.market_order({sym!r}, {item['side']} * qty, "
                f"tag={tag})", ind + 1)
            if gcons:
                o.w(gcons, ind + 1)
        elif atype == "managed_target":
            o.w(f"# qty={item['target_qty_note']!r}: the IR engine never "
                f"reads managed_target['qty'] and always", ind)
            o.w("# exits the whole position, so this target is for all of it.",
                ind)
            o.w(f"qty = int(self.portfolio[{sym!r}].quantity)", ind)
            o.w("if qty <= 0:", ind)
            o.w("return", ind + 1)
            o.w(f"limit_px = {item['target_px']}", ind)
            # `ir:<rule id>` — the identity the live layer turns into the
            # broker cid prefix, so a block deployment compiled to python
            # keeps the cid prefixes its IR deployment had.
            o.w(f"self._place_or_update_target({sym!r}, qty, limit_px, "
                f"tag={tag})", ind)
            if gcons:
                o.w(gcons, ind)
        elif atype == "at_close_order":
            o.block(_at_close_body(item, sym, tag, gcons), ind)
        elif atype == "liquidate":
            # CONSUME FIRST, then act — the IR engine's order (engine.py's
            # liquidate branch consumes before _market_close_position, and
            # its set_weights branch before _rebalance_to). It matters for
            # exactly these two actions because both reach _on_flat, which
            # POPS the once_per=position flag: consume-after would set the
            # flag the exit just released and the rule would fire once and
            # never again — a flat-sleeve instruction that stops being
            # emitted. (at_close_order consumes before its submit too, in
            # _at_close_body. market_order and managed_target consume after,
            # which is inert: on_order_event releases guards for a LIMIT
            # fill only, and a managed target rests rather than filling at
            # the point it is placed.)
            if gcons:
                o.w(gcons, ind)
            o.w(f"self._liquidate_all({sym!r}, {tag})", ind)
        elif atype == "set_weights":
            if gcons:
                o.w(gcons, ind)                     # before, see `liquidate`
            o.w(f"self._alloc_{item['method'][6:]}.rebalance(", ind)
            o.w(f"    self.time.date(), tag={tag}, on_flat=self._on_flat)",
                ind)
        o.w()

    def dispatcher(mname: str, items: list[dict], open_batch: bool = False):
        o.w(f"def {mname}(self):", 1)
        o.w("if self._current_day is None:", 2)
        o.w("return", 3)
        # last_price is what the indicator layer reads as today's price —
        # the IR engine sets it before firing rules on a bar, so mark here
        # and the whole batch sees the same prices the oracle saw.
        o.w("self._bc.mark_prices()", 2)
        o.w(f"self._bc.take_snapshot(freeze_open={open_batch!r})", 2)
        for item in items:
            o.w("try:", 2)
            o.w(f"self.{item['method']}()", 3)
            o.w("except NotReady:", 2)
            o.w("pass", 3)
        if not items:
            o.w("pass", 2)
        o.w()

    # The open batch ALWAYS runs, even with no session_open rules: it is
    # where the session's pnl basis is frozen (IR `_open_snap_entry`), and
    # an at_close rule assessed against it fires hours later.
    dispatcher("_at_open", by_trigger.get("open", []), open_batch=True)
    for m in sorted(before_close_min):
        dispatcher(f"_before_close_{m}", by_trigger.get(f"bc{m}", []))
    for key in at_times:
        dispatcher(f"_{key}", by_trigger.get(key, []))

    return o.code()
