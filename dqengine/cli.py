"""`dqengine` on the command line.

    dqengine example tqqq_weekly                       # write an example strategy here
    dqengine data fetch TQQQ QQQ --from 2021-01-01     # bars -> the local store
    dqengine backtest my_algo.py                       # the algorithm's own window
    dqengine backtest my_algo.py --from 2024-01-01 --cash 10000 --json out.json
    dqengine brokers                                   # installed broker adapters
    dqengine feeds                                     # installed live data feeds
    dqengine adopt my_algo.py --broker alpaca          # an account that already holds shares
    dqengine live my_algo.py --broker alpaca-paper     # trade it, paper money
    dqengine status                                    # is it healthy? (exit code says)
    dqengine orders / dqengine fills                   # what was sent, what filled

No logic lives here: arguments in, then the same feed (`dqengine.feed`),
store (`dqengine.store`) and backtester (`dqengine.runtime`) every other
caller uses. `live` is the same: it creates two rows
(`dqengine.live.setup`) and starts the pieces that trade
(`dqengine.live.run`), which is the composition a hosted platform does at
its own startup. Exit codes: 0 ok, 1 the run failed, 2 it could not start
(usage, missing credentials, a refusal).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

from . import __version__
from .config import _checkout_data_root, data_root
from .feed import KEY_ENV, SECRET_ENV


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _day(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        # a bare year reads naturally on a command line: --from 2021
        if len(s) == 4 and s.isdigit():
            return date(int(s), 1, 1)
        raise argparse.ArgumentTypeError(f"{s!r}: expected YYYY-MM-DD") from None


def _alpaca_feed(feed_name: str):
    """The bundled bar feed, from the standard Alpaca environment variables.
    None when they are not set (the caller explains)."""
    key, secret = os.environ.get(KEY_ENV, ""), os.environ.get(SECRET_ENV, "")
    if not key or not secret:
        return None
    from .feed import AlpacaBarFeed
    return AlpacaBarFeed(key, secret, feed=feed_name)


def _months(start: date, end: date):
    """[start, end] in calendar-month pieces: progress the user can see, and
    a failure costs one month rather than the whole range."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - timedelta(days=1))
        cur = nxt


def cmd_data_fetch(args) -> int:
    from .runtime.core.data import DataStore
    from .store import write_minute_day

    root = os.path.abspath(args.data or data_root())
    if not args.data and _checkout_data_root() == root:
        # inside the authors' checkout the default root is the curated,
        # parity-pinned bar set: vendor bars must never land there by default
        _err(f"refusing to write into the curated data tree ({root}) by default: "
             "pass --data DIR (or --data with that path, if you really mean it)")
        return 2
    end = args.to or (date.today() - timedelta(days=1))
    if args.start > end:
        _err(f"--from {args.start} is after --to {end}")
        return 2
    feed = _alpaca_feed(args.feed)
    if feed is None:
        _err(f"{KEY_ENV} and {SECRET_ENV} are not set.\n"
             "Historical minute bars come from Alpaca's market-data API, which is free:\n"
             "  1. create an account at https://alpaca.markets (paper trading is enough)\n"
             "  2. generate an API key, then\n"
             f"       export {KEY_ENV}=...\n"
             f"       export {SECRET_ENV}=...")
        return 2
    store = DataStore(root)
    for sym in (s.upper() for s in args.symbols):
        have = set() if args.force else set(store.minute_days(sym))
        wrote = 0
        for a, b in _months(args.start, end):
            # a month is skipped only when the store already covers its last
            # weekday: a partial month (yesterday's run) is fetched again
            last = b
            while last.weekday() >= 5:
                last -= timedelta(days=1)
            if last in have:
                continue
            for day, rows in sorted(feed.fetch_days(sym, a, b).items()):
                if day not in have and write_minute_day(root, sym, day, rows):
                    wrote += 1
            print(f"  {sym} {a:%Y-%m} … {wrote} new day(s)", flush=True)
        total = len(DataStore(root).minute_days(sym))
        print(f"{sym}: {wrote} day(s) written, {total} in {root}")
    return 0


