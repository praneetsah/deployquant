"""Broker adapter interface: every brokerage speaks this, the executor speaks
only this. Quantities are floats (caps.qty_step says how to round); symbols
are normalized ("SPY", "BTCUSD") — wire-format mapping is the adapter's job.

Normalized Order dict: {id, symbol, qty, side, type, limit_price, status,
client_order_id, stop_price, trail_percent} — side buy|sell, type
market|limit|stop|stop_limit|trailing_stop, client_order_id "" when the
broker has none. stop_price (float|None) is the resting stop/stop-limit
trigger level. trail_percent (float|None) is a PERCENT (e.g. 5.0, not the
fraction 0.05) — the configured trailing-stop offset. Both are None, never
0.0, when the broker's payload doesn't carry the field for that order — the
executor treats None as "unknown" and must not coerce it to zero.
"""
from dataclasses import dataclass, field


class BrokerError(Exception):
    pass


class BrokerUnavailable(BrokerError):
    """Transient — retried next sync cycle."""


class BrokerAuthExpired(BrokerError):
    """Credentials dead (e.g. OAuth refresh token expired) — connection goes
    to reconnect_needed and syncs skip until the user reconnects."""


class BrokerRejected(BrokerError):
    """The broker refused the order — recorded, not retried this sync."""


# migration alias: brokers.py callers catch this name today
BalanceUnavailable = BrokerUnavailable


# ---------------- order vocabulary ----------------
# The normalized names the platform speaks. Each adapter declares the subset
# its broker actually accepts in Caps.order_types / Caps.tifs, and translates
# to the broker's own spelling internally.
MARKET = "market"
LIMIT = "limit"
STOP = "stop"
STOP_LIMIT = "stop_limit"
TRAILING_STOP = "trailing_stop"
MARKET_ON_CLOSE = "market_on_close"
LIMIT_ON_CLOSE = "limit_on_close"

ALL_ORDER_TYPES = (MARKET, LIMIT, STOP, STOP_LIMIT, TRAILING_STOP,
                   MARKET_ON_CLOSE, LIMIT_ON_CLOSE)

# order types that require each price field
NEEDS_LIMIT_PRICE = frozenset({LIMIT, STOP_LIMIT, LIMIT_ON_CLOSE})
NEEDS_STOP_PRICE = frozenset({STOP, STOP_LIMIT})

ALL_TIFS = ("day", "gtc", "ioc", "fok", "opg", "cls")


class OrderNotSupported(BrokerRejected):
    """The broker cannot express this order type / time-in-force. Raised
    before anything is sent, so it never reaches the venue."""


def validate_order(caps, order_type: str, tif: str, limit_price=None,
                   stop_price=None, trail_percent=None, qty: float = 1.0,
                   extended_hours: bool = False, broker_name: str = "broker",
                   opens_short: bool = False):
    """Shared pre-flight check every adapter runs before building a payload.
    Catches unsupported combinations locally with a message that names the
    broker, instead of a cryptic rejection from the venue."""
    if order_type not in caps.order_types:
        raise OrderNotSupported(
            f"{broker_name} does not support {order_type} orders "
            f"(supports: {', '.join(sorted(caps.order_types))})")
    allowed_tifs = caps.tifs_for(order_type)
    if tif not in allowed_tifs:
        raise OrderNotSupported(
            f"{broker_name} does not support time-in-force {tif} "
            f"for {order_type} orders "
            f"(supports: {', '.join(sorted(allowed_tifs))})")
    if extended_hours and not caps.extended_hours:
        raise OrderNotSupported(
            f"{broker_name} does not support extended-hours orders")
    # Caps.supports_short existed from the start and was read by NOTHING —
    # a short would have been built and sent to a venue that cannot take it.
    # Only OPENING a short is gated: buying one back is always allowed, and
    # must be, or a position could never be closed.
    if opens_short and not caps.supports_short:
        raise OrderNotSupported(
            f"{broker_name} does not support short selling")
    if order_type in NEEDS_LIMIT_PRICE and limit_price is None:
        raise OrderNotSupported(f"{order_type} orders require a limit price")
    if order_type in NEEDS_STOP_PRICE and stop_price is None:
        raise OrderNotSupported(f"{order_type} orders require a stop price")
    if order_type == TRAILING_STOP and trail_percent is None:
        raise OrderNotSupported("trailing_stop orders require trail_percent")
    # Schwab (live-verified) and most venues only accept sub-1 quantities on
    # market orders: "Orders with less than 1 shares must be of type MARKET".
    if 0 < abs(qty) < 1 and order_type != MARKET:
        raise OrderNotSupported(
            f"fractional quantities require a market order at {broker_name}")


