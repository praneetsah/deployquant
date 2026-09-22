"""DQengine's live order path, timed bar by bar.

    python run_dqengine_live.py ALGORITHM.py --data DATA_ROOT [--day YYYY-MM-DD]

One process holds everything `dqengine live` holds: the ports, the feed
runner, the bus, the intent consumer, the worker loop and the executor. The
three things a benchmark may not use are replaced and nothing else is:

  * the market -- a scripted feed replays one real session's minute bars out
    of the local bar store, one bar per poll, as fast as the path will take
    them;
  * the venue -- a fake adapter that records the moment `submit` is called
    and returns an acknowledgement;
  * Redis -- an in-process bus, unless --redis is given, in which case the
    real client is used against REDIS_URL.

Everything between them is the shipped code. Three spans are recorded per
bar, all on `time.perf_counter`:

  bar -> tick      the bar event is published, through the worker's read,
                   the warm engine's step and the payload write, to the tick
                   returning;
  intent -> sweep  the intent is published, through the consumer, to the
                   executor's fast reconcile returning;
  bar -> submit    the same bar to the order reaching the venue. Only the
                   bars whose tick produced an order have one.

Two things are moved so the run can happen outside market hours, and both
change WHEN, never what:

  * the clock the engine and the worker loop read is set to the bar being
    delivered. Both already take an injected clock. Without it the engine
    will not open a session that has not started, so every tick steps
    nothing and the numbers are of an empty loop;
  * the session being replayed is removed from the history store (the rest
    of it is linked through). A store that already holds that day hands the
    whole session to the first tick, the strategy reaches its closing
    position there, and no later bar can change its mind.

Needs a Postgres of its own: it drops and rebuilds the schema. Point
DQENGINE_BENCH_DATABASE_URL at a scratch database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta



# --------------------------------------------------------------- the bus

class InProcBus:
    """Redis streams and keys in a dict, and a clock on every publish.

    Same surface the driver, the consumers and the feed runner ask of the
    real bus. `stamps` is what the benchmark reads: the perf_counter of the
    most recent publish on each stream."""

    def __init__(self, idle_s: float = 0.002):
        self.lock = threading.Lock()
        self.streams: dict = {}
        self.cursors: dict = {}
        self.keys: dict = {}
        self.idle_s = idle_s
        self.stamps: dict = {}
        self._seq = 0

    def publish(self, stream: str, fields: dict) -> str:
        at = time.perf_counter()
        with self.lock:
            self._seq += 1
            eid = f"{self._seq}-0"
            self.streams.setdefault(stream, []).append(
                (eid, {k: str(v) for k, v in fields.items()}))
            self.stamps[stream] = at
            return eid

    def ensure_group(self, stream: str, group: str) -> None:
        with self.lock:
            self.streams.setdefault(stream, [])
            self.cursors.setdefault((stream, group), 0)

    def read(self, stream: str, group: str, consumer: str,
             block_ms: int = 1000, count: int = 100) -> list:
        with self.lock:
            entries = self.streams.setdefault(stream, [])
            at = self.cursors.setdefault((stream, group), 0)
            out = entries[at:at + count]
            self.cursors[(stream, group)] = at + len(out)
        if not out:
            time.sleep(min(self.idle_s, max(block_ms, 0) / 1000.0))
        return out

    def ack(self, stream: str, group: str, *ids) -> None:
        pass

    def set_ex(self, key: str, value: str, ex_s: int) -> None:
        with self.lock:
            self.keys[key] = value

    def get(self, key: str):
        with self.lock:
            return self.keys.get(key)

    def delete(self, key: str) -> None:
        with self.lock:
            self.keys.pop(key, None)


class TimedBus:
    """The real Redis bus with the same publish clock on it."""

    def __init__(self, inner):
        self.inner = inner
        self.stamps: dict = {}

    def publish(self, stream: str, fields: dict) -> str:
        at = time.perf_counter()
        eid = self.inner.publish(stream, fields)
        self.stamps[stream] = at
        return eid

    def __getattr__(self, name):
        return getattr(self.inner, name)


# -------------------------------------------------------------- the feed

class ScriptedFeed:
    """One frame per poll, off a list set on the class -- the loader builds
    the instance, so the script has to reach it through the class."""

    SCRIPT: list = []

    def __init__(self, store=None, on_bar=None, on_quote=None, symbols=(),
                 **_cfg):
        from dqengine.feeds.base import FeedState
        self.store = store
        self.on_bar = on_bar
        self.state = FeedState(connected=True)
        self.quotes: dict = {}
        self.script = list(self.SCRIPT)
        self.closed = False
        self._symbols = {s.upper() for s in symbols}
        self.state.symbols = tuple(sorted(self._symbols))
        self.released = threading.Semaphore(0)

    @property
    def symbols(self) -> list:
        return sorted(self._symbols)

    def subscribe(self, symbols) -> None:
        self._symbols |= {s.upper() for s in symbols}
        self.state.symbols = tuple(sorted(self._symbols))

    def unsubscribe(self, symbols) -> None:
        self._symbols -= {s.upper() for s in symbols}

    def poll(self, timeout: float = 1.0) -> None:
        """Blocks until the benchmark releases the next bar, so exactly one
        bar is in flight at a time and every span belongs to one bar."""
        if not self.released.acquire(timeout=min(timeout, 0.5)):
            return
        if not self.script:
            return
        bar = self.script.pop(0)
        self.state.last_frame_at = time.time()
        changed = self.store.write([bar]) if self.store is not None else [bar]
        for b in changed:
            self.state.last_bar_at = time.time()
            if self.on_bar is not None:
                self.on_bar(b)

    def health(self, *a, **k):
        return self.state

    def close(self) -> None:
        self.closed = True
        self.state.connected = False


# ------------------------------------------------------------- the venue

class TimingBroker:
    """Records when the order path reached it, and fills at once.

    A venue that acknowledges and never fills is not a neutral stand-in: the
    order journal holds the first order open for ever and every later sweep
    refuses, so the path under test stops being exercised after one bar. A
    market order here therefore moves the position immediately, which is what
    a liquid ETF's market order does."""

    def __init__(self):
        from dqengine.adapters.base import Caps
        self.caps = Caps()
        self.submits: list = []
        self.pos: dict = {}
        self._seq = 0

    def ensure_session(self, creds):
        return None

    def fetch_balance(self, creds):
        return {"cash": 1e9, "equity": 1e9}

    def positions(self, creds):
        return dict(self.pos)

    def positions_detail(self, creds):
        return []

    def open_orders(self, creds):
        return []

    def executions(self, creds, since=None):
        return []

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        self.submits.append(time.perf_counter())
        self._seq += 1
        signed = float(qty) * (1.0 if side == "buy" else -1.0)
        if order_type == "market":
            self.pos[symbol] = self.pos.get(symbol, 0.0) + signed
        return {"id": f"o{self._seq}", "symbol": symbol, "qty": float(qty),
                "side": side, "type": order_type,
                "status": "filled" if order_type == "market" else "new",
                "limit_price": limit_price, "stop_price": stop_price,
                "client_order_id": client_order_id or ""}

    def replace(self, creds, order_id, qty=None, limit_price=None):
        self.submits.append(time.perf_counter())
        return {"id": order_id, "qty": qty, "limit_price": limit_price,
                "status": "new"}

    def cancel(self, creds, order_id):
        return None