def _summary(res: dict, wall: float) -> str:
    st = res["stats"]
    rows = [("Window", f"{res.get('start')} → {res.get('end')}  ({st.get('days')} sessions)"),
            ("Start equity", f"${st['start_equity']:,.2f}"),
            ("End equity", f"${st['end_equity']:,.2f}"),
            ("Net profit", f"{st['net_profit_pct']:+.2f}%"),
            ("CAGR", f"{st['cagr_pct']:+.2f}%"),
            ("Max drawdown", f"{st['max_drawdown_pct']:.2f}%"),
            ("Sharpe", f"{st['sharpe']:.3f}"),
            ("Fills", f"{st['fills']}  ({st['orders']} orders)"),
            ("Ran in", f"{wall:.2f}s")]
    w = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{w}}  {v}" for k, v in rows)


def cmd_backtest(args) -> int:
    from .runtime import run_python_backtest

    try:
        with open(args.algorithm, encoding="utf-8") as fh:
            code = fh.read()
    except OSError as e:
        _err(f"{args.algorithm}: {e.strerror}")
        return 2
    root = os.path.abspath(args.data or data_root())
    overrides = {k: v for k, v in (("start", args.start and args.start.isoformat()),
                                   ("end", args.to and args.to.isoformat()),
                                   ("cash", args.cash)) if v is not None}
    t0 = time.time()
    res = run_python_backtest(code, root, overrides=overrides)
    wall = time.time() - t0
    if "error" in res:
        e = res["error"]
        _err(f"{e['type']}: {e['message']}")
        if "no data available" in e["message"]:
            m = run_python_backtest(code, root, manifest_only=True).get("manifest") or {}
            syms = " ".join(m.get("subscriptions") or []) or "SYMBOL"
            start = overrides.get("start") or m.get("start") or "YYYY-MM-DD"
            _err(f"\nNo bars for this algorithm under {root}. Fetch them with:\n"
                 f"  dqengine data fetch {syms} --from {start} --data {root}")
        elif args.traceback:
            _err(e.get("traceback", ""))
        return 1
    for line in res.get("logs") or []:
        print(line)
    print(_summary(res, wall))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh)
        print(f"Full result (fills, orders, equity curve): {args.json}")
    return 0


def _examples() -> dict:
    """{name: source} for the strategies shipped in dqengine.examples."""
    from importlib import resources
    root = resources.files("dqengine.examples")
    return {f.name[:-3]: f.read_text(encoding="utf-8")
            for f in sorted(root.iterdir(), key=lambda f: f.name)
            if f.name.endswith(".py") and not f.name.startswith("_")}


def cmd_example(args) -> int:
    examples = _examples()
    if not args.name:
        import ast
        for name, src in examples.items():
            tree = ast.parse(src)
            doc = ast.get_docstring(tree) or next(
                (ast.get_docstring(n) for n in tree.body
                 if isinstance(n, ast.ClassDef) and ast.get_docstring(n)), "")
            print(f"{name:<14} {doc.strip().splitlines()[0] if doc else ''}")
        print("\nWrite one into this folder with: dqengine example NAME")
        return 0
    name = args.name[:-3] if args.name.endswith(".py") else args.name
    if name not in examples:
        _err(f"no example called {name!r}. Available: {', '.join(examples)}")
        return 2
    path = f"{name}.py"
    if os.path.exists(path):
        _err(f"{path} already exists here, not overwriting it")
        return 2
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(examples[name])
    print(f"wrote {path}. Run it with: dqengine backtest {path}")
    return 0


def cmd_brokers(_args) -> int:
    from . import brokers
    ids = brokers.available()
    for broker_id in ids:
        print(broker_id)
    if not ids:
        _err("no broker adapters installed")
    return 0


def cmd_feeds(_args) -> int:
    from . import feeds
    ids = feeds.available()
    for feed_id in ids:
        print(feed_id)
    if not ids:
        _err("no market-data feeds installed")
    return 0


# ----------------------------------------------------------------- live

