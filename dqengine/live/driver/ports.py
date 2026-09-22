"""The three things the driver cannot do for itself.

Everything between a bar event and the published intent is driver logic and
becomes open code. Three dependencies are not (spec 2026-09-19 §3.1): the rows
it reads and writes, the bars it puts where the sandbox can read them, and who
is told that a tick may have moved money. They arrive through the Protocols
below.

Installed once per process by whoever composes the system -- the process
that serves the fleet, the entry point of one worker, a test harness -- and
held in module-level names, deliberately: the same shape as the bus singleton
(`dqengine.live.bus.BUS`), so no function on the money path grows a `ports`
argument and every existing monkeypatch site keeps working (spec §10 Q1).

A port that is not configured RAISES. One that quietly did nothing would let a
tick write a payload no executor acts on, or hand an `enforce` deployment its
own model fills instead of the broker's -- the failures this stack is built to
make loud.
"""
from __future__ import annotations

from datetime import date
from typing import ContextManager, Protocol


class DriverNotConfigured(RuntimeError):
    """A driver call reached a port nobody installed. Always a composition
    error in the process that started the tick, never a strategy's fault."""


class DeploymentView(Protocol):
    """The row as the driver reads it. Attribute names are exactly today's;
    the SQLAlchemy row satisfies this structurally, so nothing is copied into
    a dataclass and the session scope, identity map and the read-then-merge of
    `position` stay exactly as they are (spec §3.2, hazard H3)."""
    id: str
    kind: str
    ir: dict | None
    code: str | None
    universe: list | None
    resolution: str | None
    start_date: date
    paused_at: object
    status: str
    cash_initial: float
    margin_max: float | None
    position: dict | None
    broker_connection_id: str | None


class ConnectionView(Protocol):
    """A brokerage connection as the driver reads it: which venue, and
    whether the broker's own fills drive the account. Attribute names are
    exactly today's, so the SQLAlchemy row satisfies this structurally and
    nothing is copied into a dataclass."""
    broker: str
    execution_truth: str | None


class DeploymentTx(Protocol):
    """One tick's scope over one deployment: the row, its cash events, and the
    two ways a tick may end.

    One transaction object rather than five callables because the invariant is
    the scope: one session spans the whole tick, and the position merge reads
    the same row instance the tick started with.

    Leaving the context without calling either commit method leaves the row
    UNTOUCHED -- that is what a RunnerUnavailable does (hazard H2), and a store
    that "helpfully" commits on exit would mark every strategy errored during
    an engine deploy."""
    dep: DeploymentView | None
    events: list                 # cash events, ordered by effective_date

    def commit_payload(self, out: dict) -> None:
        """stats / equity / position / fills / journal, tick_error cleared,
        last_tick stamped. `position` arrives already merged."""

    def commit_error(self, text: str) -> None:
        """tick_error + last_tick only: the last good payload is kept."""


class DeploymentStore(Protocol):
    def open(self, dep_id: str) -> ContextManager[DeploymentTx]:
        ...

    def ledger(self, dep) -> object:
        """The deployment's broker-execution ledger, UNCAPPED. `None` means
        this connection is not in `enforce`, and the engine then keeps its own
        model fills. A failure must RAISE: swallowing it into `None` would
        silently demote an enforcing account to modelled fills (hazard H6).
        The driver applies the live cap itself for a tick and asks uncapped for
        the roll audit; both call this one method."""

    def connection(self, conn_id: str) -> ConnectionView | None:
        """One brokerage connection as the driver reads it, or None when the
        row is gone.

        Two daily-resolution questions need it. Whether the venue takes an
        on-close order itself decides when the at-close ticket is published
        (`engine.venue_takes_moc`), and whether the connection is enforcing
        decides whether a daily deployment may run on it at all
        (`deployment.daily_broker_refusal`). Both of those are the driver's
        own work -- `dqengine.adapters` and `dqengine.live.capabilities` are
        open. Reading the row is not, so it arrives here."""


class BarSource(Protocol):
    """The five storage/vendor calls the export orchestration makes. The
    orchestration itself -- the dedupe window, the once-per-(deployment, day)
    history key, the warm-up window, "never fatal, always loud" -- stays in the
    driver. `live_rows` returns CLOSED bars only (hazard H13)."""

    def live_rows(self, symbols: list, day: date) -> dict:
        ...

    def export_history(self, symbols: list, start: date, end: date) -> bool:
        """Backfill the adjusted history and export the backtest view.
        Returns True when the backfill was clean -- the driver marks the day
        done only then, or a failed backfill would warm on a hole all day."""

    def export_live(self, symbols: list, start: date, end: date,
                    collect: dict | None = None) -> int:
        ...

    def export_daily(self, symbols: list) -> int:
        """The DAILY view a daily-resolution deployment reads as its bars,
        carried past the last day the curated files hold. Called on a daily
        deployment's tick and nowhere else; a minute or second replay reads
        the minute tree and its inputs are unchanged. Returns the number of
        symbols whose file was written."""

    def refresh(self, symbol: str) -> int:
        ...

    def refresh_source(self) -> str | None:
        """A short name for the vendor call `refresh` makes, or None when
        this install has none -- a feed with no REST bars behind it.

        Optional: a bar source that does not answer is taken to have a
        refresh, which is what every one of them had before the question
        existed. The driver asks so that an install without a fall-back
        says so in the line that reports the silence, instead of failing
        once per symbol for as long as the stream stays quiet."""


class IntentSink(Protocol):
    """Told once per tick that may have moved money. The driver never calls a
    broker: what it hands over is the committed payload on the row, and this
    notification that a fresh one exists."""

    def acted(self, dep_id: str, conn_id: str, seq: str) -> None:
        ...


_STORE: DeploymentStore | None = None
_BARS: BarSource | None = None
_SINK = None                  # (bus) -> IntentSink; bound to the loop's bus


def configure(store: DeploymentStore | None = None,
              bars: BarSource | None = None, sink=None) -> None:
    """Install this process's implementations. Arguments left out are left as
    they are, so a caller can install one port at a time."""
    global _STORE, _BARS, _SINK
    if store is not None:
        _STORE = store
    if bars is not None:
        _BARS = bars
    if sink is not None:
        _SINK = sink


def store() -> DeploymentStore:
    if _STORE is None:
        raise DriverNotConfigured(
            "no deployment store: this process must call "
            "ports.configure(store=...) before anything ticks")
    return _STORE


def bars() -> BarSource:
    if _BARS is None:
        raise DriverNotConfigured(
            "no bar source: this process must call ports.configure(bars=...) "
            "before anything ticks")
    return _BARS


def intent_sink(bus) -> IntentSink:
    """The sink for one loop, bound to the bus that loop reads."""
    if _SINK is None:
        raise DriverNotConfigured(
            "no intent sink: this process must call ports.configure(sink=...) "
            "before a worker loop is built")
    return _SINK(bus)
