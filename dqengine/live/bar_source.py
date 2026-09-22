"""The bar store behind the driver's `BarSource` port.

The process that replays a strategy has no network and no database: it
reads a LEAN-layout file tree, the same layout `dqengine.store` writes and
`dqengine.runtime.core.data.DataStore` reads. The rows this module exports
come from two tables instead -- `hist_bars`, the adjusted vendor history a
warm-up needs, and `bar_days`, the raw live feed -- and it writes them out
as real zips so a replay reads back numerically the bar the live engine
stepped.

Two views, two precedences. `export_bars` is the BACKTEST view: curated
zips over `hist_bars`. `export_live_bars` is the LIVE view: `bar_days` on
top, because today's prices are raw and the history under them is adjusted.

`export_daily` is the third file a DAILY-resolution strategy reads as its
bars: the curated daily zips, carried past the last day they hold with rows
derived from the minute tree the two exports above just wrote.

PYDATA_ROOT is where the exports land; it defaults to DATA_ROOT, which puts
them next to the curated zips. Point it at a separate directory to keep the
two apart.
"""
import json
import os
import shutil
import threading
import zipfile
from datetime import date

from dqengine.live.driver.ports import DriverNotConfigured
from dqengine.live.persistence import HistBar, SessionLocal

# LEAN minimum price variation encoding (dqengine.runtime.core.data.SCALE)
SCALE = 10000


def data_root() -> str:
    from dqengine.config import DATA_ROOT
    return DATA_ROOT


def pydata_root() -> str:
    return os.environ.get("PYDATA_ROOT") or data_root()


def _minute_dir(root: str, symbol: str) -> str:
    return os.path.join(root, "equity", "usa", "minute", symbol.lower())


def _zip_name(day: date) -> str:
    return f"{day.strftime('%Y%m%d')}_trade.zip"


def scaled_rows(rows: list) -> list:
    """Rows as the zip encodes them: [ms, o, h, l, c, v] with prices as
    LEAN-scaled ints and volume through the same %g formatting the zip
    uses. The live driver pushes THESE to the warm engine, so a pushed
    bar is numerically the bar a replay reads back from the zip. One row
    per minute, last occurrence wins (stored days have carried duplicates)."""
    dedup = {}
    for ms, o, h, l, c, v in sorted(rows, key=lambda r: int(r[0])):
        dedup[int(ms)] = [int(ms), int(round(o * SCALE)), int(round(h * SCALE)),
                          int(round(l * SCALE)), int(round(c * SCALE)), float(f"{v:g}")]
    return [dedup[k] for k in sorted(dedup)]


def _write_zip_from_rows(path: str, day: date, rows: list) -> None:
    lines = [f"{ms},{o},{h},{l},{c},{v:g}" for ms, o, h, l, c, v in scaled_rows(rows)]
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{day.strftime('%Y%m%d')}_trade.csv", "\n".join(lines))
    os.replace(tmp, path)


def export_bars(symbols: list[str], start: date, end: date,
                progress_cb=None) -> int:
    """Idempotent per symbol-day; zips (curated, parity-proven) win over
    hist_bars, the same overlay rule the backtest store reads by. Returns
    days written."""
    src_root, dst_root = data_root(), pydata_root()
    written = 0
    for sym in symbols:
        sym_u = sym.upper()
        dst_dir = _minute_dir(dst_root, sym)
        src_dir = _minute_dir(src_root, sym)
        have = set(os.listdir(dst_dir)) if os.path.isdir(dst_dir) else set()
        with SessionLocal() as s:
            pg_days = [d for (d,) in s.query(HistBar.day)
                       .filter(HistBar.symbol == sym_u,
                               HistBar.day >= start, HistBar.day <= end).all()]
        src_days = []
        if src_root != dst_root and os.path.isdir(src_dir):
            for f in os.listdir(src_dir):
                if f.endswith("_trade.zip"):
                    d = date(int(f[0:4]), int(f[4:6]), int(f[6:8]))
                    if start <= d <= end:
                        src_days.append((d, f))
        need = ({d for d in pg_days} | {d for d, _ in src_days})
        if not need:
            continue
        os.makedirs(dst_dir, exist_ok=True)
        src_names = {d: f for d, f in src_days}
        for d in sorted(need):
            name = _zip_name(d)
            if name in have:
                continue
            if d in src_names:                      # curated zip wins: copy it
                shutil.copyfile(os.path.join(src_dir, src_names[d]),
                                os.path.join(dst_dir, name))
            else:
                with SessionLocal() as s:
                    rec = s.get(HistBar, (sym_u, d))
                if rec is None or len(rec.rows) < 2:
                    continue
                _write_zip_from_rows(os.path.join(dst_dir, name), d, rec.rows)
            written += 1
            if progress_cb:
                progress_cb(sym_u, written)
    return written


