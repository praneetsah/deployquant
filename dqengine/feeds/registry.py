"""Feed plugin loader — the same door broker adapters come through.

Feeds are discovered from the `dqengine.feeds` entry-point group, so a
venue that ships its own streamer (`pip install deployquant-<venue>`)
declares one entry point and this module finds it. The bundled Alpaca
websocket feed is registered in that group too (see pyproject.toml), with
BUILTIN below as the fallback for a source checkout that was never
`pip install -e`'d — same ids, same classes, so behaviour does not depend
on how the package was installed.

Rules, each because the alternative fails quietly: an unknown id raises
and names what is installed; one id claimed by two distributions raises
rather than picking by import order; a class that does not look like a
QuoteFeed is refused before any credential touches it.
"""
from __future__ import annotations

from importlib import import_module
from importlib.metadata import EntryPoint, entry_points

GROUP = "dqengine.feeds"

BUILTIN = {
    "alpaca": "dqengine.feeds.alpaca:AlpacaQuoteFeed",
}

# the pump, the symbol set, the verdict, the exit
REQUIRED_METHODS = ("subscribe", "unsubscribe", "poll", "health", "close")


class UnknownFeed(LookupError):
    pass


class FeedConflict(RuntimeError):
    pass


class BadFeed(TypeError):
    pass


def _installed() -> list:
    """Separated so tests inject their own entry points."""
    return list(entry_points(group=GROUP))


def registry(eps: list | None = None) -> dict:
    """id -> [EntryPoint | dotted 'module:Class', ...]. More than one
    element is a conflict, reported when that id is loaded rather than on
    listing, so one bad plugin cannot hide every other feed."""
    table: dict = {}
    for ep in (_installed() if eps is None else eps):
        table.setdefault(ep.name, []).append(ep)
    for name, target in BUILTIN.items():
        if name not in table:
            table[name] = [target]
    return table


def available(eps: list | None = None) -> list:
    return sorted(registry(eps))


def _resolve(spec):
    if isinstance(spec, EntryPoint):
        return spec.load()
    mod, _, attr = spec.partition(":")
    return getattr(import_module(mod), attr)


def _check_shape(feed_id: str, cls) -> None:
    if not isinstance(cls, type):
        raise BadFeed(f"{feed_id!r} resolved to {cls!r}, not a class")
    missing = [m for m in REQUIRED_METHODS if not callable(getattr(cls, m, None))]
    if missing:
        raise BadFeed(f"{feed_id!r} ({cls.__module__}.{cls.__name__}) is "
                      f"missing feed methods: {missing}")


def load_class(feed_id: str, eps: list | None = None) -> type:
    table = registry(eps)
    specs = table.get(feed_id)
    if not specs:
        hint = (f"; try `pip install deployquant-{feed_id}`"
                if feed_id and "-" not in feed_id else "")
        raise UnknownFeed(f"no feed {feed_id!r} is installed "
                          f"(installed: {', '.join(sorted(table)) or 'none'}){hint}")
    if len(specs) > 1:
        owners = sorted({(getattr(s.dist, "name", None) or "?") if isinstance(s, EntryPoint)
                         else "builtin" for s in specs})
        raise FeedConflict(f"feed {feed_id!r} is claimed by more than one "
                           f"installed distribution: {owners}; uninstall one")
    cls = _resolve(specs[0])
    _check_shape(feed_id, cls)
    return cls


def get_feed(name: str, eps: list | None = None, **cfg):
    """A feed INSTANCE for `name`, configured with `cfg`."""
    return load_class(name, eps)(**cfg)