# Where the live path writes the bar exports a replay reads. It must not be
# the curated store `backtest` reads: the live overlay is raw-price feed
# data, and the exporter refuses outright rather than write it over parity-
# proven zips. So the command picks a second directory when nobody has.
LIVE_DATA_DIR = "live-data"


def _live_extra_missing() -> bool:
    """`pip install deployquant` is numpy+pandas. The commands below need
    the database, the bus and the credential vault, so they say which
    install is missing rather than showing an ImportError."""
    for mod in ("sqlalchemy", "psycopg2", "redis", "cryptography"):
        try:
            __import__(mod)
        except ImportError:
            _err(f"the live commands need {mod}, which is part of the live "
                 f"install:\n  pip install 'deployquant[live]'")
            return True
    return False


def _engine_mode_notice() -> str:
    """Pick the engine mode a self-hosted install wants, before anything
    imports the runner (it reads the mode once, at import).

    In-process is the default here and the sandbox is not: a self-hoster
    runs their own code, and requiring Docker to trade one account of your
    own strategies buys isolation from nobody. Setting PYRUN_ENGINE_MODE
    yourself keeps whatever you set."""
    if os.environ.get("PYRUN_ENGINE_MODE") or os.environ.get("PYRUNNER_URL"):
        return f"{os.environ.get('PYRUN_ENGINE_MODE', 'sandbox')} (set by you)"
    os.environ["PYRUN_ENGINE_MODE"] = "inproc"
    os.environ["PYRUN_INPROC_ALLOWED"] = "1"
    return ("in-process (your algorithm runs in this process, with no "
            "container and no isolation from it)")


def _startup_block(rows) -> str:
    w = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k:<{w}}  {v}" for k, v in rows)


def _caps_line(settings: dict) -> str:
    order = settings.get("max_order_notional")
    position = settings.get("max_position_notional")
    if order is None and position is None:
        return ("off (pass --max-order-usd / --max-position-usd to refuse "
                "orders above a size)")
    parts = []
    if order is not None:
        parts.append(f"${order:,.0f} per order")
    if position is not None:
        parts.append(f"${position:,.0f} per position")
    return "on: " + ", ".join(parts)


def cmd_adopt(args) -> int:
    """`dqengine adopt`: the step before `--live` on an account that is not
    empty. Every decision is `dqengine.live.adopt`; this reads the flags,
    picks the engine mode and installs the same ports the live session
    installs, because the comparison screen replays the strategy exactly as
    a tick would."""
    if _live_extra_missing():
        return 2
    engine_mode = _engine_mode_notice()
    os.environ.setdefault("PYDATA_ROOT", os.path.abspath(LIVE_DATA_DIR))
    from dqengine.live import adopt
    from dqengine.live.run import install_ports
    install_ports(fallback=args.fallback or args.feed)
    print(f"dqengine adopt: {args.algorithm} on {args.broker}")
    print(_startup_block([
        ("engine", engine_mode),
        ("bar exports", os.environ["PYDATA_ROOT"]),
        ("bars", "refreshed over REST before the replay"
                 if not args.no_refresh else "not refreshed (--no-refresh)"),
    ]))
    print()
    return adopt.run(args.algorithm, args.broker, start=args.start,
                     cash=args.cash, margin=args.margin,
                     max_order_usd=args.max_order_usd,
                     max_position_usd=args.max_position_usd,
                     dry_run=bool(args.dry_run),
                     refresh_bars=not args.no_refresh, out=print)


