"""One process that trades one deployment: the composition, and nothing else.

Everything this module starts already exists. The driver ticks
(`dqengine.live.driver.loop`), the executor sends (`dqengine.live.executor`),
the consumers carry the order signal between them
(`dqengine.live.consumer`), and the feed turns a wire frame into a stored bar
and a bus event (`dqengine.live.feed_runner`). What was missing was the
process that holds all four at once, because on a hosted platform that
process is the api and the worker fleet.

The order below is the order the hosted stack uses, and each step is here
because the one after it reads what it set:

  1. the ports, so a tick has rows and bars to reach for;
  2. the pyrunner import, so a misconfigured engine mode refuses at start
     rather than on the first trade;
  3. the bus singleton, so the feed's publish and the warm engine's stale
     marker both find it;
  4. the two consumers, so an intent published by a tick reaches the
     executor;
  5. the feed, so bars arrive;
  6. the worker loop, in the calling thread, which owns the exit code.

A fleet adds a supervisor above this and a combiner beside it. One
deployment needs neither.
"""
from __future__ import annotations

import threading


def install_ports(bars=None, fallback: str | None = None) -> None:
    """Install this process's implementations of the driver's three ports.

    `bars` left out is the single-account composition: the history over one
    Alpaca account (`bar_source.default_bar_source`), and today's minutes
    over `fallback` -- the feed whose REST bars fill in while the live feed
    is quiet. A host that pays for its own market data passes its own bar
    source instead and neither argument matters."""
    from dqengine.live import bar_source, deployment_store
    from dqengine.live.driver import ports
    from dqengine.live.driver.loop import BusIntentSink
    if bars is None:
        bars = bar_source.default_bar_source(
            feed=None if fallback is None else rest_feed(fallback))
    ports.configure(store=deployment_store.SqlDeploymentStore(),
                    bars=bars, sink=BusIntentSink)


def rest_feed(name: str):
    """A feed object built for its REST bars alone: no store, no callbacks,
    no symbols, and no socket -- a feed connects inside `poll`, which this
    one is never asked for.

    It is a second object rather than the streaming one because the two
    jobs are separate: the stream that went quiet is the reason the REST
    call is being made, and `--fallback` may name a different vendor
    entirely."""
    from dqengine.feeds import get_feed
    return get_feed(name)


def build_feed(name: str, symbols, *, bus=None, quotes=None, status=None):
    """A named feed wired to the bar store and the runner around it.

    Returns (feed, runner). The two callbacks are the runner's own: the
    store write has already committed by the time `on_bar` runs, so
    publishing the bar there is safe, and that ordering is the feed
    protocol's, not this function's."""
    from dqengine.feeds import get_feed
    from dqengine.live.feed_runner import BarDayStore, FeedRunner
    box: dict = {}
    feed = get_feed(name,
                    store=BarDayStore(source=f"{name}-stream"),
                    on_bar=lambda bar: box["runner"].on_bar(bar),
                    on_quote=lambda tick: box["runner"].on_quote(tick),
                    symbols=[s.upper() for s in symbols])
    runner = FeedRunner(feed, quotes=quotes, status=status, bus=bus)
    box["runner"] = runner
    return feed, runner


class LiveSession:
    """The four background pieces, started together and stoppable together.

    Every thread it starts is asked to stop through an event it owns, so a
    signal handler (or a test) can take the process down without leaving a
    socket open or a consumer mid-sweep."""

    def __init__(self, dep_id: str, conn_id: str, *, feed=None,
                 feed_name: str = "alpaca", fallback_name: str | None = None,
                 symbols=(), bus=None, bars=None,
                 feed_timeout: float = 1.0):
        self.dep_id = dep_id
        self.conn_id = conn_id
        self.feed_name = feed_name
        # the REST bars a silent stream falls back to come from the same
        # vendor unless the caller named another one
        self.fallback_name = fallback_name or feed_name
        self.symbols = [s.upper() for s in symbols]
        self.bus = bus
        self.bars = bars
        self.feed = feed
        self.runner = None
        self.quotes: dict = {}
        self.status: dict = {}
        self._feed_timeout = feed_timeout
        self._stop = threading.Event()
        self._threads: list = []

    # ---- start ----------------------------------------------------------

    def start(self, consumers: bool = True, feed: bool = True) -> None:
        from dqengine.feeds import FeedState
        from dqengine.live import bus as bus_mod
        install_ports(self.bars, fallback=self.fallback_name)
        # the engine mode is read at pyrunner's import: reach it here, at
        # start, so `inproc` without its acknowledgement refuses the process
        # instead of the first tick
        from dqengine.sandbox import pyrunner                 # noqa: F401
        if self.bus is None:
            self.bus = bus_mod.bus_from_env()
        if self.bus is None:
            raise RuntimeError(
                "REDIS_URL is not set: the tick, the order path and the feed "
                "all meet on the bus, so there is nothing to start")
        # the warm engine's stale marker and the feed's bar publish read the
        # module singleton, not this object
        bus_mod.BUS = self.bus
        if consumers:
            self._start_consumers()
        if feed:
            self._start_feed(FeedState)

    def _start_consumers(self) -> None:
        from dqengine.live.consumer import (start_intent_consumer,
                                            start_sync_consumer)
        stop = self._stop.is_set
        self._threads.append(start_sync_consumer(self.bus, stop=stop))
        self._threads.append(start_intent_consumer(self.bus, stop=stop))

    def _start_feed(self, FeedState) -> None:
        if self.feed is None:
            self.feed, self.runner = build_feed(
                self.feed_name, self.symbols, bus=self.bus,
                quotes=self.quotes, status=self.status)
        else:
            from dqengine.live.feed_runner import FeedRunner
            self.runner = FeedRunner(self.feed, quotes=self.quotes,
                                     status=self.status, bus=self.bus)
        self.status.update(FeedState().as_status())
        t = threading.Thread(
            target=self.runner.run, name="feed", daemon=True,
            kwargs={"stop": self._stop.is_set, "timeout": self._feed_timeout})
        t.start()
        self._threads.append(t)

    # ---- run and stop ---------------------------------------------------

    def worker(self):
        """The loop object this session runs. Exposed so a caller can step
        it once -- the same object `run` hands to the driver."""
        from dqengine.live.driver.loop import WorkerLoop
        return WorkerLoop(self.dep_id, self.bus)

    def run(self) -> int:
        """The worker loop, in the calling thread. Its return value is the
        process's exit code (see `driver.loop`)."""
        from dqengine.live.driver import loop
        return loop.run(self.dep_id, bus=self.bus)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []
        if self.feed is not None:
            try:
                self.feed.close()
            except Exception as e:                          # noqa: BLE001
                print(f"[live] closing the feed failed: {e!r}", flush=True)
