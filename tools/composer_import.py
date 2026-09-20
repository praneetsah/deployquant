"""Composer.trade symphony -> Strategy IR converter.

Translates a Composer symphony tree (the JSON the Composer editor saves) into
a platform IR document: one daily `before_close(1m) -> set_weights` rule whose
weight tree mirrors the symphony node-for-node (spec §13.2).

Semantics follow the validated LEAN port of the same symphony exactly:

- indicators evaluate on daily closes INCLUDING the rebalance-time price
  (`include_today`), RSI is Wilder-smoothed on price diffs (`smoothing`);
- `rhs-fixed-value?` true means the numeric rhs-val wins even when an rhs-fn
  is present; a condition with an unreadable side is False (else branch);
- Composer units are converted to engine units: cumulative-return and
  max-drawdown thresholds are percent (drawdown positive), the engine's
  cum_return/drawdown are fractions (drawdown negative), so both sides are
  wrapped in the matching arithmetic and drawdown rank order is flipped;
- Composer max-drawdown(w) looks at w+1 closes -> engine window w+1.

CLI: python -m tools.composer_import [--selector-window N] [--name NAME]
         [--symphony FILE.json] [--out FILE.json]
defaults to the ComposerWM74_R1 symphony with the adopted selector-window=9
optimization (2026-07-19), writing strategies/composer_wm74.json.
"""
from __future__ import annotations

import argparse
import json
import os

_METRICS = {
    "max-drawdown": "drawdown",
    "moving-average-return": "mean_return",
    "cumulative-return": "cum_return",
    "standard-deviation-return": "realized_vol",
}


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _win(node: dict, side: str):
    fp = node.get(side + "-fn-params") or {}
    w = fp.get("window")
    if w is None:
        w = node.get(side + "-window-days")
    return int(w) if w is not None else None


def _ind_expr(fn: str, ticker, w):
    """Expression for one side of a condition; None -> side unreadable
    (the whole condition becomes literal False, as in the LEAN port)."""
    if not fn or not ticker:
        return None
    if fn == "current-price":
        return {"ind": "price", "field": "last", "symbol": ticker}
    if fn == "relative-strength-index":
        return {"ind": "rsi", "symbol": ticker, "window": w or 14,
                "smoothing": "wilder", "include_today": True}
    if fn == "moving-average-price":
        if w is None:
            return None
        return {"ind": "sma", "symbol": ticker, "window": w,
                "include_today": True}
    if fn == "cumulative-return":
        if w is None:
            return None
        return {"mul": [{"ind": "cum_return", "symbol": ticker, "window": w,
                         "include_today": True}, 100]}
    if fn == "max-drawdown":
        if w is None:
            return None
        return {"mul": [{"ind": "drawdown", "symbol": ticker, "window": w + 1,
                         "include_today": True}, -100]}
    raise NotImplementedError(
        f"condition indicator {fn!r} has no IR mapping yet")


def _cond_expr(c: dict):
    lhs = _ind_expr(c.get("lhs-fn"), c.get("lhs-val"), _win(c, "lhs"))
    if c.get("rhs-fixed-value?"):
        rhs = float(c["rhs-val"]) if _is_num(c.get("rhs-val")) else None
    elif c.get("rhs-fn"):
        rhs = _ind_expr(c["rhs-fn"], c.get("rhs-val"), _win(c, "rhs"))
    else:
        rhs = float(c["rhs-val"]) if _is_num(c.get("rhs-val")) else None
    if lhs is None or rhs is None:
        return False
    op = "gt" if c.get("comparator") == "gt" else "lt"
    return {op: [lhs, rhs]}


