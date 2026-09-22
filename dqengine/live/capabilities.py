"""The degradation ladder: what happens when a venue cannot express an order.

Every order type a strategy uses resolves, at each venue, to exactly one of:

    NATIVE     the venue takes it as written               -> submit it
    EMULATED   the platform can synthesise it faithfully   -> substitute, say so
    REFUSED    neither                                     -> refuse at DEPLOY

Never silently different, and never pushed onto the strategy author. That
last part is the point of this module. Webull has no retail MARKET_ON_CLOSE,
so `dqengine/examples/tqqq_weekly.py` emulates one by hand — a market order a minute before the
close, written into the user's own algorithm. That sentence does not belong
in a strategy: it is a fact about a venue, and the next broker will have a
different one. A user writes what they mean; the platform decides how to say
it to each venue.

The decisions themselves were already being made — three times, inline, in
broker_exec, each with its own wording and its own fallback. They are here
instead so that:

  * a NEW order type has a declared answer rather than an inline guess;
  * the same substitution is described the same way everywhere it appears;
  * a type with no native support AND no emulation is caught at DEPLOY,
    rather than discovered mid-session when an order does not go out.

Adding a broker whose MOC is a retail feature (vs institutional-only
elsewhere) is then a change to that broker's `Caps`, and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass

from dqengine.adapters import base

NATIVE = "native"
EMULATED = "emulated"
REFUSED = "refused"


@dataclass(frozen=True)
class Resolution:
    mode: str                 # NATIVE | EMULATED | REFUSED
    order_type: str
    note: str = ""            # human line for the journal / deploy refusal

    @property
    def ok(self) -> bool:
        """Can this be traded at all at this venue?"""
        return self.mode in (NATIVE, EMULATED)


# How the PLATFORM synthesises a type no venue-native order exists for.
# Absent from this table means: no emulation exists, so REFUSED.
#
# Each entry says what the engine actually does, because the wording lands
# in the journal and in the deploy dialog — a user must never discover a
# substitution from a fill price.
_EMULATIONS = {
    base.MARKET_ON_CLOSE:
        "emulated as a market order near the close (this broker has no "
        "market-on-close for this account)",
    base.LIMIT_ON_CLOSE:
        "emulated as a limit order near the close (this broker has no "
        "limit-on-close for this account)",
    base.STOP:
        "emulated by the engine — it watches the level and sends a market "
        "order on breach (this broker has no resting stop)",
    base.STOP_LIMIT:
        "emulated by the engine — it watches the level and sends a limit "
        "order on breach (this broker has no resting stop-limit)",
    base.TRAILING_STOP:
        "emulated by the engine — it re-anchors the level each sweep and "
        "sends a market order on breach (this broker has no trailing stop)",
}

# Emulation is ALWAYS a worse fill than the real thing in some scenario, so
# a type that cannot be emulated safely must refuse rather than approximate.
# MARKET and LIMIT are the floor: a venue that cannot do both is unusable,
# and pretending otherwise would mean inventing fills.
_NEVER_EMULATED = frozenset({base.MARKET, base.LIMIT})


# Order types a PYTHON strategy can place but the live layer cannot yet
# project to the executor (the driver's project_orders has no channel for
# them). They resolve to REFUSED regardless of what the venue supports:
# the question is not "can the broker take it" but "would it ever get
# there". Approving one means deploying a strategy that believes it is
# protected by an order nothing sends.
#
# DAILY is the exception, and `daily=True` below lifts it. On daily data an
# at-close order is not an order the platform cannot carry: it is where the
# strategy's backtest fills, and the live payload publishes it in the
# executor's `close_orders` channel a minute before the close. See the
# live driver's daily preview (dqengine.live.driver.engine).
PYTHON_UNTRANSMITTED = frozenset({
    base.MARKET_ON_CLOSE,
    base.LIMIT_ON_CLOSE,
})


def resolve(caps, order_type: str, transmittable: bool = True) -> Resolution:
    """How this venue will express `order_type` — native, emulated, or not
    at all.

    `transmittable=False` short-circuits to REFUSED: the platform cannot
    carry this kind of order to any executor, so the venue's opinion is
    irrelevant.
    """
    if not transmittable:
        return Resolution(
            REFUSED, order_type,
            f"the platform cannot send {order_type} orders from a python "
            f"strategy to a broker yet — the strategy would believe it was "
            f"protected by an order nothing transmits")
    if order_type in caps.order_types:
        return Resolution(NATIVE, order_type)
    if order_type in _NEVER_EMULATED:
        return Resolution(
            REFUSED, order_type,
            f"this broker cannot place {order_type} orders, which the "
            f"platform cannot work around")
    note = _EMULATIONS.get(order_type)
    if note:
        # An entitlement reason replaces the "this broker has no X" clause:
        # saying a venue lacks a feature it demonstrably has is false, and a
        # user whose account could be upgraded deserves to know that is the
        # reason.
        why = caps.entitlement_for(order_type)
        if why:
            head = note.split(" (this broker")[0]
            note = f"{head} ({why} — not available to this account)"
        return Resolution(EMULATED, order_type, note)
    return Resolution(
        REFUSED, order_type,
        f"this broker does not support {order_type} orders and the platform "
        f"has no emulation for them")


def unsupported_for_deploy(caps, order_types, kind: str = "blocks",
                           daily: bool = False) -> list:
    """Every requested type this venue can neither take nor emulate.

    Returned for the DEPLOY path: a strategy pointed at a venue that cannot
    express one of its order types is a deploy-time error, in the same
    family as one that collides with another sleeve's symbols. Discovering
    it when an order fails to go out is too late — by then the strategy
    believes it has a position, or a protection, that does not exist.

    `daily` says the strategy's data is daily, where the live payload DOES
    carry an at-close order to the executor. It lifts PYTHON_UNTRANSMITTED
    and nothing else: the venue's own answer is unchanged, so a broker that
    can neither take nor emulate a type still refuses.
    """
    out = []
    for t in sorted(set(order_types or ())):
        transmittable = not (kind == "python" and not daily
                             and t in PYTHON_UNTRANSMITTED)
        r = resolve(caps, t, transmittable=transmittable)
        if not r.ok:
            out.append(r)
    return out


def journal_note(caps, order_type: str, symbol: str) -> str | None:
    """The line to record when a venue is not taking an order as written.
    None when it is native and there is nothing to say."""
    r = resolve(caps, order_type)
    if r.mode == NATIVE:
        return None
    return f"{order_type} {symbol}: {r.note}"


def ir_order_types(ir: dict) -> set:
    """Every order type an IR document can place.

    Conservative by design: an unrecognised action contributes nothing
    rather than guessing, because the consumer is a deploy-time REFUSAL and
    a false positive blocks a legitimate strategy. A false negative only
    means the substitution is announced at the first sweep instead of at
    deploy — the old behaviour, not a regression.

    Python strategies are not covered: their order types are whatever the
    code calls at runtime, which the manifest pass does not report. That is
    a real gap, recorded in the broker-agnostic spec rather than papered
    over with a guess.
    """
    out: set = set()
    for rule in (ir or {}).get("rules") or []:
        action = rule.get("action") or {}
        atype = action.get("type")
        if atype in ("market_order", "set_weights", "liquidate"):
            out.add(action.get("order_type") or base.MARKET)
        elif atype == "managed_target":
            out.add(action.get("order_type") or base.LIMIT)
        elif atype == "at_close_order":
            out.add(action.get("order_type") or base.MARKET_ON_CLOSE)
        elif atype == "bracket":
            # a bracket is its legs: a protective stop (or trailing stop)
            # and/or a take-profit limit
            if action.get("trail_pct") is not None:
                out.add(base.TRAILING_STOP)
            if action.get("stop") is not None:
                out.add(base.STOP)
            if action.get("take_profit") is not None:
                out.add(base.LIMIT)
    return {t for t in out if t in base.ALL_ORDER_TYPES}


# QCAlgorithm order methods -> the order type they place. PascalCase aliases
# are matched case-insensitively by `python_order_types`, so MarketOrder and
# market_order both resolve.
_PY_ORDER_METHODS = {
    "market_order": base.MARKET,
    "order": base.MARKET,
    "set_holdings": base.MARKET,
    "liquidate": base.MARKET,
    "limit_order": base.LIMIT,
    "stop_market_order": base.STOP,
    "stop_limit_order": base.STOP_LIMIT,
    "trailing_stop_order": base.TRAILING_STOP,
    "limit_if_touched_order": base.LIMIT,
    "market_on_open_order": base.MARKET,
    "market_on_close_order": base.MARKET_ON_CLOSE,
}


def python_order_types(code: str, daily: bool = False) -> set:
    """Every order type a python strategy's SOURCE can place.

    The manifest pass runs initialize() only, and orders are placed at
    runtime, so the manifest cannot answer this — but the source can, the
    same way determinism.screen reads it.

    Conservative in the same direction as `ir_order_types`: an unparseable
    or unrecognised call contributes nothing. A false positive would block a
    legitimate deploy; a false negative only means the substitution is
    announced at the first sweep instead of at deploy, which is the older
    behaviour rather than a regression.

    Deliberately NOT a guarantee. A strategy can reach an order method
    through a name this cannot see (getattr, an alias, a dispatch table), so
    this narrows the deploy-time gap rather than closing it. The executor's
    per-order ladder remains the backstop, which is why that path stays.

    `daily` adds MARKET_ON_CLOSE wherever the source places a MARKET order.
    On daily data a market order placed while the session is open becomes
    one (LEAN's conversion, `OrderBook.market`), and a scan of the source
    sees `market_order` / `set_holdings` / `liquidate`, never the conversion.
    Without this the venue's emulation note would not appear in the deploy
    dialog for the strategies that will actually use it.
    """
    import ast

    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return set()
    out: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None)
        if not name:
            continue
        hit = _PY_ORDER_METHODS.get(_snake(name))
        if hit:
            out.add(hit)
    if daily and base.MARKET in out:
        out.add(base.MARKET_ON_CLOSE)
    return out


def _snake(name: str) -> str:
    """MarketOnCloseOrder -> market_on_close_order. The runtime accepts both
    spellings, so the scan has to see both."""
    if "_" in name or name.islower():
        return name.lower()
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)