class ExecutionBatch(list):
    """A list of normalized execution rows that also reports how many raw
    rows the adapter had to SKIP.

    Skipping an unparseable row and returning the rest is the right call at
    the adapter layer — one bad row must not discard a real fill batch. But
    downstream, under `execution_truth=enforce`, the absence of that row on
    a reconciled day is read by `ExecutionLedger.take()` as an affirmative
    "the broker did not fill this". That inverts the rule this whole feature
    rests on: a row we FAILED TO PARSE is unknown, not not-filled. Carrying
    the count lets `executions.poll` surface it and the caller widen the
    unknown set, using the coverage mechanism that already exists.

    It is a real `list`, so every existing caller (`rows or []`, iteration,
    `== []`) is unaffected; a plain list from a third-party adapter reads as
    zero skips via `getattr(rows, "skipped", 0)`.
    """
    def __init__(self, rows=(), skipped: int = 0):
        super().__init__(rows)
        self.skipped = int(skipped)


def normalize_execution(broker_order_id, broker_exec_id, symbol, side, qty,
                        price, filled_at, fees=0.0, client_order_id="",
                        order_level_avg=False) -> dict:
    """Validate and normalize one adapter execution row.

    `qty` is UNSIGNED here and `side` carries the direction — signing happens
    once, at storage time, so an adapter can never accidentally double-negate
    a sell. `filled_at` MUST be timezone-aware: a naive timestamp silently
    becomes a wrong-by-hours fill time in the UI and, worse, orders the
    ledger wrongly against same-day model fills.
    """
    if filled_at.tzinfo is None:
        raise ValueError(f"filled_at must be timezone-aware, got {filled_at!r}")
    s = str(side).lower()
    if s not in ("buy", "sell"):
        raise ValueError(f"side must be buy|sell, got {side!r}")
    return {"broker_order_id": str(broker_order_id or ""),
            "broker_exec_id": str(broker_exec_id),
            "client_order_id": str(client_order_id or ""),
            "symbol": str(symbol).upper(), "side": s,
            "qty": abs(float(qty)), "price": float(price),
            "fees": float(fees or 0.0), "filled_at": filled_at,
            "order_level_avg": bool(order_level_avg)}