def _cut_in_progress_minute(rows: list, day: date) -> list:
    """Drop any row of TODAY whose minute has not ended. A REST bar refresh
    keeps the in-progress minute; a streaming feed stores only completed
    ones. A partial row treated as a closed bar lets a stop trigger on a low
    the real minute never printed -- decided HERE, at the source, not by
    lagging the engine's clock."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    _now = _dt.now(_Z("America/New_York"))
    if day != _now.date():
        return rows
    cutoff_ms = (_now.hour * 3600 + _now.minute * 60) * 1000
    return [r for r in rows if int(r[0]) < cutoff_ms]


def live_day_rows(symbols: list[str], day: date) -> dict:
    """{SYM: scaled rows} for ONE live day, straight from bar_days -- the
    rows the warm engine is pushed each tick. One indexed query, no zip:
    the zip is written after the tick (export_live_bars) for the replay
    fallback and the roll audit, which read the store. Same cut and the
    same encoding as the zip, so a pushed bar is the zip's bar."""
    from dqengine.live.persistence import BarDay
    out = {}
    with SessionLocal() as s:
        recs = (s.query(BarDay)
                 .filter(BarDay.symbol.in_([x.upper() for x in symbols]),
                         BarDay.day == day).all())
        for r in recs:
            if not r.rows:
                continue
            rows = _cut_in_progress_minute(list(r.rows), day)
            if len(rows) >= 2:
                out[r.symbol.upper()] = scaled_rows(rows)
    return out


# (symbol, day) -> fingerprint of the bar_days rows last exported for a
# COMPLETED day. See export_live_bars: a zip tracks its rows, not its
# existence.
_EXPORTED: dict = {}


