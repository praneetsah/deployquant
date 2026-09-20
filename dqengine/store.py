"""Writing minute bars into the LEAN-layout store the engine reads.

Read side is `dqengine.runtime.core.data.DataStore`; this is the matching write
side, so a bar the driver fetched is stored exactly the way a curated LEAN
zip stores it (prices x10000 as ints, one row per minute, last occurrence
of a duplicate minute wins) and a replay reads back numerically the same
bar the live engine stepped. Atomic replace: a reader never sees a half zip.
"""
from __future__ import annotations

import os
import zipfile
from datetime import date

from dqengine.runtime.core.data import SCALE


def minute_dir(data_root: str, symbol: str) -> str:
    return os.path.join(data_root, "equity", "usa", "minute", symbol.lower())


def minute_zip_path(data_root: str, symbol: str, day: date) -> str:
    return os.path.join(minute_dir(data_root, symbol), f"{day.strftime('%Y%m%d')}_trade.zip")


def scaled_rows(rows: list) -> list:
    """[ms, o, h, l, c, v] with float prices -> the zip's encoding."""
    dedup = {}
    for ms, o, h, l, c, v in sorted(rows, key=lambda r: int(r[0])):
        dedup[int(ms)] = [int(ms), int(round(o * SCALE)), int(round(h * SCALE)),
                          int(round(l * SCALE)), int(round(c * SCALE)), float(f"{v:g}")]
    return [dedup[k] for k in sorted(dedup)]


def _vol(v: float) -> str:
    """The volume as text. `%g` turns 1,270,160 into `1.27016e+06`, which
    reads back as the same float here but which LEAN -- whose folder layout
    this is, and which users point at the same directory -- does not parse.
    A whole number is written as one; the value is unchanged."""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def write_minute_day(data_root: str, symbol: str, day: date, rows: list) -> str:
    """Rows are [ms_since_midnight_ET, o, h, l, c, v] with float prices.
    Returns the path written. An empty row list writes nothing and returns
    ""; a day with no bars must stay ABSENT, not exist empty."""
    if not rows:
        return ""
    path = minute_zip_path(data_root, symbol, day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [f"{ms},{o},{h},{l},{c},{_vol(v)}" for ms, o, h, l, c, v in scaled_rows(rows)]
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{day.strftime('%Y%m%d')}_trade.csv", "\n".join(lines))
    os.replace(tmp, path)
    return path