def cmd_live(args) -> int:
    if _live_extra_missing():
        return 2
    if not os.environ.get("REDIS_URL"):
        _err("REDIS_URL is not set. The tick, the order path and the feed "
             "meet on Redis, so live trading needs one:\n"
             "  export REDIS_URL=redis://localhost:6379/0\n"
             "`deploy/docker-compose.yml` in this distribution starts "
             "Postgres and Redis for you.")
        return 2
    engine_mode = _engine_mode_notice()
    os.environ.setdefault("PYDATA_ROOT", os.path.abspath(LIVE_DATA_DIR))
    from dqengine.live import setup
    from dqengine.live.run import LiveSession
    try:
        rows = setup.prepare(
            args.algorithm, args.broker, live=bool(args.live),
            dry_run=bool(args.dry_run), cash=args.cash, start=args.start,
            margin=args.margin, max_order_usd=args.max_order_usd,
            max_position_usd=args.max_position_usd)
    except setup.DeploymentRefused as e:
        _err(f"refused: {e}")
        return 2
    if rows["secret_path"]:
        print(f"wrote a new credential key to {rows['secret_path']} (mode "
              f"600). Broker credentials in this database are encrypted "
              f"under it: back it up, and keep it for the life of the "
              f"install.")
    money = "REAL MONEY" if rows["mode"] == "live" else "paper money"
    fallback = args.fallback or args.feed
    from dqengine.live.persistence import DB_URL
    print(f"\ndqengine live: {rows['name']} ({money})")
    print(_startup_block([
        ("broker", f"{rows['broker']}, mode {rows['mode']}"),
        ("execution truth", rows["execution_truth"]),
        ("caps", _caps_line(rows["settings"])),
        ("dry run", "yes (real orders computed, nothing sent)"
                    if rows["settings"].get("dry_run") else "no"),
        ("universe", " ".join(rows["universe"])),
        ("resolution", rows["resolution"]),
        ("replay from", str(rows["start_date"])),
        ("cash / margin", f"${rows['cash']:,.2f} / {rows['margin']}x"),
        ("feed", f"{args.feed} (live bars and quotes)"),
        ("silence fallback", f"{fallback} (REST minutes while the feed "
                             f"is quiet)"),
        ("history", "Alpaca market data (APCA_API_KEY_ID)"),
        ("bar exports", os.environ["PYDATA_ROOT"]),
        ("engine", engine_mode),
        ("database", DB_URL.split("@")[-1]),
        ("deployment", rows["dep_id"]),
        ("connection", rows["conn_id"]),
    ]))
    print()
    session = LiveSession(rows["dep_id"], rows["conn_id"],
                          feed_name=args.feed, fallback_name=fallback,
                          symbols=rows["universe"])
    import signal

    def _bye(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    try:
        session.start()
    except Exception as e:                                   # noqa: BLE001
        _err(f"could not start: {e}")
        session.stop()
        return 2
    try:
        return session.run()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
        return 0
    finally:
        session.stop()


# --------------------------------------------------- status / orders / fills

def _table(rows: list, columns: list) -> str:
    """Plain text, one line per row, columns padded to their widest cell."""
    if not rows:
        return "(nothing yet)"
    head = [c.upper() for c in columns]
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in columns]
             for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells))
              for i, h in enumerate(head)]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(head))]
    out += ["  ".join(c[i].ljust(widths[i]) for i in range(len(head)))
            for c in cells]
    return "\n".join(out)


def _bus():
    from dqengine.live.bus import bus_from_env
    try:
        return bus_from_env()
    except Exception:                                        # noqa: BLE001
        return None


def cmd_status(args) -> int:
    if _live_extra_missing():
        return 2
    from dqengine.live import status as status_mod
    rep = status_mod.report(bus=_bus(), check_lock=not args.no_lock_check)
    bad = status_mod.problems(rep)
    if args.json:
        print(json.dumps({**rep, "problems": bad}, default=str))
        return 1 if bad else 0
    for c in rep["connections"]:
        print(f"connection {c['broker']}  mode {c['mode']}  "
              f"execution truth {c['execution_truth']}  status {c['status']}"
              + ("  DRY RUN" if c["dry_run"] else ""))
    for d in rep["deployments"]:
        held = ", ".join(f"{h['symbol']} {h['qty']:g}" for h in d["holdings"]
                         if h.get("qty")) or "flat"
        print(f"deployment {d['name']}  {d['status']}  {d['resolution']}  "
              f"[{' '.join(d['universe'])}]  holding {held}")
        print(f"  last tick {d['last_tick'] or 'never'}   "
              f"last sweep {d['last_sweep_at'] or 'never'}")
    j = rep["journal"]
    print(f"journal  open {j.get('open')}  unresolved {j.get('unresolved')}  "
          f"abandoned today {j.get('abandoned_today')}")
    feed = rep["feed"]
    print(f"feed  connected {feed.get('connected')}  "
          f"last bar {feed.get('last_bar_at')}  error {feed.get('error')}")
    if bad:
        print("\nproblems:")
        for line in bad:
            print(f"  - {line}")
        return 1
    print("\nno problems")
    return 0