# --------------------------------------------------------------- the bars

def session_bars(data_root: str, symbol: str, day: date, limit: int,
                 stamp_as: date | None = None):
    """One session's regular-hours minute bars out of the local store, as
    the feed's own bar objects.

    `stamp_as` re-dates them onto the session the engine is currently in.
    A live engine steps TODAY's bars and treats everything before it as
    settled history, so bars carrying a past date are read as history and
    the replay never moves: same numbers, one tick, nothing measured."""
    from dqengine.feeds.base import MinuteBar
    from dqengine.runtime.core.data import DataStore
    bars = DataStore(data_root).load_minute_day(symbol, day)
    if bars is None or not bars.n:
        raise SystemExit(f"no minute bars for {symbol} on {day} under "
                         f"{data_root}: run `dqengine data fetch {symbol} "
                         f"--from {day}`")
    out = []
    for i in range(bars.n):
        ms = int(bars.start_ms[i])
        if ms < 9 * 3600_000 + 30 * 60_000 or ms >= 16 * 3600_000:
            continue
        out.append(MinuteBar(symbol=symbol, day=stamp_as or day, start_ms=ms,
                             open=float(bars.open[i]), high=float(bars.high[i]),
                             low=float(bars.low[i]), close=float(bars.close[i]),
                             volume=float(bars.volume[i])))
    return out[:limit]


def history_store(src: str, dst: str, symbol: str, day: date):
    """A bar store holding every day of `symbol` STRICTLY BEFORE `day`.

    The replayed session has to be absent from it. A store that already
    holds the day being replayed hands the whole session to the first
    tick: the strategy reaches its closing position there and every later
    bar is a no-op, which measures nothing. Links, not copies, so a
    five-year history costs nothing."""
    from dqengine.store import minute_dir
    # absolute, or the link resolves from the destination directory
    sdir = minute_dir(os.path.abspath(src), symbol)
    ddir = minute_dir(dst, symbol)
    os.makedirs(ddir, exist_ok=True)
    cut = day.strftime("%Y%m%d")
    n = 0
    for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
        if not name.endswith("_trade.zip") or name[:8] >= cut:
            continue
        link = os.path.join(ddir, name)
        if not os.path.exists(link):
            os.symlink(os.path.join(sdir, name), link)
        n += 1
        last = name[:8]
    if not n:
        raise SystemExit(f"no bars for {symbol} before {day} under {src}")
    return n, date(int(last[:4]), int(last[4:6]), int(last[6:8]))


