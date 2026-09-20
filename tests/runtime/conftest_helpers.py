"""Synthetic stores shared by the dqengine.runtime tests."""
import os
import sys
from datetime import date
from datetime import timedelta as _timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime.core.data import DayBars  # noqa: E402

OPEN_MS = 9 * 3600_000 + 30 * 60_000


def synth_day(day, closes):
    """n one-minute bars from 9:30, closes as given, open=close-0.5,
    high=close+1, low=close-1."""
    c = np.array(closes, float)
    n = len(c)
    return DayBars(day=day,
                   start_ms=np.array([OPEN_MS + i * 60_000 for i in range(n)]),
                   open=c - 0.5, high=c + 1.0, low=c - 1.0, close=c,
                   volume=np.ones(n))


class SynthStore:
    def __init__(self, days):   # {date: DayBars}
        self.days = days

    def minute_days(self, sym):
        return sorted(self.days)

    def load_minute_day(self, sym, day):
        return self.days.get(day)

    def load_daily(self, sym):
        return {d: (float(b.open[0]), float(b.high.max()), float(b.low.min()),
                    float(b.close[-1]), float(b.volume.sum()))
                for d, b in self.days.items()}


def two_day_store():
    return SynthStore({date(2026, 8, 24): synth_day(date(2026, 8, 24), [100, 101, 102]),
                       date(2026, 8, 25): synth_day(date(2026, 8, 25), [103, 104, 105])})


def make_book(day, ms=10 * 3600_000, cash=100_000.0, price=50.0, sym="TQQQ"):
    """A bare OrderBook over a real Sleeve, for tests about the book itself
    rather than about a whole backtest run. Returns (book, sleeve)."""
    from dqengine.runtime.core.portfolio import Sleeve
    from dqengine.runtime.orders import OrderBook

    sleeve = Sleeve(cash=cash)
    book = OrderBook(sleeve, clock=lambda: (day, ms), events_out=lambda e: None,
                     prices={sym: price})
    return book, sleeve


# ------------------------------------------------- second-resolution store
# Moved here in Phase 3 Task 9 from tests/test_second_resolution.py, whose
# six tests all drove the deleted IR Backtester/WarmEngine but whose
# synthetic store is what test_codegen_second_res.py replays the `sec_alloc`
# and `sec_rules` goldens against. Real second bars exist locally only for
# SPY 2013, so this is the only second-resolution fixture there is.
#
# Bars are SPARSE on purpose (an open cluster, a midday cluster, a close
# tail): the gaps between clusters exercise the union walk and the
# before-close latch exactly where the 2026-08-25 late-bar lessons live.

SECOND_BAR_MS = 1000
SECOND_CLOSE_MS = 57_600_000                     # 16:00:00


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += _timedelta(days=1)
    return out


SECOND_DAYS = weekdays(date(2024, 1, 2), 4)
SECOND_TEST_DAY = SECOND_DAYS[-1]


def second_day_spec(seed, dip=False):
    """Sparse second bars: 60s open cluster, midday cluster (optionally
    dipping to trip a stop), close tail 15:58:55..15:59:59."""
    import random
    rng = random.Random(seed)
    px = 100.0 + (seed % 7)
    spec = {}
    for i in range(60):                                  # 09:30:00-09:30:59
        px = max(1.0, px * (1.0 + rng.uniform(-0.0005, 0.0005)))
        spec[OPEN_MS + SECOND_BAR_MS * i] = round(px, 4)
    mid = 43200000                                       # 12:00:00
    for i in range(20):
        drift = -0.004 if (dip and 5 <= i <= 12) else rng.uniform(-0.0005,
                                                                  0.0005)
        px = max(1.0, px * (1.0 + drift))
        spec[mid + SECOND_BAR_MS * i] = round(px, 4)
    tail = SECOND_CLOSE_MS - 65000                       # 15:58:55
    for i in range(65):                                  # ..15:59:59
        px = max(1.0, px * (1.0 + rng.uniform(-0.0005, 0.0005)))
        spec[tail + SECOND_BAR_MS * i] = round(px, 4)
    return spec


class SecondStore:
    def __init__(self, spec):
        self.spec = spec                                 # {sym: {day: {ms: px}}}

    def minute_days(self, sym):
        return sorted(self.spec.get(sym, {}))

    def load_minute_day(self, sym, day):
        m = self.spec.get(sym, {}).get(day)
        if not m:
            return None
        ts = np.array(sorted(m), dtype=np.int64)
        c = np.array([m[t] for t in sorted(m)], dtype=np.float64)
        return DayBars(day=day, start_ms=ts, open=c.copy(),
                       high=c * 1.0002, low=c * 0.9998, close=c.copy(),
                       volume=np.full(len(c), 100.0))


def build_second_spec(dip_test_day=False):
    return {
        "AAA": {d: second_day_spec(
                    11 * (i + 1),
                    dip=(dip_test_day and d == SECOND_TEST_DAY))
                for i, d in enumerate(SECOND_DAYS)},
        "BBB": {d: second_day_spec(29 * (i + 1))
                for i, d in enumerate(SECOND_DAYS)},
    }


# ---- the reference bar set ---------------------------------------------------

def has_reference_bars(data_root, sym):
    """True when `sym`'s bars under data_root are the reference set: every
    sentinel day in reference_bars.json hashes to the recorded value."""
    import hashlib
    import json
    import zipfile

    with open(os.path.join(os.path.dirname(__file__), "reference_bars.json")) as fh:
        sentinels = json.load(fh)["sentinels"]
    mine = {k: v for k, v in sentinels.items() if k.split("/")[0] == sym.lower()}
    if not mine:
        return False
    for key, want in mine.items():
        s, day = key.split("/")
        path = os.path.join(data_root, "equity", "usa", "minute", s, f"{day}_trade.zip")
        try:
            with zipfile.ZipFile(path) as z:
                got = hashlib.sha256(z.read(z.namelist()[0])).hexdigest()
        except (OSError, zipfile.BadZipFile, IndexError):
            return False
        if got != want:
            return False
    return True


def reference_bars(data_root, *symbols):
    """A skipif mark for cases that pin exact dollar results.

    Those numbers were produced on ONE specific set of bars (the set LEAN was
    run on), which market-data licences do not let the project redistribute.
    Bars fetched from a vendor today are price-identical on every common bar
    but are not the same set -- it keeps extended-hours rows these lack, and
    it is missing two regular sessions these have -- so a pinned case on them
    is a false alarm, not a regression. The set is recognised by the sha256
    of a few sentinel days (reference_bars.json), never by a directory merely
    existing."""
    import pytest

    missing = [s for s in symbols if not has_reference_bars(data_root, s)]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"pinned to the reference bar set, which is not at {data_root} "
               f"for {missing} (freshly fetched bars are a different set)")