def export_live_bars(symbols: list[str], start: date, end: date,
                     collect: dict | None = None) -> int:
    """The sandbox's view of what a LIVE replay must see.

    export_bars above is the BACKTEST view: curated zips over hist_bars,
    where hist_bars is vendor history, TOTAL-RETURN adjusted. A live replay
    needs a third source and a different precedence:

        bar_days (the live feed, RAW prices) wins for its days
          -> curated LEAN zips
            -> hist_bars (vendor history, adjusted)

    Order matters for a reason beyond freshness. hist_bars is split- and
    dividend-adjusted; bar_days is raw. Splicing them the other way round
    puts a step at the boundary the size of every corporate action since,
    and the strategy trades the step.

    Without this the runner saw ONLY export_bars' output, so a live replay
    ended at whatever day some backtest last exported: the payload was weeks
    stale, and the executor traded to match it.

    bar_days days are rewritten on every call. Today's bars accrue through
    the session, so a cached file for today is stale by construction — that
    is the entire point of running this on the tick path.
    """
    from dqengine.live.persistence import BarDay

    dst_root = pydata_root()
    src_root = data_root()
    if os.path.abspath(dst_root) == os.path.abspath(src_root):
        # PYDATA_ROOT unset (dev): the sandbox root IS the curated tree.
        # Writing raw live rows over git-tracked, parity-proven zips would
        # corrupt every backtest that follows. Refuse rather than clobber.
        print("[pydata] export_live_bars refused: PYDATA_ROOT is the curated "
              "data root — set PYDATA_ROOT to a separate directory",
              flush=True)
        return 0
    written = 0
    for sym in symbols:
        sym_u = sym.upper()
        dst_dir = _minute_dir(dst_root, sym)
        have = set(os.listdir(dst_dir)) if os.path.isdir(dst_dir) else set()
        with SessionLocal() as s:
            live_days = (s.query(BarDay)
                         .filter(BarDay.symbol == sym_u,
                                 BarDay.day >= start, BarDay.day <= end).all())
            rows_by_day = {r.day: list(r.rows) for r in live_days
                           if r.rows and len(r.rows) >= 2}
            stamp_by_day = {r.day: (r.fetched_at.isoformat()
                                    if getattr(r, "fetched_at", None) else None)
                            for r in live_days}
        if not rows_by_day:
            continue
        os.makedirs(dst_dir, exist_ok=True)
        newest = max(rows_by_day)
        # A REST bar refresh keeps the IN-PROGRESS minute; a streaming feed
        # stores only completed ones. A partial row exported as a closed bar
        # lets a stop trigger on a low the real minute never printed. Drop
        # any row of the newest day whose minute has not ended yet --
        # decided HERE, at the source, rather than by lagging the engine's
        # clock (which on the worker path, woken only by the next bar,
        # stepped every bar a minute late).
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        _now = _dt.now(_Z("America/New_York"))
        if newest == _now.date():
            rows_by_day[newest] = _cut_in_progress_minute(rows_by_day[newest], newest)
            if len(rows_by_day[newest]) < 2:
                rows_by_day.pop(newest)
                if not rows_by_day:
                    continue
                newest = max(rows_by_day)
        if collect is not None and newest == _now.date():
            # the live day's rows, already cut at the in-progress minute,
            # for the driver to push to the warm engine (same rows the zip
            # below is written from)
            collect[sym_u] = scaled_rows(rows_by_day[newest])
        for d, rows in sorted(rows_by_day.items()):
            # Bounded per tick: the live day is rewritten every call (its
            # bars accrue through the session). A COMPLETED day is rewritten
            # only when its stored rows changed since the last export --
            # NOT "written once, first time seen": a thin symbol's late
            # candle can land after the first write (39 rows in bar_days, 38
            # in the zip), and "older days are written once" never looks
            # again -- two readers of the same day then disagree by a bar.
            # Rewriting every day every tick was unbounded (~1,700 zips/tick
            # on a 29-symbol strategy three months in); the fingerprint
            # keeps it O(days) dict lookups, with one zip read per day after
            # a process restart.
            path = os.path.join(dst_dir, _zip_name(d))
            if d == newest and d == _now.date():
                _write_zip_from_rows(path, d, rows)
                written += 1
                continue
            key = (sym_u, d)
            fp = _rows_fingerprint(rows, stamp_by_day.get(d))
            if _EXPORTED.get(key) == fp:
                continue
            if key not in _EXPORTED and _read_sidecar(path) == fp:
                _EXPORTED[key] = fp          # restart: the sidecar remembers
                continue
            _write_zip_from_rows(path, d, rows)
            _write_sidecar(path, fp)
            _EXPORTED[key] = fp
            written += 1
    return written


def _rows_fingerprint(rows: list, stamp=None) -> list:
    """O(1) change detector for a stored day. Row count plus the FIRST and
    LAST rows verbatim (a feed can resend a corrected 09:30 or 15:59 candle
    for a minute it already sent, and the writer upserts it in place: same
    count, new price), plus the row's fetched_at, which every bar_days
    writer stamps on any edit, so a corrected mid-day bar is caught too.
    JSON-shaped so the sidecar round-trips it."""
    return [len(rows), [float(x) for x in rows[0]], [float(x) for x in rows[-1]],
            stamp]


def _sidecar(path: str) -> str:
    return path + ".fp"


def _read_sidecar(path: str):
    """The fingerprint the zip at `path` was written from, or None."""
    try:
        with open(_sidecar(path)) as f:
            return json.load(f)
    except Exception:                                   # noqa: BLE001
        return None


def _write_sidecar(path: str, fp) -> None:
    tmp = _sidecar(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fp, f)
    os.replace(tmp, _sidecar(path))


# The derivation the daily zips in PYDATA_ROOT were last written by. A zip
# whose sidecar does not say this is rebuilt in full once, whatever else the
# incremental rule would have reused. Bump it when the rows a given minute
# day produces change -- version 1 is the first write that drops a day still
# in progress, and the zips written before it may hold a partial row that
# nothing else would ever correct.
DAILY_DERIVE_VERSION = 1

# How many trailing days are recomputed on every pass. A thin symbol's late
# candle lands after the day was first written (REW 2026-08-31), so the tail
# cannot be trusted to stay put; everything older is copied from the zip
# rather than re-read, which is what keeps a 5-year symbol off the tick
# path's clock (a full rebuild is ~0.3 s per symbol).
DERIVE_RECOMPUTE_DAYS = 10