def last_session(before: date) -> date:
    from dqengine.runtime.core.data import is_market_holiday
    d = before - timedelta(days=1)
    while d.weekday() >= 5 or is_market_holiday(d):
        d -= timedelta(days=1)
    return d


def this_session(d: date) -> date:
    """`d` if the exchange trades that day, else the next day it does."""
    from dqengine.runtime.core.data import is_market_holiday
    while d.weekday() >= 5 or is_market_holiday(d):
        d += timedelta(days=1)
    return d


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return round(s[i] * 1000, 2)


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("algorithm", metavar="ALGORITHM.py")
    ap.add_argument("--data", required=True, metavar="DIR",
                    help="bar store to take the scripted session from")
    ap.add_argument("--symbol", help="default: the algorithm's first symbol")
    ap.add_argument("--day", help="session to replay (default: the last one "
                                  "with bars in the store)")
    ap.add_argument("--bars", type=int, default=390,
                    help="bars to feed, warm-up included (default 390)")
    ap.add_argument("--warmup", type=int, default=30,
                    help="bars whose timings are dropped (default 30)")
    ap.add_argument("--redis", action="store_true",
                    help="use REDIS_URL instead of the in-process bus")
    ap.add_argument("--json", metavar="FILE")
    args = ap.parse_args(argv)

    os.environ.setdefault("PYRUN_ENGINE_MODE", "inproc")
    os.environ.setdefault("PYRUN_INPROC_ALLOWED", "1")
    os.environ.pop("PYRUNNER_URL", None)
    url = (os.environ.get("DQENGINE_BENCH_DATABASE_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        raise SystemExit("set DQENGINE_BENCH_DATABASE_URL to a scratch "
                         "Postgres: this drops and rebuilds the schema")
    os.environ["DATABASE_URL"] = url
    tmp = tempfile.mkdtemp(prefix="bench-live-")
    os.environ["PYDATA_ROOT"] = os.path.join(tmp, "live-data")
    # the replay's history store: built below, everything before the
    # replayed session and nothing of it (see history_store)
    os.environ["DQENGINE_DATA_ROOT"] = os.path.join(tmp, "store")
    os.makedirs(os.environ["DQENGINE_DATA_ROOT"], exist_ok=True)
    os.environ.setdefault("DQENGINE_SECRET", "bench-live-throwaway-key")

    from sqlalchemy import text
    from dqengine.feeds import registry as feed_registry
    from dqengine.live import bar_source, executor, persistence, run, setup
    from dqengine.live import bus as bus_mod
    from dqengine.live.consumer import start_intent_consumer, start_sync_consumer
    from dqengine.sandbox import pyrunner                          # noqa: F401

    with persistence.SessionLocal().get_bind().begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    persistence.init_db()

    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else last_session(date.today()))
    today = this_session(date.today())     # the session the engine is in
    broker = TimingBroker()
    import dqengine.adapters.catalog as catalog
    catalog.get_adapter = lambda name: broker
    executor._poll_executions = lambda *a, **k: (0, 0)
    feed_registry.BUILTIN["bench"] = f"{__name__}:ScriptedFeed"

    # Two passes: the first only to learn which symbol the algorithm trades,
    # the second to start the deployment on the last day the history store
    # actually holds. The replay then has a settled day to anchor on and
    # meets the scripted session as LIVE bars, which is the state a running
    # deployment is in every morning and the only one where a new bar can
    # change its mind.
    rows = setup.prepare(args.algorithm, "bench", init=False,
                         creds={"k": "v"}, start=last_session(today),
                         cash=1_000_000.0)
    symbol = (args.symbol or rows["universe"][0]).upper()
    bars = session_bars(args.data, symbol, day, args.bars, stamp_as=today)
    history, anchor = history_store(args.data,
                                    os.environ["DQENGINE_DATA_ROOT"],
                                    symbol, today)
    rows = setup.prepare(args.algorithm, "bench", init=False,
                         creds={"k": "v"}, start=anchor, cash=1_000_000.0)
    ScriptedFeed.SCRIPT = list(bars)

    # The clock the engine and the worker loop read, moved to the bar being
    # delivered. Both already take an injected clock (the driver's tests
    # freeze it), and without it a benchmark run outside market hours
    # replays a session the engine will not open: every tick would step
    # nothing and the numbers would be of an empty loop.
    from dqengine.live.driver import engine as engine_mod
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=ET)
    clock = {"at": midnight}
    engine_mod._now_et = lambda: clock["at"]
    bar_source._now_et = lambda: clock["at"]
    # the in-progress cut reads the wall clock inline, so it is replaced
    # rather than injected: same rule, this clock
    bar_source._cut_in_progress_minute = lambda rows, d: (
        rows if d != clock["at"].date() else
        [r for r in rows
         if int(r[0]) < (clock["at"].hour * 3600
                         + clock["at"].minute * 60) * 1000])

    def at_bar(bar):
        return midnight + timedelta(milliseconds=bar.start_ms + 60_000)

    bus = InProcBus()
    if args.redis:
        b = bus_mod.bus_from_env()
        if b is None:
            raise SystemExit("--redis needs REDIS_URL")
        bus = TimedBus(b)
        for stream in ("bars", "intents", "sync"):
            bus.inner.delete(stream)

    # the exports bind the session factory at import, as the module they
    # were cut from did
    bar_source.SessionLocal = persistence.SessionLocal
    session = run.LiveSession(rows["dep_id"], rows["conn_id"],
                              feed_name="bench", symbols=rows["universe"],
                              bus=bus, feed_timeout=0.05,
                              bars=bar_source.SqlBarSource(
                                  history=lambda *a, **k: None,
                                  refresh=lambda sym: 0))
    swept = threading.Event()
    spans: dict = {"bar_tick": [], "intent_sweep": [], "bar_submit": []}

    def timed_intent(conn_id: str) -> str:
        from dqengine.live.consumer import default_intent
        t_intent = bus.stamps.get("intents", 0.0)
        try:
            return default_intent(conn_id)
        finally:
            spans["intent_sweep"].append(time.perf_counter() - t_intent)
            swept.set()

    session.start(consumers=False)
    stop = session._stop.is_set
    session._threads.append(start_intent_consumer(bus, handle=timed_intent,
                                                  stop=stop))
    session._threads.append(start_sync_consumer(bus, stop=stop))
    worker = session.worker()

    feed = session.feed
    ticks = idle = orders = 0
    t_wall = time.perf_counter()
    try:
        for n in range(len(bars)):
            measured = n >= args.warmup
            swept.clear()
            before = len(broker.submits)
            seen = bus.stamps.get("bars", 0.0)
            clock["at"] = at_bar(bars[n])
            feed.released.release()                 # one bar down the wire
            deadline = time.perf_counter() + 30.0
            while bus.stamps.get("bars", 0.0) == seen and \
                    time.perf_counter() < deadline:
                time.sleep(0.0002)
            t_pub = bus.stamps.get("bars", 0.0)
            state = worker.run_once(now_et=clock["at"])
            t_tick = time.perf_counter()
            if state != "ticked":
                idle += 1
                continue
            ticks += 1
            if measured:
                spans["bar_tick"].append(t_tick - t_pub)
            swept.wait(30.0)
            placed = len(broker.submits) - before
            if placed:
                orders += placed
                if measured:
                    spans["bar_submit"].append(broker.submits[before] - t_pub)
            if not measured:
                spans["intent_sweep"].clear()
    finally:
        wall = time.perf_counter() - t_wall
        session.stop(timeout=5.0)

    out = {
        "engine": "dqengine-live",
        "algo": os.path.basename(args.algorithm),
        "symbol": symbol, "price_session": day.isoformat(),
        "replayed_as": today.isoformat(), "anchor": anchor.isoformat(),
        "bus": "redis" if args.redis else "in-process",
        "bars": len(bars), "history_days": history,
        "measured": len(spans["bar_tick"]),
        "ticks": ticks, "idle": idle, "orders": orders,
        "bar_to_tick_ms": {"median": pct(spans["bar_tick"], 50),
                           "p95": pct(spans["bar_tick"], 95),
                           "max": pct(spans["bar_tick"], 100)},
        "intent_to_sweep_ms": {"median": pct(spans["intent_sweep"], 50),
                               "p95": pct(spans["intent_sweep"], 95),
                               "max": pct(spans["intent_sweep"], 100)},
        "bar_to_submit_ms": {"n": len(spans["bar_submit"]),
                             "median": pct(spans["bar_submit"], 50),
                             "p95": pct(spans["bar_submit"], 95),
                             "max": pct(spans["bar_submit"], 100)},
        "wall_s": round(wall, 2),
    }
    print(json.dumps(out))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({**out, "raw_ms": {k: [round(v * 1000, 3) for v in vs]
                                         for k, vs in spans.items()}}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