def cmd_orders(args) -> int:
    if _live_extra_missing():
        return 2
    from dqengine.live import status as status_mod
    rows = status_mod.orders(args.limit)
    if args.json:
        print(json.dumps(rows, default=str))
        return 0
    print(_table(rows, ["at", "symbol", "side", "qty", "filled_qty", "kind",
                        "limit_price", "state", "rule_tag"]))
    return 0


def cmd_fills(args) -> int:
    if _live_extra_missing():
        return 2
    from dqengine.live import status as status_mod
    rows = status_mod.fills(args.limit)
    if args.json:
        print(json.dumps(rows, default=str))
        return 0
    print(_table(rows, ["at", "symbol", "qty", "price", "fees", "source",
                        "rule_tag"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dqengine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"dqengine {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    data = sub.add_parser("data", help="market data for the local store")
    dsub = data.add_subparsers(dest="data_command", required=True)
    fetch = dsub.add_parser("fetch", help="download minute bars (Alpaca, free) into the store")
    fetch.add_argument("symbols", nargs="+", metavar="SYMBOL")
    fetch.add_argument("--from", dest="start", type=_day, required=True, metavar="YYYY-MM-DD")
    fetch.add_argument("--to", type=_day, metavar="YYYY-MM-DD", help="default: yesterday")
    fetch.add_argument("--data", metavar="DIR", help="store root (default: DQENGINE_DATA_ROOT or ./data)")
    fetch.add_argument("--feed", choices=("sip", "iex"), default="sip",
                       help="sip = full market, free for history (default); iex = one exchange")
    fetch.add_argument("--force", action="store_true", help="re-download days already in the store")
    fetch.set_defaults(fn=cmd_data_fetch)

    bt = sub.add_parser("backtest", help="run an algorithm file over the local store")
    bt.add_argument("algorithm", metavar="ALGORITHM.py")
    bt.add_argument("--from", dest="start", type=_day, metavar="YYYY-MM-DD",
                    help="override the algorithm's start date")
    bt.add_argument("--to", type=_day, metavar="YYYY-MM-DD", help="override its end date")
    bt.add_argument("--cash", type=float, help="override its starting cash")
    bt.add_argument("--data", metavar="DIR", help="store root (default: DQENGINE_DATA_ROOT or ./data)")
    bt.add_argument("--json", metavar="FILE", help="write the full result here")
    bt.add_argument("--traceback", action="store_true", help="print the algorithm's traceback on error")
    bt.set_defaults(fn=cmd_backtest)

    ex = sub.add_parser("example", help="list the example strategies, or write one into this folder")
    ex.add_argument("name", nargs="?", metavar="NAME")
    ex.set_defaults(fn=cmd_example)

    br = sub.add_parser("brokers", help="list installed broker adapters")
    br.set_defaults(fn=cmd_brokers)

    fd = sub.add_parser("feeds", help="list installed live market-data feeds")
    fd.set_defaults(fn=cmd_feeds)

    ad = sub.add_parser(
        "adopt", help="put an account that already holds shares under a "
                      "strategy, and let its fills drive the accounting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "The step before `dqengine live --live` on an account that is "
            "not empty.\n\n"
            "It creates the same two rows `dqengine live` creates, pulls "
            "the account's own\ntrade history into the ledger, prints what "
            "the broker holds next to what the\nstrategy's replay holds, "
            "and -- once you type the account label back -- sets the\n"
            "connection's execution truth to `enforce`.\n\n"
            "There is no --yes. --dry-run prints the same screen and writes "
            "nothing.\n\n"
            "If a symbol does not agree, move --start back until the replay "
            "reproduces\nwhat the account holds: until they agree the first "
            "sweep refuses to trade at all."))
    ad.add_argument("algorithm", metavar="ALGORITHM.py")
    ad.add_argument("--broker", metavar="ID", required=True,
                    help="a broker id from `dqengine brokers`")
    ad.add_argument("--start", type=_day, metavar="YYYY-MM-DD",
                    help="first day of the replay — the dial that decides "
                         "what the strategy holds")
    ad.add_argument("--cash", type=float,
                    help="starting cash for the replay (default: 1000)")
    ad.add_argument("--margin", type=float,
                    help="leverage ceiling, 1.0 = cash only (default: 1.0)")
    ad.add_argument("--feed", metavar="ID", default="alpaca",
                    help="the feed whose REST bars the replay reads "
                         "(default: alpaca)")
    ad.add_argument("--fallback", metavar="ID",
                    help="a different feed for those REST bars")
    ad.add_argument("--max-order-usd", type=float, metavar="N",
                    help="refuse any order above this notional (default: no "
                         "cap)")
    ad.add_argument("--max-position-usd", type=float, metavar="N",
                    help="cap any one position's notional (default: no cap)")
    ad.add_argument("--no-refresh", action="store_true",
                    help="skip the REST bar refresh before the replay")
    ad.add_argument("--dry-run", action="store_true",
                    help="print the screen and write nothing")
    ad.set_defaults(fn=cmd_adopt)

    live = sub.add_parser(
        "live", help="trade an algorithm through a broker connection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Trade one algorithm on one broker connection.\n\n"
            "Needs Postgres and Redis (DATABASE_URL, REDIS_URL) and the "
            "broker's credentials in\nits own environment variables; "
            "deploy/docker-compose.yml starts the two services.\n"
            "Re-running with the same file and broker updates the same rows.\n\n"
            "Paper trading and --dry-run work end to end. --live needs the "
            "connection's\nexecution truth to be `enforce`, which is what "
            "`dqengine adopt` sets."))
    live.add_argument("algorithm", metavar="ALGORITHM.py")
    live.add_argument("--broker", metavar="ID", required=True,
                      help="a broker id from `dqengine brokers`")
    live.add_argument("--feed", metavar="ID", default="alpaca",
                      help="a feed id from `dqengine feeds` (default: alpaca)")
    live.add_argument("--fallback", metavar="ID",
                      help="the feed whose REST bars fill in while the live "
                           "feed is quiet (default: the --feed feed)")
    live.add_argument("--cash", type=float,
                      help="starting cash for the replay (default: 1000)")
    live.add_argument("--start", type=_day, metavar="YYYY-MM-DD",
                      help="first day of the replay (default: the next session)")
    live.add_argument("--margin", type=float,
                      help="leverage ceiling, 1.0 = cash only (default: 1.0)")
    live.add_argument("--dry-run", action="store_true",
                      help="compute the real orders and send none")
    live.add_argument("--live", action="store_true",
                      help="real money; requires the connection in `enforce`")
    live.add_argument("--max-order-usd", type=float, metavar="N",
                      help="refuse any order above this notional (default: no cap)")
    live.add_argument("--max-position-usd", type=float, metavar="N",
                      help="cap any one position's notional: refuse to "
                           "size a position above this (default: no cap)")
    live.set_defaults(fn=cmd_live)

    st = sub.add_parser("status", help="health of the live install; "
                                       "exits non-zero when something is wrong")
    st.add_argument("--json", action="store_true", help="machine-readable")
    st.add_argument("--no-lock-check", action="store_true",
                    help="skip the sweep-lock sample (saves a second)")
    st.set_defaults(fn=cmd_status)

    od = sub.add_parser("orders", help="the most recent orders sent")
    od.add_argument("--limit", type=int, default=20)
    od.add_argument("--json", action="store_true", help="machine-readable")
    od.set_defaults(fn=cmd_orders)

    fl = sub.add_parser("fills", help="the most recent broker executions")
    fl.add_argument("--limit", type=int, default=20)
    fl.add_argument("--json", action="store_true", help="machine-readable")
    fl.set_defaults(fn=cmd_fills)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
