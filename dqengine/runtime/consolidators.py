"""Time-bucketed bar consolidation.

The single most common thing a minute/daily-only resolution set cannot
express is "run this on hourly bars". A consolidator aggregates the bars it
is fed into fixed spans and emits one bar per completed span, which is what
`self.consolidate(symbol, timedelta(hours=1), handler)` means in LEAN.

`timedelta(hours=1)` used to raise UnsupportedApiError for indicators
(`_ind_res`); with a consolidator behind it there is a real implementation
to point at.
"""
from datetime import datetime, timedelta

from .aliases import PascalMixin, alias_methods
from .bars import TradeBar
from .enums import Resolution


def span_of(period) -> timedelta:
    """LEAN accepts a timedelta or a Resolution; both mean a bucket width."""
    if isinstance(period, timedelta):
        return period
    if period == Resolution.SECOND:
        return timedelta(seconds=1)
    if period == Resolution.MINUTE:
        return timedelta(minutes=1)
    if period == Resolution.DAILY:
        return timedelta(days=1)
    raise ValueError(f"cannot consolidate on {period!r}")


@alias_methods
class TradeBarConsolidator(PascalMixin):
    """Aggregates bars into `span` buckets, emitting on the first bar that
    belongs to a LATER bucket.

    Emitting late rather than on a timer is deliberate: a bucket is only
    provably complete once a bar past it arrives, and a consolidator that
    emitted early would hand user code a partial bar indistinguishable from
    a whole one. The trailing partial bucket is flushed at session end.
    """

    def __init__(self, period):
        self.span = span_of(period)
        self.handlers = []
        self.consolidated = None      # the last EMITTED bar
        self.working_bar = None
        self._bucket = None

    def add_handler(self, fn):
        self.handlers.append(fn)

    def _bucket_of(self, t: datetime) -> int:
        epoch = datetime(t.year, t.month, t.day)
        if self.span >= timedelta(days=1):
            return (t.date() - epoch.date()).days
        return int((t - epoch).total_seconds() // self.span.total_seconds())

    def update(self, bar: TradeBar):
        b = self._bucket_of(bar.end_time)
        if self._bucket is not None and b != self._bucket:
            self.scan()
        self._bucket = b
        w = self.working_bar
        if w is None:
            self.working_bar = TradeBar(
                bar.symbol, bar.time, bar.end_time, bar.open, bar.high,
                bar.low, bar.close, bar.volume)
        else:
            self.working_bar = TradeBar(
                w.symbol, w.time, bar.end_time, w.open,
                max(w.high, bar.high), min(w.low, bar.low), bar.close,
                w.volume + bar.volume)

    def scan(self):
        """Emit the working bar, if any. Called on a bucket change and at
        session end, so a day never leaks a partial bucket into the next."""
        if self.working_bar is None:
            return
        self.consolidated = self.working_bar
        self.working_bar = None
        self._bucket = None
        for fn in self.handlers:
            fn(self.consolidated)