@dataclass(frozen=True)
class Caps:
    auth_kind: str = "keys"              # keys | oauth | session
    asset_classes: tuple = ("equity",)
    paper: bool = False                  # paper environment available
    supports_replace: bool = True        # PATCH/PUT-style order modify
    supports_client_order_id: bool = True
    supports_short: bool = True
    qty_step: float = 1.0                # 1.0 whole shares, 0.0001 crypto
    # what this broker can actually express — surfaced through
    # /api/brokers/catalog so the UI can show it per broker
    order_types: frozenset = frozenset({MARKET, LIMIT})
    tifs: frozenset = frozenset({"day", "gtc"})
    # Per-ORDER-TYPE narrowing of `tifs`. A venue that takes GTC generally
    # may still refuse it for one type -- Webull accepts GTC but a
    # TRAILING_STOP_LOSS only in DAY. Absent an entry, `tifs` applies.
    #
    # This is the shape every broker difference should take: DATA describing
    # what a venue can do, not a branch in the executor. Adding a broker
    # whose MOC is a retail feature (vs institutional-only elsewhere) should
    # be one line here, not a code path.
    tif_by_type: dict = field(default_factory=dict)
    # WHY a type is absent from `order_types`, when the reason is the
    # ACCOUNT rather than the venue. Webull's MARKET_ON_CLOSE is real and
    # documented, just institutional-only — so "this broker has no MOC" is
    # false, and a user who upgrades their account would find it appears.
    # Maps order_type -> a short human reason. Only meaningful for types NOT
    # in `order_types`; it explains an absence, it never creates support.
    entitlement_by_type: dict = field(default_factory=dict)
    extended_hours: bool = False         # pre/post-market session routing

    def tifs_for(self, order_type: str) -> frozenset:
        """The time-in-force values this venue accepts FOR THIS TYPE."""
        return self.tif_by_type.get(order_type, self.tifs)

    def entitlement_for(self, order_type: str):
        """Why this type is unavailable, when the reason is the account and
        not the venue. None when the venue simply does not offer it."""
        if order_type in self.order_types:
            return None
        return self.entitlement_by_type.get(order_type)


class BrokerAdapter:
    id = ""
    name = ""
    caps = Caps()

    def ensure_session(self, creds: dict):
        """Called once per sync before any other method. Refresh tokens /
        keep sessions alive here. Return an updated creds dict to have it
        re-encrypted and stored; return None if nothing changed. Raise
        BrokerAuthExpired when the user must re-authorize."""
        return None

    def fetch_balance(self, creds: dict) -> dict:
        """-> {equity, cash, buying_power, history: {days, values},
        account_label, currency, fetched_at}"""
        raise NotImplementedError

    def positions(self, creds: dict) -> dict:
        """-> {normalized_symbol: signed float qty} for all nonzero positions."""
        raise NotImplementedError

    def positions_detail(self, creds: dict) -> list:
        """-> [{symbol, qty, market_value, unit_cost}] — everything the broker
        says about each holding, including ITS OWN mark.

        positions() intentionally returns quantities only: the platform values
        sleeves with the same prices the strategy trades on, so a broker's mark
        must never leak into that path. This is the diagnostic view, for
        comparing the two when a number looks off by a few cents. Default
        implementation just reports quantities with no marks."""
        return [{"symbol": s, "qty": q, "market_value": None,
                 "unit_cost": None} for s, q in self.positions(creds).items()]

    def open_orders(self, creds: dict) -> list:
        """-> [Order dict, ...] for all open/working orders."""
        raise NotImplementedError

    def executions(self, creds: dict, since=None) -> list:
        """-> [normalize_execution(...) row, ...] for fills at or after
        `since` (a tz-aware datetime, or None for the broker's default
        window). ONE ROW PER EXECUTION where the broker reports that
        granularity; brokers that only expose order-level averages set
        order_level_avg=True.

        The default returns [] — a broker with no implementation stays
        model-priced and is LABELED as such in the UI. Never synthesize a
        row from a submitted order: an unconfirmed fill is unknown, not
        filled (see the execution-truth spec §6a)."""
        return []

    def submit(self, creds: dict, symbol: str, qty: float, side: str,
               order_type: str = "market", tif: str = "day",
               limit_price: float = None, stop_price: float = None,
               trail_percent: float = None, extended_hours: bool = False,
               client_order_id: str = None,
               opens_short: bool = False) -> dict:
        """order_type is one of ALL_ORDER_TYPES; adapters raise
        OrderNotSupported for anything outside their Caps.order_types.

        `opens_short` says this sell OPENS exposure rather than reducing it.
        Venues distinguish the two (Webull: SELL vs SHORT), and
        Caps.supports_short gates only the opening case — buying back must
        always be allowed, or a position could never be closed."""
        raise NotImplementedError

    def replace(self, creds: dict, order_id: str, qty: float = None,
                limit_price: float = None) -> dict:
        raise NotImplementedError

    def cancel(self, creds: dict, order_id: str) -> None:
        raise NotImplementedError
