"""self.history(...) — LEAN-shaped pandas frames from the platform's stores.

v1 scope: subscribed symbols only; DAILY or MINUTE; last-N periods (or a
timedelta) strictly before algo.time. Under DQENGINE_MANIFEST_PASS=1 (the sandbox
runner's subscription-discovery pass) every call returns an empty frame."""
import os
from datetime import datetime, timedelta

import pandas as pd

from .enums import Resolution
from .errors import UnsupportedApiError, unsupported

COLS = ["open", "high", "low", "close", "volume"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLS)


def _norm_symbols(algo, symbols) -> tuple[list[str], bool]:
    single = not isinstance(symbols, (list, tuple, set))
    syms = [symbols] if single else list(symbols)
    out = []
    for s in syms:
        u = str(s).upper()
        if u not in algo.securities:
            raise UnsupportedApiError(
                f"history for unsubscribed symbol {u} — add_equity it first "
                f"(v1 serves history only for subscribed symbols)")
        out.append(u)
    return out, single


def _now(algo) -> datetime:
    if algo.time is not None:
        return algo.time
    sd = algo._start_date
    return datetime(sd.year, sd.month, sd.day) if sd else datetime.min


def _daily_frame(algo, sym: str, n: int, now: datetime) -> pd.DataFrame:
    daily = algo._store.load_daily(sym) or {}
    days = sorted(d for d in daily if d < now.date())[-n:]
    rows = [daily[d] for d in days]
    return pd.DataFrame(rows, columns=COLS,
                        index=pd.Index([pd.Timestamp(d) for d in days], name="time"))


def _minute_frame(algo, sym: str, n: int, now: datetime) -> pd.DataFrame:
    store = algo._store
    times: list[pd.Timestamp] = []
    rows: list[tuple] = []
    now_ms = (now.hour * 3600 + now.minute * 60 + now.second) * 1000
    for day in reversed(store.minute_days(sym)):
        if len(rows) >= n:
            break
        if day > now.date():
            continue
        b = store.load_minute_day(sym, day)
        if b is None or not b.n:
            continue
        span = int(b.start_ms[1] - b.start_ms[0]) if b.n > 1 else 60_000
        for i in range(b.n - 1, -1, -1):
            end_ms = int(b.start_ms[i]) + span
            if day == now.date() and end_ms > now_ms:
                continue
            times.append(pd.Timestamp(datetime(day.year, day.month, day.day)
                                      + timedelta(milliseconds=end_ms)))
            rows.append((float(b.open[i]), float(b.high[i]), float(b.low[i]),
                         float(b.close[i]), float(b.volume[i])))
            if len(rows) >= n:
                break
    times.reverse()
    rows.reverse()
    return pd.DataFrame(rows, columns=COLS, index=pd.Index(times, name="time"))


def history(algo, symbols, periods, resolution=None) -> pd.DataFrame:
    if os.environ.get("DQENGINE_MANIFEST_PASS") == "1":
        return _empty()
    syms, single = _norm_symbols(algo, symbols)
    if resolution is None:
        resolution = algo.securities[syms[0]].resolution
    if resolution not in (Resolution.DAILY, Resolution.MINUTE, Resolution.SECOND):
        unsupported(f"history(resolution={resolution})")
    now = _now(algo)
    if isinstance(periods, timedelta):
        n = max(1, periods.days) if resolution == Resolution.DAILY \
            else max(1, int(periods.total_seconds() // 60))
    else:
        n = int(periods)

    frames = {}
    for s in syms:
        if resolution == Resolution.DAILY:
            frames[s] = _daily_frame(algo, s, n, now)
        else:
            frames[s] = _minute_frame(algo, s, n, now)

    if single:
        return frames[syms[0]]
    return pd.concat(frames, names=["symbol", "time"])
