"""Live market-data feeds (spec 2026-09-18 §6).

`dqengine.feed` is history; this is the live tape. One protocol
(`QuoteFeed`), one bundled implementation (the Alpaca v2 websocket), the
silence check that says when a stream has gone quiet, and the REST
backfill a worker falls back to when it has.

    from dqengine.feeds import get_feed, MinuteZipStore
    store = MinuteZipStore("./data")
    feed = get_feed("alpaca", store=store, on_bar=print, symbols=["TQQQ"])
    while True:
        feed.poll(timeout=1.0)

Feeds are discovered through the `dqengine.feeds` entry-point group, the
same way broker adapters are.
"""
from dqengine.feeds.backfill import backfill_day
from dqengine.feeds.base import (BarRefresher, BarStore, FeedState, MinuteBar,
                                 MinuteZipStore, QuoteBoard, QuoteFeed,
                                 QuoteTick, Socket, bar_is_sane,
                                 bar_refresher, in_regular_session,
                                 usable_snapshot)
from dqengine.feeds.health import (FeedHealth, SilencePolicy, check_feed,
                                   market_is_open, session_open_epoch)
# `registry()` itself is deliberately not re-exported: the name would
# shadow the dqengine.feeds.registry module on this package.
from dqengine.feeds.registry import (BadFeed, FeedConflict, UnknownFeed,
                                     available, get_feed, load_class)

__all__ = ["BarRefresher", "BarStore", "FeedState", "MinuteBar",
           "MinuteZipStore", "QuoteBoard",
           "QuoteFeed", "QuoteTick", "Socket", "bar_is_sane", "bar_refresher",
           "in_regular_session",
           "usable_snapshot",
           "FeedHealth", "SilencePolicy", "check_feed", "market_is_open",
           "session_open_epoch", "backfill_day", "BadFeed", "FeedConflict",
           "UnknownFeed", "available", "get_feed", "load_class"]
