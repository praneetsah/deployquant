"""The LEAN live-trading brokerage roster. Every broker is a catalog entry the
UI can render; only entries marked `implemented` are connectable, and those
resolve to a class through the plugin loader (dqengine.brokers) -- the roster
itself imports no concrete adapter. Source: LEAN docs 'Live Trading >
Brokerages' (2026-08)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    implemented: bool = False           # an adapter is installed under this id (loader-resolved)
    auth_kind: str = "keys"
    asset_classes: tuple = ("equity",)
    paper: bool = False
    notes: str = ""


BROKERS = {e.id: e for e in [
    Entry("alpaca", "Alpaca", True, "keys", ("equity",), True),
    Entry("webull", "Webull", True, "keys", ("equity",), False,
          "live-money only — Webull's US OpenAPI has no working paper env"),
    Entry("schwab", "Charles Schwab", True, "oauth", ("equity",), False,
          "live-money only — no paper environment"),
    Entry("ibkr", "Interactive Brokers", False, "session",
          ("equity", "options", "futures", "forex", "cfd")),
    Entry("tradestation", "TradeStation", False, "oauth",
          ("equity", "options", "futures"), True),
    Entry("tastytrade", "Tastytrade", False, "oauth",
          ("equity", "options", "futures"), True),
    Entry("public", "Public", False, "keys", ("equity", "options", "crypto")),
    Entry("tradier", "Tradier", False, "oauth", ("equity", "options"), True),
    Entry("binance", "Binance", False, "keys", ("crypto",), True),
    Entry("bybit", "Bybit", False, "keys", ("crypto",)),
    Entry("kraken", "Kraken", False, "keys", ("crypto",)),
    Entry("coinbase", "Coinbase", False, "keys", ("crypto",)),
    Entry("bitfinex", "Bitfinex", False, "keys", ("crypto",)),
    Entry("dydx", "dYdX", False, "keys", ("crypto",)),
    Entry("bloomberg_emsx", "Bloomberg EMSX", False, "session",
          ("equity", "options", "futures")),
    Entry("eze", "SS&C Eze", False, "session", ("equity", "options", "futures")),
    Entry("trading_technologies", "Trading Technologies", False, "session",
          ("futures",)),
    Entry("wolverine", "Wolverine", False, "session", ("equity",)),
    Entry("fix", "FIX Connection", False, "session",
          ("equity", "options", "futures"),
          ),
]}


def get_adapter(broker_id: str):
    """An adapter INSTANCE for a catalog id. Resolution goes through the
    entry-point loader (dqengine.brokers), so a venue is available exactly
    when its distribution is installed — the platform and a self-hoster
    get the same answer from the same door.

    The roster is not the list of ids that can be traded. It is the list of
    LEAN brokerages, which is what the platform's broker dropdown renders;
    `alpaca-paper` is installed, connectable and the CLI's default, and it
    is not a LEAN brokerage. An id the roster does not carry is therefore
    asked of the loader rather than refused here — the loader is the one
    door that knows what is installed. Adding the id to the roster instead
    would put an "Alpaca (paper)" row in front of every platform user, whose
    Alpaca connection already carries paper or live in its credentials."""
    from dqengine import brokers
    e = BROKERS.get(broker_id)
    if e is None:
        return brokers.load(broker_id)     # UnknownBroker names what is installed
    if not e.implemented:
        raise LookupError(f"{e.name} adapter not implemented yet")
    try:
        return brokers.load(broker_id)
    except brokers.UnknownBroker as exc:
        raise LookupError(f"{e.name}: {exc}") from exc


def adapter_class(broker_id: str):
    from dqengine import brokers
    return brokers.load_class(broker_id)


def catalog() -> list:
    """One row per LEAN broker. Implemented brokers also carry what they can
    actually execute (order types / time-in-force / extended hours), read
    straight off the adapter's Caps so the UI can show per-broker support
    without a second list to keep in sync. An implemented id whose plugin
    distribution is not installed here advertises nothing."""
    from dqengine import brokers
    rows = []
    for e in BROKERS.values():
        row = {"id": e.id, "name": e.name, "auth_kind": e.auth_kind,
               "asset_classes": list(e.asset_classes), "paper": e.paper,
               "notes": e.notes,
               "order_types": [], "tifs": [], "extended_hours": False}
        if e.implemented:
            try:
                caps = adapter_class(e.id).caps
            except brokers.UnknownBroker:      # plugin not installed here
                # only ABSENCE is quiet: an installed-but-broken plugin
                # (BadAdapter, BrokerConflict) must fail loud, not advertise
                # nothing and look merely uninstalled (Ruling 13)
                caps = None
        else:
            caps = None
        if caps is not None:
            row["order_types"] = sorted(caps.order_types)
            row["tifs"] = sorted(caps.tifs)
            row["extended_hours"] = caps.extended_hours
        row["implemented"] = e.implemented
        rows.append(row)
    return rows
