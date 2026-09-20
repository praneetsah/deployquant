"""`dqengine` on the command line.

    dqengine example tqqq_weekly                       # write an example strategy here
    dqengine data fetch TQQQ QQQ --from 2021-01-01     # bars -> the local store
    dqengine backtest my_algo.py                       # the algorithm's own window
    dqengine backtest my_algo.py --from 2024-01-01 --cash 10000 --json out.json
    dqengine brokers                                   # installed broker adapters

No logic lives here: arguments in, then the same feed (`dqengine.feed`),
store (`dqengine.store`) and backtester (`dqengine.runtime`) every other
caller uses. Exit codes: 0 ok, 1 the run failed, 2 it could not start
(usage, missing credentials, a command that has not shipped).
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

KEY_ENV, SECRET_ENV = "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"


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


def cmd_live(_args) -> int:
    _err("`dqengine live` ships in 0.2: the single-account live driver and "
         "executor are being moved into this package from the hosted platform "
         "that runs them today, unchanged. This release backtests.")
    return 2


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

    live = sub.add_parser("live", help="run an algorithm live (ships in 0.2)")
    live.add_argument("algorithm", metavar="ALGORITHM.py")
    live.add_argument("--broker", metavar="ID")
    live.set_defaults(fn=cmd_live)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
