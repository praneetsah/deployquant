"""Sandbox-side bar stores beyond the plain minute/daily zips.

Pure-file ports of the API's second-resolution stores (alpaca_data.py) —
the sandbox has no Postgres, so coverage bookkeeping stays outside; these
just read what the export step materialized under /data."""
import os
from datetime import date

import numpy as np

from dqengine.runtime.core.data import DataStore, DayBars


class FileSecondStore:
    """npz second bars: {root}/equity/usa/second/{SYM}/{YYYYMMDD}.npz,
    each guarded by the writer's cache version."""

    def __init__(self, data_root: str, cache_version: int):
        self._root = data_root
        self._version = int(cache_version)

    def _dir(self, symbol: str) -> str:
        return os.path.join(self._root, "equity", "usa", "second",
                            symbol.upper())

    def minute_days(self, symbol: str):
        d = self._dir(symbol)
        if not os.path.isdir(d):
            return []
        out = []
        for f in os.listdir(d):
            if f.endswith(".npz"):
                try:
                    out.append(date(int(f[0:4]), int(f[4:6]), int(f[6:8])))
                except ValueError:
                    continue
        return sorted(out)

    def load_minute_day(self, symbol: str, day: date):
        path = os.path.join(self._dir(symbol), day.strftime("%Y%m%d") + ".npz")
        if not os.path.exists(path):
            return None
        try:
            z = np.load(path)
            if int(z["v"]) != self._version:
                return None
            return DayBars(day=day, start_ms=z["start_ms"].astype(np.int64),
                           open=z["open"], high=z["high"], low=z["low"],
                           close=z["close"], volume=z["volume"])
        except Exception:
            return None

    def load_daily(self, symbol: str):
        return DataStore(self._root).load_daily(symbol)


class HybridStore:
    """Days before `second_from` serve minute zips (pre-start warm-up only
    consumes daily session closes, identical at any intraday resolution);
    days at/after serve second npz ONLY — a gap is a gap, never a silent
    minute fallback."""

    def __init__(self, data_root: str, second_from: date, cache_version: int):
        self.second_from = second_from
        self._minute = DataStore(data_root)
        self._second = FileSecondStore(data_root, cache_version)

    def minute_days(self, symbol: str):
        pre = [d for d in self._minute.minute_days(symbol)
               if d < self.second_from]
        post = [d for d in self._second.minute_days(symbol)
                if d >= self.second_from]
        return sorted(set(pre) | set(post))

    def load_minute_day(self, symbol: str, day: date):
        if day < self.second_from:
            return self._minute.load_minute_day(symbol, day)
        return self._second.load_minute_day(symbol, day)

    def load_daily(self, symbol: str):
        return self._minute.load_daily(symbol)