def _now_et():
    """Injectable clock (tests freeze it). The market's timezone, because
    every session boundary below is an ET boundary."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    return _dt.now(_Z("America/New_York"))


def _day_is_complete(day: date) -> bool:
    """Has `day`'s session ended and settled?

    A daily bar for a session still in progress is a lie with a price in
    it: its close is whatever the tape happened to print a moment ago, and
    a strategy that reads it decides on a close the exchange has not made.
    A day that is not today is complete by definition. Today is complete a
    minute after its real close -- the same moment the worker rolls the
    session (worker.roll_due), and late enough for the last candles to have
    landed. Early closes move with close_time_ms."""
    from dqengine.runtime.core.data import close_time_ms
    now = _now_et()
    if day != now.date():
        return True
    now_ms = ((now.hour * 3600 + now.minute * 60 + now.second) * 1000
              + now.microsecond // 1000)
    return now_ms >= close_time_ms(day) + 60_000


def export_daily(symbols: list[str], extend: bool = False) -> int:
    """Daily bars for the sandbox (benchmark curves + DAILY-resolution
    algos read them). A curated zip is copied verbatim; a symbol WITHOUT
    one gets a zip DERIVED from its exported minute days. Without the
    derivation, any DAILY algo over a freshly fetched ticker died with 'no
    data available' (2026-08-30, five ETFs with no curated files). Runs
    AFTER export_bars so the minute tree is complete.

    `extend=True` also carries a CURATED symbol past the last day its
    curated file holds, deriving the days after it from the minute tree.
    The curated lines are copied through byte for byte, so the days the
    parity pins live on cannot move; only days the curated file never had
    are added. Daily callers pass it -- a daily strategy whose data stops
    at the curated file's last day cannot trade today. Minute and second
    callers do not, so their inputs are exactly what they were.

    Returns the number of symbols whose file was written."""
    src_root, dst_root = data_root(), pydata_root()
    same_root = os.path.abspath(src_root) == os.path.abspath(dst_root)
    copied = 0
    for sym in symbols:
        src = os.path.join(src_root, "equity", "usa", "daily", f"{sym.lower()}.zip")
        dst = os.path.join(dst_root, "equity", "usa", "daily", f"{sym.lower()}.zip")
        wrote = False
        if os.path.exists(src):
            if src != dst and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                wrote = True
            if extend:
                if same_root:
                    # PYDATA_ROOT unset (dev): the sandbox root IS the
                    # curated tree, and the extension writes the file it
                    # would be extending. Refuse rather than rewrite a
                    # git-tracked, parity-proven zip, exactly as
                    # export_live_bars refuses for the minute tree.
                    print("[pydata] export_daily extension refused: "
                          "PYDATA_ROOT is the curated data root — set "
                          "PYDATA_ROOT to a separate directory", flush=True)
                elif _extend_daily_zip(sym, src, dst, dst_root):
                    wrote = True
        elif _derive_daily_zip(sym, dst_root, dst):
            wrote = True
        if wrote:
            copied += 1
    return copied


def _daily_line(day: date, o: float, h: float, l: float, c: float,
                v: float) -> str:
    """One LEAN daily row, as the zip stores it."""
    return (f"{day.strftime('%Y%m%d')} 00:00,"
            f"{int(round(o * SCALE))},"
            f"{int(round(h * SCALE))},"
            f"{int(round(l * SCALE))},"
            f"{int(round(c * SCALE))},{v:g}")


def _read_daily_lines(path: str) -> tuple[list[str], dict]:
    """(every line in the daily zip at `path`, {day: line}). Empty for a
    file that is missing or unreadable -- a corrupt zip is rebuilt, not
    raised on."""
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read(z.namelist()[0]).decode()
    except Exception:                                   # noqa: BLE001
        return [], {}
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    by_day = {}
    for ln in lines:
        try:
            by_day[date(int(ln[0:4]), int(ln[4:6]), int(ln[6:8]))] = ln
        except ValueError:
            continue
    return lines, by_day


def _daily_version_path(dst: str) -> str:
    return dst + ".v"


def _read_daily_version(dst: str) -> int:
    try:
        with open(_daily_version_path(dst)) as f:
            return int(json.load(f))
    except Exception:                                   # noqa: BLE001
        return 0


def _own_tmp(path: str) -> str:
    """A temp name no other writer uses. Two daily deployments that share a
    symbol export the same daily zip from two worker processes; with one
    shared `<zip>.tmp` the first rename took the file away from under the
    second, whose tick then failed (seen in prod the day this shipped)."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"


