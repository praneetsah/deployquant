"""Second-bar aggregation core — THE shared function (second-resolution
spec §5/§6).

Historical second bars (consolidated-tape trades) and live second bars (a
level-one quote stream) are both built HERE, by the same arithmetic, so the
backtest and the live engine can never disagree about what a second bar
is. Changing anything in this module that alters output bumps
CACHE_VERSION, which lazily invalidates the on-disk cache day by day.

Honesty notes, pinned:
- EXCLUDED_CONDITIONS is the consolidated-tape "ineligible to update
  OHLC" set (odd lots, cash/next-day sales, prior-reference, average
  price, extended-hours prints, derivatively priced, out-of-sequence,
  official open/close records). Excluded trades contribute NOTHING —
  including volume. That is a deliberate simplification (some codes are
  volume-eligible on the tape); consistency between history and live
  outweighs per-field tape fidelity here.
- A level-one stream carries no condition codes; its lastPrice already
  reflects the exchange's own eligibility logic. The live consolidator
  therefore filters nothing, and the nightly cross-source job (spec §9)
  measures the residual drift instead of anyone guessing at it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CACHE_VERSION = 1

EXCLUDED_CONDITIONS = frozenset({
    "C",   # cash sale
    "G",   # bunched sold (out of sequence)
    "H",   # price variation
    "I",   # odd lot
    "M",   # market center official close (record, not a trade)
    "N",   # next day
    "P",   # prior reference price
    "Q",   # market center official open (record, not a trade)
    "R",   # seller
    "T",   # extended hours
    "U",   # extended hours (sold out of sequence)
    "V",   # contingent
    "W",   # average price
    "Z",   # sold (out of sequence)
    "4",   # derivatively priced
    "7",   # qualified contingent
    "9",   # corrected consolidated close (record)
})

SESSION_OPEN_MS = 34200000        # 09:30:00 ET


def trade_eligible(conditions) -> bool:
    return not any(c in EXCLUDED_CONDITIONS for c in (conditions or ()))


def parse_alpaca_trade(raw: dict):
    """(ms_of_day_et, price, size, conditions) for one Alpaca v2 trade
    row, or None when unparseable. Timestamps arrive RFC3339 with up to
    nanosecond precision; python parses microseconds, so the fractional
    part is truncated to 6 digits — sub-millisecond truncation cannot
    move a trade across a 1000ms bucket boundary by more than the SIP's
    own clock jitter, and both sides of a boundary are ours anyway."""
    t = raw.get("t")
    if not t or raw.get("p") is None:
        return None
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    if "." in t:
        head, _, rest = t.partition(".")
        frac = rest[:-6]                      # strip the +00:00
        tz = rest[len(frac):]
        t = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    try:
        dt = datetime.fromisoformat(t).astimezone(ET)
    except ValueError:
        return None
    ms = (dt.hour * 3600 + dt.minute * 60 + dt.second) * 1000 \
        + dt.microsecond // 1000
    return (ms, float(raw["p"]), float(raw.get("s") or 0.0),
            tuple(raw.get("c") or ()))


def aggregate_trades_to_seconds(trades, close_ms: int) -> dict:
    """{bucket_start_ms: [ms, o, h, l, c, v]} from (ms, price, size,
    conditions) tuples. Regular session only ([SESSION_OPEN_MS,
    close_ms)); a second with no eligible trades produces NO bar (a gap —
    the engine's union walk handles gaps natively). Trades are applied in
    input order; callers pass them time-ordered (Alpaca returns them
    ordered; the live consolidator receives them ordered)."""
    out: dict = {}
    for ms, px, size, conds in trades:
        if not (SESSION_OPEN_MS <= ms < close_ms):
            continue
        if not trade_eligible(conds):
            continue
        bucket = (ms // 1000) * 1000
        bar = out.get(bucket)
        if bar is None:
            out[bucket] = [bucket, px, px, px, px, size]
        else:
            if px > bar[2]:
                bar[2] = px
            if px < bar[3]:
                bar[3] = px
            bar[4] = px
            bar[5] += size
    return out


class SecondBucketConsolidator:
    """Streaming counterpart for the live path (Phase C): feed trades as
    they arrive, collect completed bars each time the clock crosses a
    second boundary. Same arithmetic as aggregate_trades_to_seconds by
    construction (both funnel through the same bucket-update rules —
    pinned by the equivalence property test)."""

    def __init__(self, close_ms: int):
        self.close_ms = close_ms
        self._open: dict = {}          # sym -> [ms,o,h,l,c,v] in-progress
        self._done: list = []          # [(sym, bar)] completed, unfetched

    def add_trade(self, sym: str, ms: int, px: float, size: float,
                  conditions=()) -> None:
        if not (SESSION_OPEN_MS <= ms < self.close_ms):
            return
        if not trade_eligible(conditions):
            return
        bucket = (ms // 1000) * 1000
        bar = self._open.get(sym)
        if bar is not None and bar[0] != bucket:
            # the trade itself proves the previous second elapsed --
            # complete that bar even if no flush ran in between
            self._done.append((sym, bar))
            bar = None
        if bar is None:
            self._open[sym] = [bucket, px, px, px, px, size]
        else:
            if px > bar[2]:
                bar[2] = px
            if px < bar[3]:
                bar[3] = px
            bar[4] = px
            bar[5] += size

    def flush_before(self, now_ms: int) -> list:
        """[(sym, bar), ...]: everything completed by trade-succession plus
        every in-progress bar whose second has fully elapsed
        (bucket + 1000 <= now_ms). Call on each second boundary."""
        done, self._done = self._done, []
        for sym in list(self._open):
            bar = self._open[sym]
            if bar[0] + 1000 <= now_ms:
                done.append((sym, bar))
                del self._open[sym]
        return done
