"""The tables the executor owns, and the one engine and session factory
every process on this database shares.

One DeclarativeBase lives here and nothing else declares another: a host that
runs more tables of its own declares them on this `Base`, so a foreign key
from one of them into these resolves at `create_all` and the schema the host
emits is one schema, not two that have to be kept in step by hand.

`SessionLocal` stays a `sessionmaker` and `engine` stays a module attribute
on purpose. Open code resolves the factory at call time -- `SessionLocal()`
freshly imported inside the function, or `persistence.SessionLocal()` -- so a
test that re-points the factory at a scratch database re-points every module
at once, and so the advisory-lock helper can read the bind off it.

`user_id` and `strategy_id` on the two rows a host owns are plain nullable
strings here: an owner tag the engine never reads. A host that has users
tightens them into real foreign keys on these same tables before it creates
anything (see its own models module); a self-hoster leaves them null and the
executor behaves identically either way.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey,
                        Index, String, Text, UniqueConstraint,
                        create_engine)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker

import os

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://dqengine:dqengine@localhost:5432/dqengine")
# coolify/heroku style URLs sometimes lack the driver
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    id = Column(String, primary_key=True, default=_uuid)
    # owner tag, unused by the engine (see the module docstring)
    user_id = Column(String, nullable=True)
    broker = Column(String, nullable=False)          # webull | schwab | alpaca
    label = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")
    # pending | connected | reconnect_needed | error
    mode = Column(String, nullable=False, default="paper")   # paper | live
    settings = Column(JSONB, nullable=True)
    # safety-rail overrides + kill switch:
    # {"paused": bool, "max_order_notional": float,
    #  "max_position_notional": float, "max_orders_per_sync": int,
    #  "dry_run": bool}
    creds_encrypted = Column(Text, nullable=True)
    # cached account preview: {"equity","cash","history":{"days":[],"values":[]},
    #  "fetched_at"} — refreshed on read when stale
    balance = Column(JSONB, nullable=True)
    balance_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    # off | observe | enforce — see the execution-truth spec §12. `off` is
    # today's behavior; `observe` polls and displays the ledger without
    # touching accounting or sizing; `enforce` lets it drive both.
    execution_truth = Column(String, nullable=False, default="off")


class Deployment(Base):
    __tablename__ = "deployments"
    id = Column(String, primary_key=True, default=_uuid)
    # owner tags, unused by the engine (see the module docstring)
    user_id = Column(String, nullable=True)
    strategy_id = Column(String, nullable=True)
    name = Column(String, nullable=False)
    # authoring flavor, mirroring Strategy: 'blocks' carries ir, 'python'
    # carries code. Snapshotted at deploy time either way — editing the
    # strategy never mutates a running deployment.
    kind = Column(String, nullable=False, default="blocks")
    ir = Column(JSONB, nullable=True)                # snapshot at deploy time
    code = Column(Text, nullable=True)               # snapshot at deploy time
    # Lifted out of the IR for BOTH kinds: these are the only two things the
    # live/execution layer ever reads out of `ir`, and reading them from
    # columns keeps the executor from reaching into engine internals.
    universe = Column(JSONB, nullable=True)          # ["SPY", "QQQ"]
    # NULL means "derive it" (the driver's resolution rule), never
    # "minute". A column default here would silently override
    # ir.meta.resolution for every creation path that forgot to set it.
    resolution = Column(String, nullable=True)
    mode = Column(String, nullable=False, default="paper")   # paper (live later)
    # destination account: null = built-in paper simulator; a broker_connections
    # id once live execution ships (many deployments may share one account —
    # that's the sleeve engine)
    broker_connection_id = Column(String, ForeignKey("broker_connections.id"),
                                  nullable=True)
    live_confirmed = Column(Boolean, nullable=False, default=False)
    # typed user confirmation required before real-money orders fire
    status = Column(String, nullable=False, default="running")
    # running | paused | stopped
    cash_initial = Column(Float, nullable=False)
    margin_max = Column(Float, nullable=False, default=1.0)
    start_date = Column(Date, nullable=False)
    paused_at = Column(Date, nullable=True)          # replay end pin when paused/stopped
    # tick outputs
    stats = Column(JSONB, nullable=True)
    equity = Column(JSONB, nullable=True)
    position = Column(JSONB, nullable=True)          # current holdings summary
    fills = Column(JSONB, nullable=True)
    journal = Column(JSONB, nullable=True)     # recent decision trail (why view)
    last_tick = Column(DateTime(timezone=True), nullable=True)
    tick_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    # first day whose fills come from the broker ledger rather than the
    # replay's model prices; NULL means the whole history is modeled. The
    # equity curve renders this boundary so the two are never silently mixed.
    reconciled_from = Column(Date, nullable=True)


def managed_deployments(session, conn_id):
    """The deployments a broker connection's executor manages: running AND
    paused.

    One copy of a query that lived in three places -- the sweep, ledger
    attribution and the multi-deployment wrapper -- each carrying a comment
    that the other two must be kept identical. When they drift, fills land
    in the wrong sleeve, and under `enforce` a `manual` row is
    indistinguishable from no fill: the sleeve flattens and the next sweep
    sells a position the account holds.

    `paused` means "open no new positions", not "this sleeve no longer owns
    its shares" -- the executor still maintains a paused deployment's resting
    exits. `stopped` genuinely is not managed.

    No ORDER BY: that is what all three have always done. Deployment order is
    heap order, and it is what the max_orders_per_sync budget has always cut
    against. Giving it one here would be a behaviour change, not a fix.
    """
    return (session.query(Deployment)
            .filter(Deployment.broker_connection_id == conn_id,
                    Deployment.status.in_(["running", "paused"])).all())


class SleeveEvent(Base):
    __tablename__ = "sleeve_events"
    id = Column(String, primary_key=True, default=_uuid)
    deployment_id = Column(String, ForeignKey("deployments.id"), nullable=False)
    kind = Column(String, nullable=False)            # deposit
    amount = Column(Float, nullable=False)
    effective_date = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)


class BrokerOrder(Base):
    """Audit trail: every submit/replace/cancel/refusal the executor performs,
    with deployment attribution (works even for brokers without client order
    ids — we attribute at record time, not by round-tripping the broker)."""
    __tablename__ = "broker_orders"
    id = Column(String, primary_key=True, default=_uuid)
    connection_id = Column(String, ForeignKey("broker_connections.id"),
                           nullable=False, index=True)
    deployment_id = Column(String, ForeignKey("deployments.id"), nullable=True)
    broker_order_id = Column(String, nullable=True)
    client_order_id = Column(String, nullable=True)
    symbol = Column(String, nullable=False)
    qty = Column(Float, nullable=False)
    side = Column(String, nullable=False)            # buy | sell
    order_type = Column(String, nullable=False)      # market | limit
    limit_price = Column(Float, nullable=True)
    status = Column(String, nullable=True)           # broker-reported status
    action = Column(String, nullable=False)          # submit|replace|cancel|refused
    created_at = Column(DateTime(timezone=True), default=_now)
    rule_tag = Column(String, nullable=True)


class OrderJournal(Base):
    """Write-ahead order ledger (spec: 2026-08-31-order-journal.md).

    A row exists BEFORE the wire call (state `sending`) and leaves the
    outstanding set only on affirmative evidence. UNIQUE(connection_id,
    client_order_id) is the cross-process duplicate gate: two passes
    computing the same intent produce the same deterministic cid, and the
    second INSERT fails instead of double-sending -- the 2026-08-31
    double-buy class, closed at the database."""
    __tablename__ = "order_journal"
    __table_args__ = (UniqueConstraint("connection_id", "client_order_id",
                                       name="uq_journal_conn_cid"),)
    id = Column(String, primary_key=True, default=_uuid)
    connection_id = Column(String, ForeignKey("broker_connections.id"),
                           nullable=False, index=True)
    deployment_id = Column(String, ForeignKey("deployments.id"),
                           nullable=True)
    client_order_id = Column(String, nullable=False)
    broker_order_id = Column(String, nullable=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)             # buy | sell
    qty = Column(Float, nullable=False)
    filled_qty = Column(Float, nullable=False, default=0.0)
    kind = Column(String, nullable=False)             # market | limit | ...
    limit_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    rule_tag = Column(String, nullable=True)
    # sending | submitted | open | filled | canceled | rejected | abandoned
    state = Column(String, nullable=False, index=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class Execution(Base):
    """What the BROKER actually did — one row per execution, not per order.

    `broker_orders` records what the executor SUBMITTED; this records what
    came back. Per-execution (not per-order) so a partial fill survives as
    two rows instead of collapsing into an average. Brokers that only report
    order-level averages (Webull) set order_level_avg=True and reuse the
    order id as broker_exec_id.

    deployment_id is NULL for executions the platform did not submit (the
    user's own manual trades in the same account) — those are surfaced in
    the reconciliation panel and never folded into a sleeve.
    """
    __tablename__ = "executions"
    id = Column(String, primary_key=True, default=_uuid)
    connection_id = Column(String, ForeignKey("broker_connections.id"),
                           nullable=False, index=True)
    deployment_id = Column(String, ForeignKey("deployments.id"),
                           nullable=True, index=True)
    broker_order_id = Column(String, nullable=True, index=True)
    broker_exec_id = Column(String, nullable=False)
    client_order_id = Column(String, nullable=True)
    symbol = Column(String, nullable=False, index=True)
    signed_qty = Column(Float, nullable=False)     # +buy / -sell
    price = Column(Float, nullable=False)
    fees = Column(Float, nullable=False, default=0.0)
    filled_at = Column(DateTime(timezone=True), nullable=False, index=True)
    rule_tag = Column(String, nullable=True)
    source = Column(String, nullable=False, default="broker")  # broker|manual
    order_level_avg = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("connection_id", "broker_exec_id",
                                       name="uq_exec_conn_execid"),)


class ReconcileFrame(Base):
    """One recorded sweep: everything reconcile() was handed and everything
    it produced (frames.py).

    The other three tables say what the executor DID. This one says what it
    was TOLD, which is the half a replay needs and which nothing else keeps.
    Written off the sweep's lock by the recorder's own thread, sampled when
    a pass was quiet, and dropped after 30 days.
    """
    __tablename__ = "reconcile_frames"
    id = Column(String, primary_key=True, default=_uuid)
    connection_id = Column(String, ForeignKey("broker_connections.id"),
                           nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    mode = Column(String, nullable=False)         # fast | audit | shadow
    kept = Column(String, nullable=False)         # active | sampled
    frame = Column(JSONB, nullable=False)
    __table_args__ = (Index("ix_reconcile_frames_created", "created_at"),)


# LEAN's order-state vocabulary, verbatim in spirit: an order is born `new`,
# reaches the venue as `submitted`, accretes fills through
# `partially_filled` to `filled`, or terminates `canceled` / `rejected` /


class KV(Base):
    """Small operational key-value store (e.g. the rotating Schwab refresh
    token) so restarts and redeploys keep runtime credentials."""
    __tablename__ = "kv"
    key = Column(String, primary_key=True)
    value = Column(JSONB, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class BarDay(Base):
    """One symbol-day of minute bars cached from the live feed (accruing archive).
    rows: [[ms_since_midnight_ET, o, h, l, c, v], ...] with raw float prices."""
    __tablename__ = "bar_days"
    symbol = Column(String, primary_key=True)
    day = Column(Date, primary_key=True)
    rows = Column(JSONB, nullable=False)
    source = Column(String, nullable=False, default="schwab")
    fetched_at = Column(DateTime(timezone=True), default=_now)


class SecondBarDay(Base):
    """One symbol-day of SECOND bars consolidated live from the tick stream
    (second-resolution spec §6). Same row shape as BarDay at 1s buckets;
    only symbols traded by running second-resolution deployments are ever
    recorded, and rows older than the retention window are exported to the
    npz cache and deleted (the nightly maintenance job)."""
    __tablename__ = "second_bar_days"
    symbol = Column(String, primary_key=True)
    day = Column(Date, primary_key=True)
    rows = Column(JSONB, nullable=False)
    source = Column(String, nullable=False, default="schwab-l1")
    fetched_at = Column(DateTime(timezone=True), default=_now)


class HistBar(Base):
    """One symbol-day of minute bars backfilled from Alpaca for backtests.
    Same row shape as BarDay but TOTAL-RETURN adjusted (splits + dividends baked
    in, adjustment=all) — the engine has no corporate-action handling, so this
    is the only way arbitrary tickers backtest correctly. Kept separate from
    bar_days, which is the raw-price live-feed record deployments replay."""
    __tablename__ = "hist_bars"
    symbol = Column(String, primary_key=True)
    day = Column(Date, primary_key=True)
    rows = Column(JSONB, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=_now)


def init_db():
    """Create every table declared on `Base`, then bring the columns this
    package has added over time onto a database that predates them.

    Idempotent `ADD COLUMN IF NOT EXISTS` rather than a migration tool, on
    purpose: the same list has to run against a hosted database that already
    has every column and against a self-hoster's empty one, and it has to be
    safe to run on every boot. `create_all` covers whatever is registered on
    `Base` when it runs, so a host that declares its own tables on the same
    Base gets them from this one call, in dependency order.
    """
    Base.metadata.create_all(engine)
    from sqlalchemy import text
    with engine.begin() as c:
        c.execute(text("ALTER TABLE broker_connections ADD COLUMN IF NOT "
                       "EXISTS mode VARCHAR NOT NULL DEFAULT 'paper'"))
        c.execute(text("ALTER TABLE broker_connections ADD COLUMN IF NOT "
                       "EXISTS settings JSONB"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "live_confirmed BOOLEAN NOT NULL DEFAULT FALSE"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "kind VARCHAR NOT NULL DEFAULT 'blocks'"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "code TEXT"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "universe JSONB"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "resolution VARCHAR"))
        c.execute(text("ALTER TABLE deployments ALTER COLUMN ir DROP NOT NULL"))
        # Backfill the two lifted columns from the IR for existing rows. The
        # WHERE clauses make this a no-op on every run after the first.
        c.execute(text(
            "UPDATE deployments SET universe = ir->'universe'->'static' "
            "WHERE universe IS NULL AND ir IS NOT NULL"))
        c.execute(text(
            "UPDATE deployments SET resolution = ir->'meta'->>'resolution' "
            "WHERE resolution IS NULL AND ir IS NOT NULL"))
        c.execute(text("ALTER TABLE broker_orders ADD COLUMN IF NOT EXISTS "
                       "rule_tag VARCHAR"))
        c.execute(text("ALTER TABLE broker_connections ADD COLUMN IF NOT "
                       "EXISTS execution_truth VARCHAR NOT NULL "
                       "DEFAULT 'off'"))
        c.execute(text("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
                       "reconciled_from DATE"))