def _write_daily_version(dst: str) -> None:
    tmp = _own_tmp(_daily_version_path(dst))
    with open(tmp, "w") as f:
        json.dump(DAILY_DERIVE_VERSION, f)
    os.replace(tmp, _daily_version_path(dst))


def _derived_lines(sym: str, dst_root: str, after: date | None,
                   keep: dict, full: bool) -> list[str]:
    """Daily rows derived from the minute tree for every COMPLETE day
    strictly after `after` (None = all of them).

    `keep` is {day: line} already in the file; a day older than the
    recompute window is taken from it rather than re-read. `full` ignores
    `keep` entirely -- the one rebuild the version sidecar forces."""
    from dqengine.runtime.core.data import DataStore, derive_daily_row

    store = DataStore(dst_root)
    days = [d for d in store.minute_days(sym)
            if (after is None or d > after) and _day_is_complete(d)]
    fresh = set(days) if full else set(days[-DERIVE_RECOMPUTE_DAYS:])
    lines = []
    for d in days:
        if d not in fresh and d in keep:
            lines.append(keep[d])
            continue
        b = store.load_minute_day(sym, d)
        if b is None or not b.n:
            continue
        lines.append(_daily_line(d, *derive_daily_row(b)))
    return lines


def _write_daily_lines(dst: str, sym: str, lines: list[str]) -> bool:
    """Write the file only when its contents would change. Returns whether
    it was written. The version sidecar is stamped either way, so the
    forced rebuild happens once and not on every pass."""
    have, _ = _read_daily_lines(dst)
    if have == lines:
        _write_daily_version(dst)
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = _own_tmp(dst)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{sym.lower()}.csv", "\n".join(lines))
    os.replace(tmp, dst)
    _write_daily_version(dst)
    return True


def _extend_daily_zip(sym: str, src: str, dst: str, dst_root: str) -> bool:
    """Carry a curated daily file past its last day with derived rows.

    The curated lines go through verbatim. The days after them come from
    the minute tree in the DESTINATION root -- the same bars a later
    backtest of those days reads, so the live close and the backtest close
    are one number.

    The seam is real and worth naming: the curated rows are the exchange's
    official daily bars, and the rows after them are aggregated from minute
    trades. They differ by small amounts (measured on SPY: about 0.15% on
    the close). Nothing can remove that difference while the curated file
    ends where it ends; what this does guarantee is that both engines read
    the same side of it."""
    curated, cur_by = _read_daily_lines(src)
    if not curated or not cur_by:
        return False
    last = max(cur_by)
    _, have = _read_daily_lines(dst)
    full = _read_daily_version(dst) != DAILY_DERIVE_VERSION
    keep = {d: ln for d, ln in have.items() if d > last}
    derived = _derived_lines(sym, dst_root, last, keep, full)
    return _write_daily_lines(dst, sym, curated + derived)


def _derive_daily_zip(sym: str, dst_root: str, dst: str) -> bool:
    """Aggregate the symbol's minute days (in the DESTINATION tree, which
    export_bars just completed) into a LEAN-format daily zip. Incremental:
    the trailing days are re-read and everything older is carried over from
    the file, so a symbol with five years of minute days costs a directory
    listing rather than 1,250 zip reads."""
    _, have = _read_daily_lines(dst)
    full = _read_daily_version(dst) != DAILY_DERIVE_VERSION
    lines = _derived_lines(sym, dst_root, None, have, full)
    if not lines:
        return False
    return _write_daily_lines(dst, sym, lines)