class _Converter:
    def __init__(self, selector_window=None):
        self.selector_window = selector_window
        self.assets: set[str] = set()

    @staticmethod
    def _labeled(out: dict, n: dict) -> dict:
        """Carry the Composer group/filter name through as a display label —
        the engine ignores unknown keys; the allocation canvas titles blocks
        with it (a 600-node tree is unnavigable without names)."""
        name = (n.get("name") or "").strip()
        if name:
            out = dict(out)
            out["label"] = name
        return out

    def node(self, n: dict):
        step = n.get("step")
        if step == "root":
            return self.node(n["children"][0])
        if step == "asset":
            t = n["ticker"]
            self.assets.add(t)
            return {"asset": t}
        if step in ("group", "wt-cash-equal"):
            return self._labeled(
                {"equal": [self.node(c) for c in n.get("children", [])]}, n)
        if step == "wt-cash-specified":
            items = []
            for c in n.get("children", []):
                wt = c.get("weight") or {"num": 0, "den": 100}
                den = float(wt["den"]) or 100.0
                frac = float(wt["num"]) / den
                if frac == 0:
                    continue
                items.append({"w": frac, "of": self.node(c)})
            return self._labeled({"weighted": items}, n)
        if step == "wt-inverse-vol":
            return self._labeled({"inverse_vol": {
                "window": int(n.get("window-days") or 20),
                "of": [self.node(c) for c in n.get("children", [])]}}, n)
        if step == "if":
            kids = n["children"]
            cond = next(k for k in kids if not k.get("is-else-condition?"))
            els = next((k for k in kids if k.get("is-else-condition?")), None)
            return {"if": _cond_expr(cond),
                    "then": [self.node(c) for c in cond.get("children", [])],
                    "else": [self.node(c) for c in els.get("children", [])]
                            if els else []}
        if step == "filter":
            return self._filter(n)
        raise NotImplementedError(f"symphony node {step!r} has no IR mapping")

    def _filter(self, n: dict):
        kids = n.get("children", [])
        fn = n.get("sort-by-fn")
        metric = _METRICS.get(fn)
        if metric is None:
            raise NotImplementedError(f"filter sort-by {fn!r} unsupported")
        win = int((n.get("sort-by-fn-params") or {}).get("window", 5))
        # the strategy-rank max-drawdown selector is the tunable root selector
        # (LEAN SELECTOR_WINDOW); asset-rank filters keep their verbatim window
        strategy_rank = not all(k.get("step") == "asset" for k in kids)
        if strategy_rank and fn == "max-drawdown" and self.selector_window:
            win = int(self.selector_window)
        top = n.get("select-fn", "top") == "top"
        # engine drawdown is a NEGATIVE fraction while Composer ranks a
        # positive percentage -> the rank direction flips for drawdown
        if metric == "drawdown":
            top = not top
        return self._labeled({"best": {
            "of": [self.node(c) for c in kids],
            "by": {"metric": metric, "window": win},
            "order": "top" if top else "bottom",
            "n": int(n.get("select-n", 1)),
        }}, n)


def convert_symphony(tree: dict, selector_window=None,
                     name: str = "Composer import") -> dict:
    """Symphony tree -> IR document (one daily set_weights rule)."""
    cv = _Converter(selector_window)
    weights = cv.node(tree)
    return {
        "ir_version": "0.2",
        "meta": {"name": name, "version": 1},
        "params": {},
        "universe": {"static": sorted(cv.assets)},
        "rules": [{
            "id": "daily-rebalance",
            "trigger": {"type": "before_close", "minutes": 1, "days": "all"},
            "action": {"type": "set_weights", "weights": weights},
        }],
    }


def _load_default_symphony():
    """The symphony this converter was validated on, from the authors'
    upstream checkout. Outside it there is no default: pass --symphony."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    src = os.path.join(repo, "qc", "ComposerWM74_R1")
    if not os.path.isfile(os.path.join(src, "symphony.py")):
        raise SystemExit("no default symphony here: pass --symphony FILE "
                         "(the JSON the Composer editor saves)")
    import sys
    sys.path.insert(0, src)
    from symphony import SYMPHONY_JSON        # gzip+base64 self-decoder
    return json.loads(SYMPHONY_JSON)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symphony", help="symphony JSON file "
                    "(default: the ComposerWM74_R1 embedded tree)")
    ap.add_argument("--selector-window", type=int, default=9,
                    help="root max-drawdown selector window override "
                    "(0 = verbatim; default 9, the adopted optimization)")
    ap.add_argument("--name", default="ComposerWM74 Optimized")
    # NOT strategies/ — files there auto-register as PUBLIC templates at API
    # startup; this doc is a personal strategy, created via the API instead
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "composer_wm74.json"))
    args = ap.parse_args()

    if args.symphony:
        with open(args.symphony) as f:
            tree = json.load(f)
    else:
        tree = _load_default_symphony()

    ir = convert_symphony(tree, selector_window=args.selector_window or None,
                          name=args.name)
    out = os.path.normpath(args.out)
    with open(out, "w") as f:
        json.dump(ir, f, indent=1)
        f.write("\n")
    n_assets = len(ir["universe"]["static"])
    print(f"wrote {out}: {n_assets} tradable assets, "
          f"selector_window={args.selector_window}")


if __name__ == "__main__":
    main()
