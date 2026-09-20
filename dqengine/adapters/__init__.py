"""Broker adapter interface, catalog and the bundled Alpaca adapter (open
since 2026-09-18, spec §4). Plugins add venues through the
`dqengine.brokers` entry-point group; this package is what they implement.

The roster module is `dqengine.adapters.catalog` (BROKERS, get_adapter,
catalog()); import it as a module -- `from dqengine.adapters import catalog`
-- so a test can patch `dqengine.adapters.catalog.get_adapter` and every
caller sees the patch. Its `catalog()` function is deliberately NOT
re-exported here: a package attribute named `catalog` would shadow the
submodule of the same name, and `from dqengine.adapters import catalog`
would hand callers a function instead of the roster.
"""
from .base import (ALL_ORDER_TYPES, ALL_TIFS, BalanceUnavailable,          # noqa: F401
                   BrokerAdapter, BrokerAuthExpired, BrokerError,
                   BrokerRejected, BrokerUnavailable, Caps,
                   OrderNotSupported, validate_order)
from .catalog import BROKERS, get_adapter                                  # noqa: F401