class SqlBarSource:
    """The driver's `BarSource` port over the exports above.

    Two of the four calls cannot be answered from the tables alone. Filling
    a hole in `hist_bars` means asking a market-data vendor for history, and
    refreshing today's minutes means asking one for bars; which vendor, and
    with whose account, is the host's decision, so both arrive from the
    host:

        SqlBarSource(history=fetch, refresh=refresh_one)
        SqlBarSource(history=fetch, feed=running_feed)

        history(symbol, start, end, zip_days)   # backfill hist_bars
        refresh(symbol) -> int                  # today's minutes into bar_days

    `feed` is the shorter way to say the same thing about the refresh, and
    the one an install with a live feed wants: a feed that implements
    `dqengine.feeds.BarRefresher` already has its vendor's REST minutes, so
    the fall-back for a silent stream is that stream's own vendor without
    anybody composing it. An explicit `refresh=` wins over it, which is how
    a host refreshes from one account and streams on another.

    A host that passes neither still gets `live_rows` and `export_live`, and
    a driver that reaches either of the other two gets a
    `DriverNotConfigured` naming what is missing. It never quietly succeeds:
    an export that silently skipped the backfill would leave the warm-up
    reading a hole all day, and a refresh that silently did nothing would
    step the engine on yesterday's bars.
    """

    def __init__(self, history=None, refresh=None, feed=None):
        self._history = history
        self._refresh = refresh
        self._feed = feed

    def _installed(self, fn, name: str, consequence: str):
        if fn is None:
            raise DriverNotConfigured(
                f"the bar source has no {name}: build it as "
                f"SqlBarSource({name}=...) in the process that ticks, or "
                f"{consequence}")
        return fn

    def live_rows(self, symbols: list, day) -> dict:
        return live_day_rows(symbols, day)

    def export_history(self, symbols: list, start, end) -> bool:
        from dqengine.config import DATA_ROOT
        from dqengine.runtime.core.data import DataStore
        # asked for before the try: a backfill nobody installed is a
        # composition error, not the vendor outage `clean=False` describes
        history = self._installed(
            self._history, "history",
            "every warm-up reads whatever hole hist_bars already has")
        # the ADJUSTED history the warm-up reads, then the backtest view over
        # it. `clean` is the driver's retry signal: a backfill that failed
        # part-way must be asked again on the next call.
        clean = True
        try:
            zips = DataStore(DATA_ROOT)
            for sym in symbols:
                history(sym, start, end, zips.minute_days(sym))
        except Exception as fe:                     # noqa: BLE001
            clean = False
            print(f"[live] history backfill failed: {fe}", flush=True)
        export_bars(symbols, start, end)
        return clean

    def export_live(self, symbols: list, start, end,
                    collect: dict | None = None) -> int:
        return export_live_bars(symbols, start, end, collect=collect)

    def export_daily(self, symbols: list) -> int:
        # extend=True is what the live path wants and the only thing it
        # wants: a daily deployment whose data stops at the last day the
        # curated file holds cannot trade today. A backtest job calls
        # export_daily directly and decides for itself.
        return export_daily(symbols, extend=True)

    def _refresher(self):
        """What a refresh actually calls: the callable the host passed, else
        the feed's own REST bars, else None."""
        from dqengine.feeds.base import bar_refresher
        if self._refresh is not None:
            return self._refresh
        return bar_refresher(self._feed)

    def refresh_source(self) -> str | None:
        """A short name for what a silence fall-back would ask, or None when
        this install has nothing to ask.

        The driver reads it before it starts refreshing, so an install whose
        feed has no REST bars says that once, in the line that reports the
        silence, instead of failing once per symbol every window."""
        if self._refresh is not None:
            return "REST"
        return (None if self._refresher() is None
                else f"{type(self._feed).__name__} REST")

    def refresh(self, symbol: str) -> int:
        fn = self._refresher()
        if fn is None and self._feed is not None:
            raise DriverNotConfigured(
                f"the bar source has no refresh: the feed it was built with "
                f"({type(self._feed).__name__}) fetches no recent bars over "
                f"REST, so a silent stream has no fall-back here. Pass "
                f"SqlBarSource(refresh=...), or name a feed that has them")
        return self._installed(
            fn, "refresh",
            "the engine steps on whatever bar_days already holds")(symbol)


def default_bar_source(creds=None, feed=None) -> SqlBarSource:
    """The bar source a single-deployment install gets: the history from
    `dqengine.live.history` on one Alpaca account, and today's minutes from
    `feed` when one is passed -- the running feed's own vendor -- or from
    that same Alpaca account when none is.

    This is a composition, not a default. `SqlBarSource()` still refuses
    both calls rather than quietly picking a vendor, because a host that
    pays for its own market data must say so; this is the answer for a
    host that has not got one."""
    from dqengine.live import history
    return SqlBarSource(
        history=lambda sym, start, end, zip_days: history.ensure_history(
            sym, start, end, zip_days, creds=creds),
        refresh=(None if feed is not None
                 else lambda sym: history.refresh(sym, creds=creds)),
        feed=feed)
