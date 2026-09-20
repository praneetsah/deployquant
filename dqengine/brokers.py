"""Broker plugin loader.

Adapters are discovered from the `dqengine.brokers` entry-point group, so
`pip install deployquant-webull` is the whole installation step: the plugin's
pyproject declares `webull = "dqengine_webull:WebullAdapter"` and this
module finds it. The engine's own bundled adapters are registered in the
same group (see pyproject.toml), and BUILTIN below is the fallback for a
source checkout that was never `pip install -e`'d -- same ids, same
classes, so behaviour does not depend on how the package was installed.

Rules, each because the alternative fails quietly:

  * an unknown id raises UnknownBroker and NAMES what is installed, with
    the pip hint -- never returns None;
  * the same id claimed by two installed distributions raises
    BrokerConflict at load time rather than picking one by import order;
  * a loaded class must look like a BrokerAdapter (caps + the five
    methods the driver calls) or it is refused as BadAdapter before any
    credentials touch it. Duck-typed on purpose: the interface module is
    dqengine.adapters.base; the shape check stays duck-typed so a plugin
    needs no import just to be recognised.
"""
from __future__ import annotations

from importlib import import_module
from importlib.metadata import EntryPoint, entry_points

GROUP = "dqengine.brokers"

BUILTIN = {
    "alpaca": "dqengine.adapters.alpaca:AlpacaAdapter",
    "alpaca-paper": "dqengine.adapters.alpaca_paper:AlpacaPaperAdapter",
    # webull / schwab arrive via their plugin distributions' entry points
}

# what the reference driver calls; a plugin missing one of these would
# fail on the first tick that needed it, which may be the first order
REQUIRED_METHODS = ("positions", "open_orders", "submit", "cancel",
                    "fetch_balance")
REQUIRED_CAPS = ("order_types", "tifs", "qty_step", "supports_short")


class UnknownBroker(LookupError):
    pass


class BrokerConflict(RuntimeError):
    pass


class BadAdapter(TypeError):
    pass


def _installed() -> list[EntryPoint]:
    """Separated so tests inject their own entry points."""
    return list(entry_points(group=GROUP))


def registry(eps: list[EntryPoint] | None = None) -> dict[str, list]:
    """id -> [EntryPoint | dotted 'module:Class' string, ...]. More than one
    element means a conflict, reported when that id is loaded, not on
    listing, so one bad plugin cannot hide every other broker."""
    table: dict[str, list] = {}
    for ep in (_installed() if eps is None else eps):
        table.setdefault(ep.name, []).append(ep)
    for name, target in BUILTIN.items():
        if name not in table:
            table[name] = [target]
    return table


def available(eps: list[EntryPoint] | None = None) -> list[str]:
    return sorted(registry(eps))


def _resolve(spec) -> type:
    if isinstance(spec, EntryPoint):
        return spec.load()
    mod, _, attr = spec.partition(":")
    return getattr(import_module(mod), attr)


def _check_shape(broker_id: str, cls) -> None:
    if not isinstance(cls, type):
        raise BadAdapter(f"{broker_id!r} resolved to {cls!r}, not a class")
    caps = getattr(cls, "caps", None)
    missing_caps = [c for c in REQUIRED_CAPS if not hasattr(caps, c)]
    if caps is None or missing_caps:
        raise BadAdapter(f"{broker_id!r} ({cls.__module__}.{cls.__name__}) "
                         f"declares no usable caps (missing: {missing_caps})")
    missing = [m for m in REQUIRED_METHODS if not callable(getattr(cls, m, None))]
    if missing:
        raise BadAdapter(f"{broker_id!r} ({cls.__module__}.{cls.__name__}) "
                         f"is missing adapter methods: {missing}")


def load_class(broker_id: str, eps: list[EntryPoint] | None = None) -> type:
    table = registry(eps)
    specs = table.get(broker_id)
    if not specs:
        hint = (f"; try `pip install deployquant-{broker_id}`"
                if broker_id and "-" not in broker_id else "")
        raise UnknownBroker(f"no broker {broker_id!r} is installed "
                            f"(installed: {', '.join(sorted(table)) or 'none'}){hint}")
    if len(specs) > 1:
        owners = sorted({(getattr(s.dist, "name", None) or "?") if isinstance(s, EntryPoint)
                         else "builtin" for s in specs})
        raise BrokerConflict(f"broker {broker_id!r} is claimed by more than one "
                             f"installed distribution: {owners}; uninstall one")
    cls = _resolve(specs[0])
    _check_shape(broker_id, cls)
    return cls


def load(broker_id: str, eps: list[EntryPoint] | None = None):
    """An adapter INSTANCE for `broker_id`."""
    return load_class(broker_id, eps)()
