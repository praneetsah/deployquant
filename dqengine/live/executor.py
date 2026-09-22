"""Broker-agnostic mirror executor. compute_targets() and reconcile() are
pure (no DB, adapter injected) — the brain stays in the replay, these are the
hands. sync_broker_account() is the thin DB wrapper the driver's ticker
calls.

Preserves the original Alpaca executor's contract exactly: client_order_id
prefixes sl-mkt-/sl-tp-{dep_id[:18]}, market deltas guarded by in-flight
orders, per-deployment GTC take-profit limits."""
import hashlib
import math
import os
import re
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dqengine.live import capabilities
from dqengine.adapters.base import (BrokerAuthExpired, BrokerRejected,
                                  BrokerUnavailable, OrderNotSupported)

from dqengine.runtime.core.data import SessionCalendar, is_market_holiday  # noqa: E402

ET = ZoneInfo("America/New_York")

# how long a symbol sits out the ordinary market-delta reconciliation pass
# after a quote-driven fast-path exit (execute_quote_breach_exit) -- long
# enough to comfortably span one minute-bar close (the replay's own
# bar-driven accounting catches up on the next closed bar), short enough
# that a real, unrelated desired-qty change for that symbol isn't stuck
# waiting behind it for long.
QB_COOLDOWN_S = 90

# Per-connection sweep pacing (lean-fills spec §3.5). The floor collapses
# bursts of sync events (one per ticked deployment per bar) into one sweep
# per SYNC_FLOOR_S; the cooldown answers a broker rate-limit refusal the
# way the FIXGW backoff answers a closed gateway: one loud line and a
# scheduled pause, never hammering. In-memory is fine: the gate only paces,
# it never protects -- one writer at a time is the two locks below (threads:
# _conn_lock, processes: conn_sweep_lock) -- and a restart merely forgets a
# pause that the next rate-limit refusal would re-establish.
SYNC_FLOOR_S = 10
RATE_LIMIT_COOLDOWN_S = 45
_SYNC_GATE: dict = {}     # conn_id -> {"last": epoch_s, "cooldown_until": s}

# one transmitter at a time, enforced with a per-connection lock shared by
# the fast path and the auditor (direct-submit spec §6)
import threading as _threading
_CONN_LOCKS: dict = {}
_CONN_LOCKS_GUARD = _threading.Lock()


def _conn_lock(conn_id: str):
    with _CONN_LOCKS_GUARD:
        return _CONN_LOCKS.setdefault(conn_id, _threading.Lock())


# ...and one transmitting PROCESS at a time, enforced with a Postgres
# advisory lock (Phase 3 spec §6.6, decision Q5). _conn_lock only binds the
# threads of one interpreter: a rolling deploy runs two api containers for a
# minute, and a self-hoster can start the live command twice. The journal's
# UNIQUE(conn, cid) collapses duplicate market deltas, but exits and entries
# carry a random cid suffix, so two processes sweeping one account would both
# send them.
def conn_sweep_key(conn_id: str) -> int:
    """The connection's advisory-lock key: signed int64, what Postgres takes.

    sha256, deliberately not Python's hash(), which is salted per process --
    the two builds overlapping in a rolling deploy are exactly the case this
    lock exists for, and they must agree on the key."""
    digest = hashlib.sha256(f"dq-conn-sweep:{conn_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _sweep_lock_bind():
    """The engine behind the same database the sweep itself writes to (tests
    rebind SessionLocal to their own)."""
    from dqengine.live import persistence
    bind = persistence.SessionLocal.kw.get("bind")
    if bind is None:
        bind = persistence.engine
    return bind


@contextmanager
def conn_sweep_lock(conn_id: str):
    """Yields True while THIS process owns the connection's sweep, False if
    another process holds it or the lock could not be taken at all.

    SESSION-level lock on its own connection, held for the whole sweep and
    released in the finally: the sweep's own commits and rollbacks must not
    touch it, and a connection still holding it must never go back into the
    pool. A process that dies holding it is released by Postgres.

    Not re-entrant, and it does not need to be: every caller takes the
    in-process _conn_lock first (a plain, non-reentrant Lock), so a nested
    sweep on one connection would already deadlock there today."""
    from sqlalchemy import text
    key = conn_sweep_key(conn_id)
    conn = None
    held = False
    try:
        bind = _sweep_lock_bind()
        if bind.dialect.name == "postgresql":
            # AUTOCOMMIT: an advisory lock is session-scoped either way, and
            # a connection parked idle-in-transaction for a whole sweep
            # blocks DDL behind it
            conn = bind.connect().execution_options(
                isolation_level="AUTOCOMMIT")
            held = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                     {"k": key}).scalar())
        else:
            held = True         # no advisory locks outside Postgres
    except Exception as e:
        # fail CLOSED: a sweep cannot do its job without the database, and
        # transmitting while unable to prove we are the only writer is the
        # one outcome this lock exists to prevent
        print(f"[exec] connection {conn_id}: could not take the sweep lock: "
              f"{e!r}", flush=True)
        held = False
    try:
        yield held
    finally:
        if conn is not None:
            try:
                if held:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                                 {"k": key})
                conn.close()
            except Exception as e:
                print(f"[exec] connection {conn_id}: releasing the sweep "
                      f"lock failed: {e!r}", flush=True)
                conn.invalidate()   # never pool a connection that may
                conn.close()        # still be holding the lock


class BookAdapter:
    """The fast path's view (direct-submit spec §4): reconcile() reads the
    account from the BOOK (0ms) and mutations go to the real adapter AND
    the book. The order dict recorded on ack is built from OUR submit
    args, never from the adapter's return shape."""

    def __init__(self, inner, book):
        self._inner = inner
        self._book = book
        self.caps = inner.caps
        self.id = getattr(inner, "id", "?")

    def positions(self, creds):
        return self._book.positions_view()

    def open_orders(self, creds):
        return self._book.open_orders_view()

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None):
        cid = client_order_id or ""
        self._book.note_submit(cid, symbol, side, qty)
        try:
            out = self._inner.submit(
                creds, symbol, qty, side, order_type=order_type, tif=tif,
                limit_price=limit_price, stop_price=stop_price,
                trail_percent=trail_percent, extended_hours=extended_hours,
                client_order_id=client_order_id)
        except Exception:
            self._book.note_reject(cid)
            raise
        # ALWAYS ack with the order dict -- keyed by client id when the
        # venue returns no broker id (Webull), so the book never goes
        # blind to its own submit (the 2026-08-31 duplicate-order bug)
        oid = (out.get("id") if isinstance(out, dict) else "") or ""
        self._book.note_ack(cid, {
            "id": oid, "symbol": symbol.upper(), "qty": float(qty),
            "side": side, "type": order_type, "limit_price": limit_price,
            "stop_price": stop_price, "trail_percent": trail_percent,
            "client_order_id": cid,
            "status": (out.get("status") if isinstance(out, dict)
                       else "") or ""})
        return out

    def cancel(self, creds, order_id):
        out = self._inner.cancel(creds, order_id)
        self._book.note_cancel(order_id)
        return out

    def replace(self, creds, order_id, qty=None, limit_price=None):
        out = self._inner.replace(creds, order_id, qty=qty,
                                  limit_price=limit_price)
        with self._book.lock:
            o = self._book.open_orders.get(order_id)
            if o is not None:
                if qty is not None:
                    o["qty"] = float(qty)
                if limit_price is not None:
                    o["limit_price"] = limit_price
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


class AuditAdapter:
    """The auditor's view: REAL fetches (cached on the instance so the
    pass can apply them to the book), and mutations either pass through
    (transmit=True -- the fast path does not own this connection) or are
    RECORDED ONLY (transmit=False -- any would-be action is evidence of
    book drift, never an order to place; spec §6)."""

    def __init__(self, inner, book, transmit: bool):
        self._inner = inner
        self._book = book
        self.transmit = transmit
        self.caps = inner.caps
        self.id = getattr(inner, "id", "?")
        self.fetched_positions = None
        self.fetched_open_orders = None
        self.recorded: list = []

    def positions(self, creds):
        self.fetched_positions = self._inner.positions(creds)
        return self.fetched_positions

    def open_orders(self, creds):
        self.fetched_open_orders = self._inner.open_orders(creds)
        return self.fetched_open_orders

    def _mutate(self, kind, symbol, detail, fn):
        if not self.transmit:
            self.recorded.append({"kind": kind, "symbol": symbol,
                                  "detail": detail})
            return None
        return fn()

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None):
        def _do():
            # 2026-09-01: the auditor's own submits must enter the book's
            # settle window too, or apply_audit wipes their ack with the
            # pre-submit fetch and the fast path re-sends them
            self._book.note_submit(client_order_id or "", symbol, side, qty)
            try:
                out = self._inner.submit(
                    creds, symbol, qty, side, order_type=order_type,
                    tif=tif, limit_price=limit_price, stop_price=stop_price,
                    trail_percent=trail_percent,
                    extended_hours=extended_hours,
                    client_order_id=client_order_id)
            except Exception:
                self._book.note_reject(client_order_id or "")
                raise
            oid = (out.get("id") if isinstance(out, dict) else "") or ""
            self._book.note_ack(client_order_id or "", {
                "id": oid, "symbol": symbol.upper(), "qty": float(qty),
                "side": side, "type": order_type,
                "limit_price": limit_price, "stop_price": stop_price,
                "trail_percent": trail_percent,
                "client_order_id": client_order_id or "",
                "status": (out.get("status") if isinstance(out, dict)
                           else "") or ""})
            return out
        return self._mutate("submit", symbol.upper(),
                            f"{side} {qty} {order_type}", _do)

    def cancel(self, creds, order_id):
        def _do():
            out = self._inner.cancel(creds, order_id)
            self._book.note_cancel(order_id)
            return out
        sym = ""
        oo = self.fetched_open_orders or []
        for o in oo:
            if o.get("id") == order_id:
                sym = (o.get("symbol") or "").upper()
        return self._mutate("cancel", sym, order_id, _do)

    def replace(self, creds, order_id, qty=None, limit_price=None):
        def _do():
            return self._inner.replace(creds, order_id, qty=qty,
                                       limit_price=limit_price)
        return self._mutate("replace", "", order_id, _do)

    def __getattr__(self, name):
        return getattr(self._inner, name)


CLOSE_WINDOW_BEFORE_S = 180
CLOSE_WINDOW_AFTER_S = 120


def _in_close_window(now: Optional[datetime] = None) -> bool:
    """True inside [close-180s, close+120s] on a session day -- the minutes
    where a skipped sync means lost orders, not a few seconds' delay."""
    now = (now or datetime.now(ET))
    if not _is_session_day(now.date()):
        return False
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    close_s = SessionCalendar.close_time_ms(now.date()) / 1000.0
    return (close_s - CLOSE_WINDOW_BEFORE_S <= now_s
            <= close_s + CLOSE_WINDOW_AFTER_S)


def _poll_into(report: dict, adapter, creds, conn_id: str) -> None:
    try:
        new, skipped = _poll_executions(adapter, creds, conn_id)
        report["executions"]["new"] = new
        report["executions"]["skipped"] = skipped
        if skipped:
            # I6: rows the adapter could not parse read as UNKNOWN, not as
            # "the broker did not fill this" -- same flag a broker outage
            # raises (the driver's unknown-symbol set).
            report["executions"]["error"] = (
                f"{skipped} execution row(s) could not be parsed and were "
                f"skipped — fill state is unknown, not empty")
    except Exception as e:
        report["executions"]["error"] = str(e)[:300]


def _rate_limited(report: dict) -> bool:
    texts = [str(t) for t in report.get("errors", [])]
    ee = (report.get("executions") or {}).get("error")
    if ee:
        texts.append(str(ee))
    return any("rate-limited" in t.lower() or "too many request" in t.lower()
               or "to many request" in t.lower() for t in texts)

# I7: broker refusal codes that mean "the market gateway isn't ready", not
# "this order is bad" -- Webull's Caps comment documents that MARKET/day-TIF
# orders are refused for exactly this timing reason while the market is
# closed, never as an unsupported feature. Matched case-insensitively as a
# substring of the adapter's BrokerRejected message (see
# dqengine_webull/adapter.py's "Webull refused the request: {msg}"). Easy to
# extend as other brokers' equivalents turn up.
MARKET_NOT_READY_CODES = frozenset({
    # common prefix of Webull's FIXGW variants (_MARKET, _NIGHT, ...) —
    # matched as a substring, so the suffix is deliberately left off
    "CAN_NOT_TRADING_FOR_FIXGW_NOT_READY",
    "MARKET_NOT_READY",
    "DAY_ORDER_NOT_ALLOWED_AFT_CORE_TIME",
})

# Backoff after a market-not-ready refusal received while our calendar says
# the session is OPEN right now -- a genuine broker outage, not routine, so
# retry almost immediately (60-120s) rather than let it look like a routine
# closed-market refusal.
OPEN_SESSION_BACKOFF_S = 90

OPEN_S = 9 * 3600 + 30 * 60         # 09:30 ET, seconds since midnight
PRE_MARKET_OPEN_S = 4 * 3600        # 04:00 ET -- Webull's extended-hours
                                    # session start (Caps.extended_hours=True)


def _is_market_not_ready(msg: str) -> bool:
    upper = (msg or "").upper()
    return any(code in upper for code in MARKET_NOT_READY_CODES)


def _is_session_day(d) -> bool:
    return d.weekday() < 5 and not is_market_holiday(d)


def _market_open_now(now: datetime) -> bool:
    """Session-calendar open/closed check (holidays + early closes) -- the
    sole signal the asymmetric backoff uses to pick a 60-120s outage retry
    vs. a wait-for-the-next-gateway-window backoff."""
    now = now.astimezone(ET)
    if not _is_session_day(now.date()):
        return False
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    close_s = SessionCalendar.close_time_ms(now.date()) / 1000.0
    return OPEN_S <= now_s < close_s


def _next_gateway_retry_time(now: datetime) -> datetime:
    """The next time worth retrying a market-not-ready refusal, when our
    calendar says the session is CLOSED right now: pre-market open
    (04:00 ET) first, THEN the regular open (09:30 ET) if pre-market has
    already passed for the day, THEN the next trading day's pre-market
    open. Deliberately NOT just "wait for the regular session" -- Webull's
    Caps already declare extended_hours=True, and the brief is explicit
    that Webull, not us, is the arbiter of what it accepts; targeting only
    09:30 would suppress the 04:00-09:29 window Webull might actually take
    an order in, which is exactly the kind of pre-judging the brief says
    not to do. This also self-corrects a routine evening refusal down to
    about two refusal/backoff cycles a day (one at each open) instead of
    one every ~30 minutes overnight."""
    now = now.astimezone(ET)
    d = now.date()
    if _is_session_day(d):
        for anchor_s in (PRE_MARKET_OPEN_S, OPEN_S):
            candidate = datetime.combine(
                d, datetime.min.time(), tzinfo=ET) + timedelta(seconds=anchor_s)
            if now < candidate:
                return candidate
    d += timedelta(days=1)
    while not _is_session_day(d):
        d += timedelta(days=1)
    return datetime.combine(
        d, datetime.min.time(), tzinfo=ET) + timedelta(seconds=PRE_MARKET_OPEN_S)


@dataclass
class Rails:
    # Notional caps are OFF by default (None). They are position-sizing
    # policy, not a safety rail: the strategy decided the size, and a cap we
    # picked would bind arbitrarily depending on how big the account is. When
    # a user does set one we REFUSE the order rather than silently resizing
    # it — a refusal you can read in the execution log beats a position
    # quietly smaller than the strategy asked for.
    max_order_notional: float = None
    max_position_notional: float = None
    max_orders_per_sync: int = 20
    price_band_pct: float = 50.0   # absurdity cap, not a distance limit
    cross_tol_pct: float = 1.0     # how far a limit may cross the market
    stop_band_pct: float = 50.0   # stops are meant to sit far from market;
                                   # this only catches an absurdly-far value
    paused: bool = False
    dry_run: bool = False
    live_allowed: bool = True     # False = live-money conn missing confirmation


def _cap(v):
    """A notional cap: None/0/negative all mean "no cap". Lets a user clear a
    cap by setting it to 0 as well as by never setting one."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _poll_executions(adapter, creds, conn_id):
    """Seam for tests; the real work lives in executions.poll."""
    from dqengine.live import executions
    return executions.poll(adapter, creds, conn_id)


def rails_from(settings: dict, mode: str, all_live_confirmed: bool) -> Rails:
    s = settings or {}
    return Rails(
        max_order_notional=_cap(s.get("max_order_notional")),
        max_position_notional=_cap(s.get("max_position_notional")),
        max_orders_per_sync=int(s.get("max_orders_per_sync", 20)),
        price_band_pct=float(s.get("price_band_pct", 50.0)),
        cross_tol_pct=float(s.get("cross_tol_pct", 1.0)),
        stop_band_pct=float(s.get("stop_band_pct", 50.0)),
        paused=bool(s.get("paused", False)),
        dry_run=bool(s.get("dry_run", False)),
        live_allowed=(mode != "live") or all_live_confirmed,
    )


# Preference order for a RESTING order's time-in-force. GTC first: a resting
# exit should survive the session, and a DAY stop that expires overnight
# leaves a real position unprotected at the next open. DAY is the fallback
# for venues (or types) that refuse GTC.
_RESTING_TIF_PREFERENCE = ("gtc", "day")


def pick_tif(caps, order_type: str, preferred: str = "gtc") -> str:
    """The best time-in-force this venue will accept FOR THIS ORDER TYPE.

    The executor used to hard-code GTC on every resting order. That is
    wrong wherever a venue narrows the tifs of one type -- Webull takes GTC
    generally but a TRAILING_STOP_LOSS only in DAY -- and the order was
    refused for a reason no log explained.

    Degrades rather than failing: an exit that rests for a day is worth more
    than an exit that was never placed. Raises only when the venue accepts
    nothing at all for the type, which is a capability error worth hearing.
    """
    allowed = caps.tifs_for(order_type)
    if preferred in allowed:
        return preferred
    for candidate in _RESTING_TIF_PREFERENCE:
        if candidate in allowed:
            return candidate
    for candidate in sorted(allowed):
        return candidate
    raise OrderNotSupported(
        f"no time-in-force available for {order_type} at this broker")


def limit_price_refusal(side, level, px, rails):
    """Why this limit price must not be sent, or None if it's fine.

    The hazard in a limit price is not its DISTANCE from the market, it's
    whether it CROSSES it. A sell below the bid or a buy above the offer
    executes immediately against whatever is resting there, so a stale or
    miscomputed level becomes an instant fill at a price the strategy never
    intended. A limit on the passive side just rests — that is precisely what
    a take-profit is — so distance alone is not a defect.

    The original symmetric band had this exactly backwards: it refused
    take-profits (harmless, and the whole point of the order) while waving
    through a sell priced 4% under the market (an instant bad fill). Stops
    already reasoned directionally — "a sell stop at or above the last price
    would trigger instantly" — limits simply never got the same treatment.

    price_band_pct survives as a wide absurdity cap, to catch a decimal slip
    like 4000 for 400.00 rather than to bound where a target may sit.
    """
    if px <= 0 or level is None:
        return None
    if side == "sell" and level < px * (1 - rails.cross_tol_pct / 100):
        return (f"would fill immediately, ~{(1 - level / px) * 100:.1f}% "
                f"below the market (last {px:.2f})")
    if side == "buy" and level > px * (1 + rails.cross_tol_pct / 100):
        return (f"would fill immediately, ~{(level / px - 1) * 100:.1f}% "
                f"above the market (last {px:.2f})")
    if abs(level / px - 1) * 100 > rails.price_band_pct:
        return (f"{abs(level / px - 1) * 100:.0f}% from last {px:.2f} — "
                f"outside the {rails.price_band_pct}% sanity band")
    return None


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class DesiredState:
    """What ONE broker connection's executor is told to want.

    The same values the three compute_* passes produced, read per deployment
    instead of per connection: reconcile() still takes this dict and these
    tuples, unchanged. `desired` is insertion-ordered -- each deployment's
    universe seeded at 0.0 in universe order, then its holdings -- and
    reconcile() walks it in that order, so the order decides which deltas the
    max_orders_per_sync budget cuts at the close (H33)."""
    desired: dict         # {SYM: signed target qty}, insertion-ordered
    exit_wants: tuple     # (owner, SYM, |qty|, kind, price, stop, trail, rule, side)
    entry_wants: tuple    # (owner, SYM, qty, kind, price, stop, rule, side)
    close_wants: tuple    # (owner, SYM, signed_qty, kind, price, rule)
    last_px: dict         # {SYM: float}
    held_px: frozenset    # symbols whose last_px came from a HOLDING's
    # last_price. A holding's price OVERWRITES; the primary symbol's and
    # `last_prices`' only fill a gap. Folding two deployments needs to know
    # which is which, or dep1's last_prices quietly beats dep2's holding and
    # the limit/stop rails and the notional caps read a stale price.


@dataclass(frozen=True)
class Owner:
    """One deployment feeding this connection, as the rails see it."""
    id: str
    live_confirmed: bool
    fills_today: tuple    # ((SYM, signed qty), ...) confirmed, today, in
    # payload order: the fold rail sums them sequentially and signed, and
    # pre-aggregating them per owner would change the summation order.


@dataclass(frozen=True)
class ConnectionInputs:
    state: DesiredState
    owners: tuple         # () means no managed deployment


class MultiDeploymentConnection(RuntimeError):
    """More than one deployment on a connection, and no combiner to fold
    their desired states into one."""


_COMBINER = None


def set_combiner(fn) -> None:
    """Install the cross-deployment combiner: fn(list[DesiredState]) ->
    DesiredState. One account shared by several strategies is the platform's
    job, not the engine's; without it a second deployment is refused rather
    than traded around."""
    global _COMBINER
    _COMBINER = fn


def desired_from_payload(dep_id, pos, universe) -> DesiredState:
    """One deployment's position payload -> what it wants at the broker.

    `dep_id`, `pos`, `universe` are one of the (dep_id, position_dict,
    universe_list) tuples the old sync_alpaca_account built. Each exit_wants
    tuple carries an 8th `rule_id` element (the replay's own rule/target id,
    e.g. open_orders_payload's "rule") so two same-kind exits resting on the
    same symbol get distinguishable cid identities — see _rule_token; the
    6th element of a close want and the 7th of an entry want are the same
    id, for the same reason."""
    desired, exit_wants, last_px = {}, [], {}
    held_px = set()
    for u in universe:
        desired.setdefault(u.upper(), 0.0)
    holdings = pos.get("holdings")
    if holdings is None:
        sym = (pos.get("symbol") or "").upper()
        holdings = ([{"symbol": sym, "qty": float(pos.get("qty", 0)),
                      "last_price": pos.get("last_price")}] if sym else [])
    qty_by_sym = {}
    for h in holdings:
        hs = (h.get("symbol") or "").upper()
        if not hs:
            continue
        q = float(h.get("qty", 0))
        qty_by_sym[hs] = qty_by_sym.get(hs, 0.0) + q
        desired[hs] = desired.get(hs, 0.0) + q
        if h.get("last_price"):
            last_px[hs] = float(h["last_price"])
            held_px.add(hs)          # this one ASSIGNS; the two below don't
    if pos.get("last_price") and (pos.get("symbol") or "").upper():
        last_px.setdefault((pos["symbol"]).upper(),
                           float(pos["last_price"]))
    # I6: `last_prices` (the python runtime's replay payload) carries every
    # universe symbol's last price, not just held ones plus the
    # primary — a breakout entry is by construction placed while FLAT
    # on a non-primary symbol, so without this the buy-stop rail below
    # never sees a price for it and refuses forever.
    if isinstance(pos.get("last_prices"), dict):
        for sym, price in pos["last_prices"].items():
            sym_u = (sym or "").upper()
            if sym_u and price:
                last_px.setdefault(sym_u, float(price))
    for o in pos.get("open_orders", []):
        osym = (o.get("symbol") or pos.get("symbol") or "").upper()
        oqty = qty_by_sym.get(osym, 0.0)
        if not osym or oqty == 0:
            continue
        otype = o.get("type")
        kind = "limit" if otype == "limit_sell" else otype
        if kind not in ("limit", "stop", "stop_limit", "trailing_stop"):
            continue
        # An exit closes whatever is held: a long is sold, a short is
        # bought back. qty is the MAGNITUDE and side carries the
        # direction, so nothing downstream has to infer it from a sign.
        exit_wants.append((
            dep_id, osym, abs(oqty), kind,
            float(o["price"]) if o.get("price") is not None else None,
            float(o["stop"]) if o.get("stop") is not None else None,
            float(o["trail_pct"]) if o.get("trail_pct") is not None else None,
            o.get("rule"),
            "sell" if oqty > 0 else "buy"))
    # today's at_close_order intents (market_on_close / limit_on_close), so
    # reconcile() can place them natively where the broker supports the kind.
    #
    # Dead between 2026-09-14 and 2026-09-21: the IR engine's at-close
    # preview was the only producer and it went with the IR live path. The
    # producer now is the driver's daily_preview, for DAILY deployments only
    # — that is the one resolution where a resting on-close ticket exists and
    # there is a moment to place it in. At every other resolution the list is
    # still empty.
    #
    # The timing lives entirely in that preview. This computation and
    # reconcile() have no clock: they place whatever the payload publishes,
    # the moment they are called. A daily deployment on a venue with a native
    # on-close order publishes its entry when the ticket rests (before the
    # exchange's cutoff); everywhere else it publishes a minute before the
    # close, and the note from the capability ladder plus the market-delta
    # pass make that the near-close market emulation.
    close_wants = []
    for o in pos.get("close_orders", []):
        sym = (o.get("symbol") or "").upper()
        kind = o.get("type")
        if not sym or kind not in ("market_on_close", "limit_on_close"):
            continue
        close_wants.append((dep_id, sym, float(o.get("qty", 0)), kind,
                            float(o["price"]) if o.get("price") is not None else None,
                            o.get("rule")))
    # resting BUY entry orders (breakout stop / pullback limit / stop_limit —
    # strategy-ir-spec.md §15.5), rested natively where the broker's
    # Caps.order_types allow the kind.
    entry_wants = []
    for o in pos.get("entry_orders", []):
        sym = (o.get("symbol") or "").upper()
        kind = o.get("type")
        if not sym or kind not in ("limit", "stop", "stop_limit"):
            continue
        qty = float(o.get("qty", 0))
        if qty <= 0:
            continue
        # 8th element: the entry's SIDE. IR entries carry no `side` key
        # and are long by construction, so the default keeps that path
        # byte-identical. A python entry may be a short — and the submit
        # path below used to hard-code side="buy", which would have sent
        # it to the venue as a BUY (a wrong-way position, in real money).
        # Whether the venue will accept an opening short is decided by
        # Caps.supports_short, locally, before anything is sent.
        entry_wants.append((
            dep_id, sym, qty, kind,
            float(o["price"]) if o.get("price") is not None else None,
            float(o["stop"]) if o.get("stop") is not None else None,
            o.get("rule"), (o.get("side") or "buy")))
    return DesiredState(desired=desired, exit_wants=tuple(exit_wants),
                        entry_wants=tuple(entry_wants),
                        close_wants=tuple(close_wants), last_px=last_px,
                        held_px=frozenset(held_px))


def owner_of(dep, today_iso) -> Owner:
    """A deployment row as the connection's rails read it.

    `fills_today` is the rail input (lean-fills spec §3.4): what this sleeve
    has FOLDED today -- confirmed fills only. Unconfirmed model fills are the
    normal model-ahead-of-broker state of a just-signalled order and must not
    freeze anything. `today_iso` is passed in so one sweep reads the ET clock
    once for every deployment, as it always has."""
    return Owner(
        id=dep.id, live_confirmed=dep.live_confirmed,
        fills_today=tuple(
            ((f.get("sym") or "").upper(), float(f.get("qty") or 0.0))
            for f in (dep.fills or [])
            if f.get("day") == today_iso and f.get("confirmed")))


def connection_inputs(dep_states, conn_id, owners=()) -> ConnectionInputs:
    """The one desired state this connection's sweep reconciles against.

    Pure: the rows were read in the session block of the caller. One
    deployment is the open engine's case and its state is used as it stands.
    More than one is the platform's, and the installed combiner folds them in
    deployment order; with none installed nothing is sent, because an
    executor that silently traded one of several sleeves' targets would
    flatten the others."""
    states = [desired_from_payload(dep_id, pos, universe)
              for dep_id, pos, universe in dep_states]
    if not states:
        state = DesiredState(desired={}, exit_wants=(), entry_wants=(),
                             close_wants=(), last_px={},
                             held_px=frozenset())
    elif len(states) == 1:
        state = states[0]
    elif _COMBINER is None:
        raise MultiDeploymentConnection(
            f"connection {conn_id}: {len(states)} deployments are running "
            f"or paused on this broker connection and no combiner is "
            f"installed. Nothing was sent. The open engine trades one "
            f"deployment per connection: stop or move the others. "
            f"Deployments: {', '.join(str(d) for d, _p, _u in dep_states)}")
    else:
        state = _COMBINER(states)
    return ConnectionInputs(state=state, owners=tuple(owners))


# ---------------------------------------------------- quote-driven fast path

def quote_breaches_stop(kind: str, stop_price, last_price,
                        side: str = "sell") -> bool:
    """Same breach condition engine.py's `_check_targets` applies for
    stop/trailing_stop kinds -- strict: the bar's low crosses the stop, i.e.
    `l < stop` -- applied to a live quote's last-trade price as the
    single-point analogue of a bar's low (a quote carries no OHLC). Pure and
    independently testable without a live broker or quote-feed connection; see
    session-timing-and-realtime-design.md §3/§4."""
    if kind not in ("stop", "trailing_stop"):
        return False
    if stop_price is None or last_price is None:
        return False
    # Mirrored by side: a long's sell-stop is breached from ABOVE (price
    # falls through it), a short's buy-stop from BELOW (price rises through
    # it). Same condition, reflected -- not a different rule.
    if side == "buy":
        return float(last_price) > float(stop_price)
    return float(last_price) < float(stop_price)


def execute_quote_breach_exit(conn_id: str, dep_id: str, symbol: str,
                              qty: float, rule_id: str = None) -> Optional[dict]:
    """Fast path for a SIMULATED exit (a kind this broker cannot rest
    natively -- e.g. a Webull trailing stop) whose level a live
    LEVELONE_EQUITIES quote just breached (the live quote feed). Submits
    a plain market order directly, bypassing the bar-driven replay -- which
    cannot see this breach until the current minute's bar closes, ~60s away
    -- so the exit reaches the broker in the same tick the quote proved it,
    instead of waiting on the next poll. The DECISION (breach) is identical
    to what the bar-driven path would eventually have produced
    (`quote_breaches_stop` mirrors `_check_targets` exactly); only detection
    latency improves.

    `qty` is SIGNED: negative to sell a long, positive to buy back a short
    (same convention as exit_wants / compute_targets' `desired`).

    Deliberately does NOT touch `desired`/the replay -- see reconcile()'s
    "2) market deltas" `qb_cooldown` handling (sync_broker_account) for how
    the resulting brief replay/broker divergence is kept from causing a
    spurious buy-back before the next closed bar lets the replay catch up
    on its own. Idempotent within QB_COOLDOWN_S: a burst of quote frames all
    breaching the same level submits at most one exit per (deployment,
    symbol) per window."""
    # Deliberately outside the cross-process sweep lock too, like the book,
    # the journal and _conn_lock: this path moves as it is (spec H43,
    # decision Q8) and its own cooldown row is its idempotency.
    from dqengine.live import vault
    from dqengine.adapters import catalog as registry
    from dqengine.live.persistence import (BrokerConnection, BrokerOrder,
                                           Deployment, SessionLocal)

    sym = symbol.upper()
    qabs = abs(qty)
    if qabs <= 0:
        return None
    with SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None or not conn.creds_encrypted:
            return None
        if conn.status in ("reconnect_needed", "error", "pending"):
            return None
        try:
            adapter = registry.get_adapter(conn.broker)
        except (KeyError, LookupError):
            return None
        dep = s.get(Deployment, dep_id)
        rails = rails_from(conn.settings, conn.mode,
                           bool(dep.live_confirmed) if dep is not None else False)
        if rails.paused or not rails.live_allowed:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=QB_COOLDOWN_S)
        existing = (s.query(BrokerOrder.id)
                   .filter(BrokerOrder.connection_id == conn_id,
                           BrokerOrder.deployment_id == dep_id,
                           BrokerOrder.symbol == sym,
                           BrokerOrder.action == "submit",
                           BrokerOrder.client_order_id.like("sl-qb-%"),
                           BrokerOrder.created_at >= cutoff)
                   .first())
        if existing is not None:
            return None
        qabs = _round_step(qabs, adapter.caps.qty_step)
        if qabs <= 0:
            return None
        creds = vault.decrypt_creds(conn.creds_encrypted)
        side = "sell" if qty < 0 else "buy"
        cid = f"sl-qb-{sym}-{dep_id[:8]}-{uuid.uuid4().hex[:8]}"
        entry = {"action": "submit", "symbol": sym, "qty": qabs, "side": side,
                 "order_type": "market", "limit_price": None,
                 "broker_order_id": "", "client_order_id": cid, "status": "",
                 "deployment_id": dep_id, "rule_tag": rule_id}
        if rails.dry_run:
            entry["status"] = "dry_run"
            s.add(broker_order_row(conn_id, entry))
            s.commit()
            return entry
        try:
            out = adapter.submit(creds, sym, qabs, side,
                                 client_order_id=cid)
        except BrokerRejected as e:
            entry = {**entry, "action": "refused", "status": str(e)[:120]}
            s.add(broker_order_row(conn_id, entry))
            s.commit()
            return entry
        if isinstance(out, dict):
            entry = {**entry, "broker_order_id": out.get("id", ""),
                     "status": out.get("status", "")}
        s.add(broker_order_row(conn_id, entry))
        s.commit()
        return entry


def _round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(abs(qty) / step) * step * (1 if qty >= 0 else -1)


# client-order-id discriminator per exit kind — each deployment's kinds must
# get distinct prefixes, or one kind's resting order gets mistaken for
# another's (e.g. a trailing stop finding a plain stop as "existing" and
# never getting placed).
_EXIT_CID_CODE = {"limit": "tp", "stop": "stp", "stop_limit": "stl",
                  "trailing_stop": "trl"}
_EXIT_CID_PREFIXES = tuple(f"sl-{c}-" for c in _EXIT_CID_CODE.values())

# Webull's clientOrderId field truncates at 40 chars total, and stop_limit
# (two legs to encode) is the tight case. MAX_CID_LEN is the hard ceiling
# every generated cid is checked against in _cid_with_level below — the
# guard exists so a future change to a prefix/token width can never
# silently regress into truncation again (a truncated token misparses,
# which resurrects the exact churn bug this whole level-token scheme
# exists to close).
MAX_CID_LEN = 40

# take-profit ("limit") keeps its original 18-char deployment-id slice —
# untouched, per the "sl-tp- format stays byte-identical" constraint (its
# symbol-collision fix lives in the matching predicate, not the cid text —
# see reconcile()). The other kinds use a shorter dep-id slice (still 16**4
# = 65,536 combinations — ample for one account's deployment count) to leave
# real headroom under MAX_CID_LEN for the symbol and rule tokens added below
# (C1: cid identity was (deployment, kind) only, which let two orders of the
# same kind on different symbols — or two same-kind rules on one symbol —
# be mistaken for one another, cancelling one and duplicating the other).
_DEP_ID_SLICE_LEN = 4
_TP_DEP_ID_SLICE_LEN = 18
_SYM_SLICE_LEN = 5
_RULE_TOKEN_LEN = 3   # base36 digits; 36**3 = 46,656 buckets


def _rule_token(rule_id) -> str:
    """Short, deterministic base36 hash of a rule_id, embedded in the cid so
    two rules of the same kind resting on the same symbol get distinct
    identities. Without it, the second rule's `existing` lookup on a later
    sweep would find the first rule's resting order (same symbol, same
    kind), read a mismatched reference level off it, and cancel+resubmit it
    — while the first rule's actual order sits there un-managed and a
    duplicate gets created for the second. Returns "" when rule_id is
    unavailable (e.g. an older payload) — the (deployment, kind, symbol)
    identity is still a strict improvement over the pre-fix
    (deployment, kind)-only blindness even without a rule token."""
    if not rule_id:
        return ""
    h = zlib.crc32(str(rule_id).encode()) % (36 ** _RULE_TOKEN_LEN)
    return "-" + _b36encode(h).rjust(_RULE_TOKEN_LEN, "0")


def _exit_cid_prefix(dep_id: str, kind: str, sym: str = "", rule_id=None) -> str:
    if kind == "limit":
        # byte-identical to the original take-profit format — see the
        # comment above. Symbol collisions for this kind are guarded at the
        # matching site (an o["symbol"] == sym check) instead of the cid.
        return f"sl-{_EXIT_CID_CODE['limit']}-{dep_id[:_TP_DEP_ID_SLICE_LEN]}"
    code = _EXIT_CID_CODE.get(kind, kind[:3])
    return (f"sl-{code}-{sym[:_SYM_SLICE_LEN]}-{dep_id[:_DEP_ID_SLICE_LEN]}"
            f"{_rule_token(rule_id)}")


# Resting BUY entry orders (breakout stop / pullback limit / stop_limit —
# strategy-ir-spec.md §15.5) get their own "en-" prefix family, distinct
# from the "sl-" exit family, so a symbol's resting sell exit and resting
# buy entry can never be mistaken for one another by client_order_id.
_ENTRY_CID_CODE = {"limit": "lmt", "stop": "stp", "stop_limit": "stl"}
_ENTRY_CID_PREFIXES = tuple(f"en-{c}-" for c in _ENTRY_CID_CODE.values())


def _entry_cid_prefix(dep_id: str, kind: str, sym: str = "", rule_id=None) -> str:
    code = _ENTRY_CID_CODE.get(kind, kind[:3])
    return (f"en-{code}-{sym[:_SYM_SLICE_LEN]}-{dep_id[:_DEP_ID_SLICE_LEN]}"
            f"{_rule_token(rule_id)}")


# --- level token embedded in the client_order_id -----------------------
# Some adapters' open-orders payload doesn't report the resting reference
# level at all (Webull: the one live shape actually captured — a LIMIT
# order, see webull.py's _norm — carries neither field; a resting
# STOP_LOSS/STOP_LOSS_LIMIT payload has not been observed). Without it,
# change detection can only ever notice a qty change, so a strategy that
# tightens a live protective stop's level (without changing qty) would
# silently go stale forever there.
#
# Since we already mint a fresh client_order_id for every exit we place,
# a short token encoding the level we asked for rides along for free and
# gives change detection a broker-independent fallback: decode the token
# off the *resting* order's own client_order_id and compare that against
# the newly wanted level, instead of trusting the broker to echo the level
# back. Two-decimal precision, base36-encoded (not hex) and fixed-width so
# stop_limit's two legs can be packed into one compact token — clientOrderId
# space is tight (Webull truncates to MAX_CID_LEN chars total).
_LEVEL_WIDTH = 5   # base36 digits; capacity 36**5 - 1 = 60,466,175 cents
                    # (~$604,661.75) — well past any level the rails would
                    # ever let through, with room to spare.
_B36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36encode(n: int) -> str:
    if n == 0:
        return "0"
    digits = []
    while n:
        n, r = divmod(n, 36)
        digits.append(_B36_ALPHABET[r])
    return "".join(reversed(digits))


def _b36decode(s: str):
    try:
        return int(s, 36)
    except ValueError:
        return None


def _quantize_level(value: float) -> float:
    """Round to the exact cents precision a round trip through
    _encode_level/_decode_level produces. Used once, on both sides of the
    fallback comparison (see below) — comparing a raw, un-quantized wanted
    level against an already-quantized decoded one would put any level
    whose distance to the nearest cent lands in (0.004, 0.005] on the
    wrong side of the 0.004 tolerance forever (e.g. a computed 0.95 x
    entry giving 380.0045), churning a live protective stop every sweep
    even though nothing actually changed."""
    return max(0, round(value * 100)) / 100.0


def _encode_level(value: float):
    """Fixed-width (_LEVEL_WIDTH chars), zero-padded base36 encoding of a
    quantized level's cents. Returns None if the value doesn't fit in
    that width — callers must skip embedding the token rather than
    truncate it, since a truncated token silently decodes to the wrong
    level, which is worse than carrying no fallback at all.

    round(), not int(): re-multiplying the already-quantized float by 100
    can land a hair under the true integer (e.g. 202.99999999999997 for
    2.03) due to binary float representation — int() would truncate that
    down to the wrong cent and silently desync from _quantize_level's own
    value."""
    cents = max(0, round(_quantize_level(value) * 100))
    if cents > 36 ** _LEVEL_WIDTH - 1:
        return None
    return _b36encode(cents).rjust(_LEVEL_WIDTH, "0")


def _decode_level(cid: str):
    """Parse the trailing '-L<base36 x _LEVEL_WIDTH>' token _encode_level
    embeds for single-leg kinds (stop, trailing_stop). Returns None when
    absent or unparseable (e.g. truncated by a length limit) — callers
    must treat that as "still unknown", not "zero"."""
    m = re.search(rf"-L([0-9a-z]{{{_LEVEL_WIDTH}}})$", cid or "")
    if not m:
        return None
    n = _b36decode(m.group(1))
    return None if n is None else n / 100.0


def _decode_stop_limit_legs(cid: str):
    """Decode the combined '-B<stop><limit>' token _cid_with_level embeds
    for stop_limit orders — both legs packed into one token (rather than
    two separate '-T'/'-L' tokens) to leave more headroom under
    MAX_CID_LEN. Each half is exactly _LEVEL_WIDTH chars, fixed-width, so
    no separator is needed between them and the split is unambiguous.
    Returns (stop, limit), each None when the token is absent/unparseable
    — "still unknown" per leg, same as _decode_level."""
    m = re.search(rf"-B([0-9a-z]{{{2 * _LEVEL_WIDTH}}})$", cid or "")
    if not m:
        return (None, None)
    raw = m.group(1)
    stop_n = _b36decode(raw[:_LEVEL_WIDTH])
    limit_n = _b36decode(raw[_LEVEL_WIDTH:])
    return (stop_n / 100.0 if stop_n is not None else None,
            limit_n / 100.0 if limit_n is not None else None)


# non-take-profit kinds use a shorter random suffix than take-profit's
# original 6 hex chars — the level token(s) need the room, and 4 hex
# chars (65,536 combinations) is still ample for a same-sweep batch of at
# most rails.max_orders_per_sync new orders.
_EXIT_RAND_LEN = 4


def _cid_with_level(cid_prefix: str, kind: str, price, stop_px, trail) -> str:
    """A fresh client_order_id for an exit, carrying a level token (see
    above) so change detection has a fallback when the broker doesn't
    report the resting reference itself. take-profit (kind "limit") is
    left byte-identical to the original format — its resting reference
    always comes straight from the broker's limit_price, it never needs
    this fallback, and the constraint is to not touch that path at all.

    stop_limit carries BOTH legs, packed into a single -B<stop><limit>
    token rather than two separate tokens — a token that only remembered
    one leg would leave the other invisible forever at a broker that
    reports neither field (Webull), and two separate tokens cost more of
    the tight clientOrderId budget than this repo has room for."""
    if kind == "limit":
        return f"{cid_prefix}-{uuid.uuid4().hex[:6]}"

    cid = f"{cid_prefix}-{uuid.uuid4().hex[:_EXIT_RAND_LEN]}"
    token = ""
    if kind == "trailing_stop":
        value = trail * 100 if trail is not None else None
        enc = _encode_level(value) if value is not None else None
        if enc is not None:
            token = f"-L{enc}"
    elif kind == "stop_limit":
        if stop_px is not None and price is not None:
            stop_enc = _encode_level(stop_px)
            limit_enc = _encode_level(price)
            if stop_enc is not None and limit_enc is not None:
                token = f"-B{stop_enc}{limit_enc}"
    else:  # "stop"
        enc = _encode_level(stop_px) if stop_px is not None else None
        if enc is not None:
            token = f"-L{enc}"

    result = f"{cid}{token}"
    if len(result) > MAX_CID_LEN:
        # Hard guard: unreachable given the fixed widths above, but if a
        # future change (a longer dep_id slice, a wider rand suffix, ...)
        # ever pushes past a broker's clientOrderId limit, fail loudly
        # here instead of silently emitting a cid Webull will truncate
        # and misparse — silent truncation is exactly what caused the
        # "-L only" trigger-blind-spot bug this format replaces.
        raise ValueError(
            f"client_order_id {result!r} ({len(result)} chars) exceeds "
            f"MAX_CID_LEN={MAX_CID_LEN}")
    return result


SUBMIT_POOL_DEFAULT = 6


_EMPTY_GATES = {"open_qty": {}, "open_rows": {}, "unfolded": {},
                "filled_today": {}, "filled_side": {}, "unresolved": set(),
                "epoch": {}}


def _exe_sums_read(s, conn_id) -> dict:
    """Today's signed fills of OUR OWN orders (sl- cids) per symbol -- the
    enforce fold rail's input."""
    from dqengine.live.persistence import Execution
    out: dict = {}
    start_et = datetime.now(ET).replace(hour=0, minute=0, second=0,
                                        microsecond=0)
    for esym, eqty in (s.query(Execution.symbol, Execution.signed_qty)
                       .filter(Execution.connection_id == conn_id,
                               Execution.filled_at >= start_et,
                               Execution.client_order_id.like("sl-%"))
                       .all()):
        esym = (esym or "").upper()
        out[esym] = out.get(esym, 0.0) + float(eqty)
    return out


def _journal_gates_read(s, conn_id, truth_mode, today_start_et,
                        model_folded_side) -> dict:
    """Journal gates for one pass, with the enforce fold-lift applied and
    scoped to what can CLOSE a row (spec: state-based, evidence-lifted).
    Fold-lift is PER SIDE (a sell and an equal buy sum to zero). Rows close
    via the executions poll, which runs for observe/enforce only -- on a
    truth `off` connection the freeze gates are inert (an unclosable row
    would freeze its symbol for the day); the unfolded freeze needs
    enforce's fold evidence. Write-ahead + unique-cid dedupe stay on for
    every mode."""
    from dqengine.live import journal as journal_mod
    empty = {k: (set() if isinstance(v, set) else dict(v))
             for k, v in _EMPTY_GATES.items()}
    try:
        g = journal_mod.gates(s, conn_id, today_start_et)
        if truth_mode == "enforce":
            for jsym, fs in list((g.get("filled_side") or {}).items()):
                if not g["unfolded"].get(jsym):
                    continue
                ms = (model_folded_side or {}).get(
                    jsym, {"buy": 0.0, "sell": 0.0})
                if abs(ms.get("buy", 0.0) - fs.get("buy", 0.0)) < 1e-6 and \
                        abs(ms.get("sell", 0.0) - fs.get("sell", 0.0)) < 1e-6:
                    journal_mod.mark_folded(s, conn_id, jsym, None)
                    g["unfolded"].pop(jsym, None)
            s.commit()
    except Exception as e:
        print(f"[journal] gates read failed {conn_id}: {e!r}", flush=True)
        return empty
    if truth_mode not in ("observe", "enforce"):
        g = {**g, "open_qty": {}, "open_rows": {}, "unfolded": {},
             "unresolved": set()}
    elif truth_mode != "enforce":
        g = {**g, "unfolded": {}}
    return g


def _journal_before(conn_id, entry) -> bool:
    """Write-ahead insert (spec: 2026-08-31-order-journal.md). True =
    proceed to the wire; False = the journal already holds this exact
    intent (same deterministic cid), stand down this one submit. A
    bookkeeping failure must never block orders -- but the duplicate
    verdict is the whole point and always stands."""
    from dqengine.live import journal
    if conn_id is None \
            or entry.get("action") != "submit" \
            or not entry.get("client_order_id"):
        return True
    from dqengine.live.persistence import SessionLocal
    try:
        with SessionLocal() as js:
            row = journal.record_sending(js, conn_id, entry)
            js.commit()
        return row is not None
    except Exception as e:
        print(f"[journal] pre-write failed {conn_id}: {e!r}", flush=True)
        return True


def _journal_cancel(conn_id, entry) -> None:
    """A cancel we sent succeeded: close the journal row (B6, 2026-09-01)."""
    from dqengine.live import journal
    if conn_id is None:
        return
    from dqengine.live.persistence import SessionLocal
    try:
        with SessionLocal() as js:
            journal.mark_canceled(js, conn_id,
                                  cid=entry.get("client_order_id") or None,
                                  broker_order_id=entry.get(
                                      "broker_order_id") or None)
            js.commit()
    except Exception as e:
        print(f"[journal] cancel-write failed {conn_id}: {e!r}", flush=True)


def _journal_after(conn_id, entry, out, rejected_note=None) -> None:
    """Ack/reject transition for the row _journal_before wrote."""
    from dqengine.live import journal
    if conn_id is None \
            or entry.get("action") != "submit" \
            or not entry.get("client_order_id"):
        return
    from dqengine.live.persistence import SessionLocal
    try:
        with SessionLocal() as js:
            if rejected_note is not None:
                journal.mark_rejected(js, conn_id,
                                      entry["client_order_id"],
                                      rejected_note)
            else:
                journal.mark_submitted(
                    js, conn_id, entry["client_order_id"],
                    (out or {}).get("id") if isinstance(out, dict) else "",
                    (out or {}).get("status") if isinstance(out, dict)
                    else "")
            js.commit()
    except Exception as e:
        print(f"[journal] post-write failed {conn_id}: {e!r}", flush=True)

# fast-pass gather cache (2026-08-27 decision-in-hand work): the four
# bookkeeping query sets (MOC dedupe, refusal fingerprints, qb-cooldown,
# fold-pending exe sums) tolerate 2s of staleness -- a stale refusal
# fingerprint costs one duplicate log line, never a wrong order -- and
# serving them from memory takes ~30ms off the pass that races the close.
# Audits always refresh; dep payloads and the connection row are ALWAYS
# read fresh (they are the decision).
GATHER_TTL_S = 2.0
_GATHER_CACHE: dict = {}


def _launch_market_batch(batch, report, record, batch_lock, gateway_state,
                         handle_rejected, buying_power,
                         journal_hooks=None) -> None:
    """The blind launcher (2026-08-27, user decision): fire the whole
    market-delta batch in parallel. When the account's buying power PROVES
    the buys clear without the sells' proceeds (the margin-account normal
    case), everything launches simultaneously; otherwise sells launch
    together first and the buys launch the moment the sells are all
    ACKNOWLEDGED -- one network round-trip of gap, and the buying-power
    reject class never exists on any account type. A gateway trip in the
    first wave stops the second (its orders were doomed to the same
    refusal)."""
    from concurrent.futures import ThreadPoolExecutor

    # stage timings (2026-09-18: a two-order close batch took 431 ms end to
    # end and nothing said where) -- per-order wire time and the wave shape
    # are printed once per batch, so the next close decomposes itself
    t_batch = time.monotonic()
    timings: list = []

    def run_one(b):
        with batch_lock:
            if gateway_state["tripped"]:
                # a sibling already met the closed gateway: serial launches
                # stop exactly here (I3: one attempt, one report line);
                # parallel launches stop everything not yet in flight
                return
        # write-ahead journal: the deferred launcher executes here, so the
        # insert-before-wire happens here too (gate_only never journals)
        if journal_hooks is not None and not journal_hooks[0](b["entry"]):
            with batch_lock:
                report["actions"].append(
                    f"{b['sym']} submit skipped — journal holds this "
                    f"intent ({b['entry'].get('client_order_id')})")
            return
        t_wire = time.monotonic()
        try:
            out = b["fn"]()
        except BrokerRejected as e:
            with batch_lock:
                timings.append((b["sym"], round((time.monotonic() - t_wire) * 1000), "rejected"))
            if journal_hooks is not None:
                journal_hooks[1](b["entry"], None, str(e)[:300])
            with batch_lock:
                handle_rejected(b["entry"], e)
            return
        with batch_lock:
            timings.append((b["sym"], round((time.monotonic() - t_wire) * 1000), "ok"))
        if journal_hooks is not None:
            journal_hooks[1](b["entry"], out, None)
        entry = b["entry"]
        if isinstance(out, dict):
            entry = {**entry, "broker_order_id": out.get("id", ""),
                     "status": out.get("status", "")}
        with batch_lock:
            record(entry)
            report["actions"].append(
                f"{'BUY' if b['side'] == 'buy' else 'SELL'} {b['qty']} "
                f"{b['sym']} (market, "
                f"{out.get('status', '?') if isinstance(out, dict) else '?'})")

    sells = [b for b in batch if b["side"] == "sell"]
    buys = [b for b in batch if b["side"] == "buy"]
    buy_notional = sum(b["qty"] * b["px"] for b in buys)
    proven = (buying_power is not None and buys
              and buy_notional <= float(buying_power))
    if proven or not sells or not buys:
        waves = [sells + buys]
    else:
        waves = [sells, buys]
    pool = max(1, int(os.environ.get("SUBMIT_POOL",
                                     str(SUBMIT_POOL_DEFAULT))))
    # Launch stagger: Webull's per-endpoint limiter is TIGHT (measured
    # 2026-08-27: a 429 after two rapid calls). Truly-simultaneous
    # launches risk refusing our own batch mid-flight; ~75ms between
    # launches keeps a 6-order batch under the limiter and still lands
    # everything inside ~0.6s. Tunable; 0 restores pure-simultaneous.
    stagger = max(0.0, float(os.environ.get("SUBMIT_STAGGER_MS",
                                            "75")) / 1000.0)
    for wave in waves:
        if not wave:
            continue
        if gateway_state["tripped"]:
            # the first wave met a closed gateway; the second would meet
            # the same refusal order by order -- stand down (the backoff
            # window in the report says when it retries)
            break
        if pool <= 1 or len(wave) == 1:
            for b in wave:
                run_one(b)
        else:
            with ThreadPoolExecutor(
                    max_workers=min(pool, len(wave))) as ex:
                futs = []
                for i, b in enumerate(wave):
                    if i and stagger:
                        time.sleep(stagger)
                    futs.append(ex.submit(run_one, b))
                for f in futs:
                    f.result()
    if batch:
        print(f"[oms] launch n={len(batch)} waves={len(waves)} "
              f"proven_bp={bool(proven)} stagger_ms={int(stagger * 1000)} "
              f"total_ms={round((time.monotonic() - t_batch) * 1000)} "
              f"wire_ms={[(s, ms, st) for s, ms, st in timings]}", flush=True)


# ------------------------------------------------- the first-sync preflight

# Every order this executor places carries a client order id starting with
# one of these (the `ours` filter inside reconcile uses the same two). An
# execution row carrying one is a fill of an order THIS executor sent; one
# without is the account's own history.
_OUR_CID_PREFIXES = ("sl-", "en-")


class UnknownBrokerPosition(RuntimeError):
    """The broker holds a universe symbol this connection has never ordered,
    and the strategy disagrees with the quantity it holds. Nothing is sent."""


PREFLIGHT_LINE = (
    "{sym}: the account holds {have:g}, this strategy holds none of it, and "
    "nothing on this connection has ever ordered {sym}. Nothing was sent. "
    "Reconcile the account first — `dqengine adopt`, or the hosted "
    "platform's adoption step.")


def preflight_unknown_positions(positions, desired, known_symbols,
                                qty_step) -> list:
    """The symbols this sweep must not trade, one loud line each.

    A strategy's replay starts on its start date holding nothing. Point it
    at an account that already holds one of its universe symbols and the
    first sweep reads `want 0, have 74` and market-sells 74 shares nobody
    asked it to touch. That is the failure this answers, and it answers it
    before anything transmits: a non-empty return refuses the whole sweep.

    A symbol is refused only when NOTHING accounts for the shares. Two
    things account for them, and either is enough:

    * this executor has ordered the symbol on this connection --
      `known_symbols`, which is what it ORDERED, not what the account has
      ever traded (see `_known_symbols_read`). After the first order it
      ever places in a symbol the check is permanently silent for it;
    * the strategy's replay holds a position in the symbol. Backdating
      `start_date` until the replay reproduces the shares the account holds
      is how a live sleeve adopted a real broker position, and it passes
      here with no ceremony, which is the point.

    So a quantity the two disagree on is NOT refused while one of them
    vouches for the symbol: `want 5, have 8` is a manual trade in a symbol
    the strategy trades, and the executor already has machinery for that
    kind of disagreement -- the fold rail, the journal's visibility rule,
    the auditor's drift freeze -- which a hard halt here would replace with
    a stuck connection. What has no machinery, and no legitimate reading,
    is a position the strategy has never held and never ordered.

    Two more things are deliberately not flagged: a symbol the account is
    flat in (most of a wide universe, and nothing to disagree about), and a
    quantity below the venue's own `qty_step` -- a broker's zero row, or
    fractional dust no order could express, is not a position, decided by
    the same rounding every order in this module goes through.
    """
    known = {str(s).upper() for s in (known_symbols or ())}
    out = []
    for sym in sorted(desired):
        have = float(positions.get(sym) or 0.0)
        if _round_step(have, qty_step) == 0.0:
            continue
        if sym in known:
            continue
        if _round_step(float(desired.get(sym) or 0.0), qty_step) != 0.0:
            continue
        out.append(PREFLIGHT_LINE.format(sym=sym, have=have))
    return out


def _known_symbols_read(session, conn_id: str) -> set:
    """Every symbol this executor has ordered on this connection.

    Three tables, one pass each: `broker_orders` (append-only, every submit,
    replace, cancel and refusal it ever performed), `order_journal` (the
    write-ahead row, which exists even for an order whose wire call was
    never answered) and `executions` — the last one restricted to fills of
    OUR OWN client order ids.

    That restriction is the whole correctness of the preflight, not a
    refinement. `executions.poll` pulls the ACCOUNT'S history, the user's
    own trades included, and on a connection with no stored fills it asks
    the broker for everything it will give; it runs earlier in the same
    sweep. Counting any execution row as history would therefore mark the
    adopted-by-accident symbol known on the sweep after the first one, and
    the check would protect an account for ten seconds and then sell its
    shares. A row we placed is one this executor can prove it owns.
    """
    from sqlalchemy import or_

    from dqengine.live.persistence import (BrokerOrder, Execution,
                                           OrderJournal)
    out: set = set()
    for model in (BrokerOrder, OrderJournal):
        out |= {s for (s,) in session.query(model.symbol)
                .filter(model.connection_id == conn_id).distinct().all() if s}
    ours = or_(*[Execution.client_order_id.like(f"{p}%")
                 for p in _OUR_CID_PREFIXES])
    out |= {s for (s,) in session.query(Execution.symbol)
            .filter(Execution.connection_id == conn_id, ours)
            .distinct().all() if s}
    return {str(s).upper() for s in out}


def reconcile(adapter, creds, desired, exit_wants, last_px, rails, report,
              record, moc_wants=(), entry_wants=(), moc_done=(),
              recent_refusals=(), qb_cooldown=(), fold_pending=(),
              submit_backoff: Optional[dict] = None,
              buying_power: Optional[float] = None,
              now: Optional[datetime] = None,
              conn_id: Optional[str] = None,
              journal_gates: Optional[dict] = None,
              known_symbols: Optional[set] = None) -> None:
    if rails.paused:
        report["actions"].append("connection paused — no orders")
        return
    if not rails.live_allowed:
        report["actions"].append(
            "live-money gate: deployment not confirmed — no orders")
        return

    now = now or datetime.now(ET)

    # I7 (market-gateway-not-ready backoff): `submit_backoff` is the
    # caller's still-active window from a PRIOR sweep's market-not-ready
    # refusal (persisted in the KV table, since this executor is otherwise
    # DB-free -- see sync_broker_account). If it hasn't expired, order
    # SUBMISSIONS (not cancels -- those still run normally, and not this
    # sweep's balance/execution polling, which callers do outside
    # reconcile() entirely) are suppressed here WITHOUT even attempting the
    # broker call, and the report says so exactly once. If it has expired,
    # this sweep proceeds exactly as if no backoff existed at all (I5:
    # suppression is only ever entered after a fresh refusal below, never
    # assumed to still apply).
    gateway_state = {"tripped": False}
    if submit_backoff and submit_backoff.get("until"):
        active_until = None
        try:
            active_until = submit_backoff["until"]
            if isinstance(active_until, str):
                active_until = datetime.fromisoformat(active_until)
            still_active = now < active_until
        except (TypeError, ValueError):
            # a malformed/naive `until` (should never come from our own
            # writes -- see the isoformat() calls below -- but this is a
            # permanent, unrecoverable "stuck suppressed forever" failure
            # mode on a LIVE account if it ever did) must never raise here
            # and skip the trailing setdefault: treat it as no backoff at
            # all rather than let a bad KV row wedge submissions off
            # indefinitely.
            still_active = False
        if still_active:
            gateway_state["tripped"] = True
            reason = submit_backoff.get("reason", "market gateway not ready")
            until_disp = active_until.astimezone(ET).strftime(
                "%Y-%m-%d %H:%M %Z")
            report["errors"].append(
                f"order submissions paused until {until_disp} — {reason}")
            report["submit_backoff"] = {"until": active_until.isoformat(),
                                        "reason": reason}

    budget = {"n": rails.max_orders_per_sync}

    def spend():
        if budget["n"] <= 0:
            return False
        budget["n"] -= 1
        return True

    batch_lock = _threading.Lock()   # report/gateway/record under threads

    def handle_rejected(entry, e):
        # match on the FULL message, not the [:300]-truncated one used
        # for display/storage below -- Webull leads with the code
        # today so truncation happens not to matter yet, but nothing
        # guarantees that stays true.
        full_msg = str(e)
        msg = full_msg[:300]
        if (entry["action"] in ("submit", "replace")
                and not gateway_state["tripped"]
                and _is_market_not_ready(full_msg)):
            # I7: the broker's gateway, not our request, isn't ready.
            # Trip the backoff so every OTHER submission this same
            # sweep is skipped (I3: record once, not once per standing
            # order) and report a single asymmetric window (I4): brief
            # if our calendar says the session is open right now (a
            # genuine outage), until the next gateway retry window if
            # it says closed (nothing would have succeeded anyway).
            gateway_state["tripped"] = True
            until = (now + timedelta(seconds=OPEN_SESSION_BACKOFF_S)
                     if _market_open_now(now)
                     else _next_gateway_retry_time(now))
            until_disp = until.astimezone(ET).strftime(
                "%Y-%m-%d %H:%M %Z")
            report["errors"].append(
                f"order submissions paused until {until_disp} — {msg}")
            report["submit_backoff"] = {"until": until.isoformat(),
                                        "reason": msg}
        else:
            report["errors"].append(msg)
        record({**entry, "action": "refused", "status": "rejected"})

    def act(fn, entry, gate_only=False):
        """Run one adapter mutation behind the rails + dry-run + audit.
        gate_only=True runs the gates (trip, budget, dry-run) and returns
        "go" without executing -- the blind launcher defers execution to
        its parallel flush while the BUDGET is still consumed here, in
        deterministic loop order."""
        if entry["action"] in ("submit", "replace") and gateway_state["tripped"]:
            return None
        if not spend():
            if not any("circuit breaker tripped" in a
                       for a in report["actions"]):
                report["actions"].append(
                    "circuit breaker tripped — max orders per sync reached")
            return None
        if rails.dry_run:
            report["actions"].append(f"DRY-RUN {entry['action']} "
                                     f"{entry['symbol']} x{entry['qty']}")
            record({**entry, "status": "dry_run"})
            return None
        if gate_only:
            return "go"
        # write-ahead journal: insert-before-wire; a duplicate cid means a
        # concurrent pass already sent this exact intent -- stand down
        if not _journal_before(conn_id, entry):
            report["actions"].append(
                f"{entry['symbol']} submit skipped — journal holds this "
                f"intent ({entry.get('client_order_id')})")
            return None
        try:
            out = fn()
        except BrokerRejected as e:
            _journal_after(conn_id, entry, None,
                           rejected_note=str(e)[:300])
            handle_rejected(entry, e)
            return None
        _journal_after(conn_id, entry, out)
        if entry.get("action") == "cancel":
            _journal_cancel(conn_id, entry)
        if isinstance(out, dict):
            entry = {**entry, "broker_order_id": out.get("id", ""),
                     "status": out.get("status", "")}
        record(entry)
        return out

    def _match_no_cid(pool, sym, side, kind, qty, price, stop_px, trail):
        """No-client-order-id identity match for the PLACEMENT lookup:
        symbol + side + kind + qty + reference level — the same predicate
        the release pass's `_claimed`/`_entry_claimed` already use (CRITICAL
        3). Matching on (symbol, side, kind) alone let two same-kind resting
        orders on one symbol (e.g. two stops at different qty/level) be
        mistaken for each other: sweep 1 would cancel one and duplicate the
        other, alternating forever; for entries it could duplicate a resting
        BUY stop so both trigger. Consumes (pops) the matched order from
        `pool` so a second want can never also claim it — without that, two
        different wants scanning the same static list could both match the
        one resting order they share a (symbol, side, kind) with."""
        qty_r = _round_step(qty, adapter.caps.qty_step)
        for i, o in enumerate(pool):
            if o["symbol"] != sym or o.get("side") != side \
                    or o.get("type") != kind:
                continue
            if abs(o["qty"] - qty_r) >= 1e-9:
                continue
            if kind == "limit":
                if abs((o.get("limit_price") or 0) - (price or 0)) > 0.004:
                    continue
            elif kind in ("stop", "stop_limit"):
                if abs((o.get("stop_price") or 0) - (stop_px or 0)) > 0.004:
                    continue
            elif kind == "trailing_stop":
                resting_trail = o.get("trail_percent")
                resting_frac = (resting_trail / 100.0
                                if resting_trail is not None else None)
                if abs((resting_frac or 0) - (trail or 0)) > 0.004:
                    continue
            else:
                continue
            return pool.pop(i)
        return None

    def _fold_explains(sym, delta) -> bool:
        """Does this symbol's UNFOLDED fill quantity actually explain the
        trade we are about to make?

        THE LATCH: the rail used to freeze a symbol for the
        whole ET day on the bare fact that today's fills of our own orders
        differ from what the sleeves folded. That premise -- "the sleeve
        will fold it on the next tick" -- is FALSE for a fill of shares no
        sleeve ever modelled: the OMS's own corrective market delta (the
        one that liquidates a duplicate buy) is by construction a trade
        that makes the BROKER match the model, so the model has nothing to
        fold and the mismatch never clears. That latched a symbol for
        every sleeve on the connection: its take-profit stayed parked at
        the level of the previous session while the strategy wanted a new
        one, and a real exit signal would have been frozen out too.

        The rail's actual purpose is narrower than the latch: don't correct
        a delta the unfolded fills already explain (the buy-back shape --
        broker filled our sell, sleeve still thought it held the shares,
        so `want - have` read as "buy them back"). Folding those fills
        would move the model by `diff`, i.e. the delta would become
        `delta + diff`. Freeze only when that is genuinely closer to flat
        than acting now would be; when it is not, the divergence is not a
        pending fold and freezing only strands the position.

        `fold_pending` may be a plain set (magnitude unknown -- legacy
        callers and tests): unknown stays conservative and freezes."""
        if sym not in fold_pending:
            return False
        diff = (fold_pending.get(sym)
                if isinstance(fold_pending, dict) else None)
        if diff is None:
            return True
        return abs(delta + diff) < abs(delta)

    def _already_refused(dep_id, sym, kind, status, level):
        """I6: was this exact standing want already refused (same reason,
        same level) earlier today? `recent_refusals` is the caller's set of
        (deployment_id, symbol, order_type, status, level) fingerprints
        built from today's already-recorded `refused` BrokerOrder rows.
        Without this, an unchanged refused want re-records a fresh row
        every sweep forever (~78/day for one entry) — this only suppresses
        the DB write; the report action still surfaces it each sweep."""
        return (dep_id, sym, kind, status,
                round(level, 2) if level is not None else None) \
            in recent_refusals

    positions = adapter.positions(creds)
    open_orders = adapter.open_orders(creds)

    # THE FIRST-SYNC PREFLIGHT, here because this is the first line at which
    # both halves of the comparison exist and still the last line before
    # anything can transmit: section 0 below is the first `act`. A refusal
    # leaves this pass with no submissions, no cancels, no journal rows and
    # no backoff verdict (the trailing `setdefault` is never reached), and
    # `sync_broker_account` records it as a sweep error with its tail
    # intact. `known_symbols=None` means the caller gathered no history --
    # every direct, pure call of `reconcile` -- and the check sits out;
    # `sync_broker_account` always passes a set, so both the auditor and
    # the fast lane are covered by the one entry point.
    if known_symbols is not None:
        unknown = preflight_unknown_positions(
            positions, desired, known_symbols, adapter.caps.qty_step)
        if unknown:
            for line in unknown:
                print(f"[exec] first-sync check refused this sweep — {line}",
                      flush=True)
            raise UnknownBrokerPosition(
                unknown[0] if len(unknown) == 1 else
                f"{unknown[0]} (and {len(unknown) - 1} more symbol(s))")

    ours = [o for o in open_orders
            if (o.get("client_order_id") or "").startswith(("sl-", "en-"))]
    if not adapter.caps.supports_client_order_id:
        # no client ids at this broker: treat ALL its open orders as ours
        # (the account is executor-managed by contract)
        ours = list(open_orders)

    # a bracket (e.g. stop + take-profit on the same shares) must never rest
    # both legs natively: every roster broker rejects a second GTC sell
    # against shares already committed to a sibling sell, and on any venue
    # that did accept both, a fast move through both levels inside one
    # sweep window can fill both and leave the account short. Policy: rest
    # only the protective leg (stop/stop_limit/trailing_stop) natively;
    # simulate the take-profit sibling exactly like the existing
    # no-broker-support fallback — the replay already detects the
    # take-profit and drives a market exit through `desired` when the
    # target is hit. Computed up front — section 3 (release-cancels) below
    # needs it too, to know when a resting TP's sibling has newly become
    # native and the TP itself must be swept away (C4).
    _sibling_counts = {}
    for t in exit_wants:
        key = (t[0], t[1])
        _sibling_counts[key] = _sibling_counts.get(key, 0) + 1

    def _tp_simulated(dep_id, sym, kind):
        return kind == "limit" and _sibling_counts.get((dep_id, sym), 0) > 1

    # 0) at_close_order intents: native market_on_close/limit_on_close where
    # the broker's Caps.order_types allow the kind; otherwise leave the qty
    # in `desired` (it's already baked into the replay's holdings) so the
    # ordinary market-delta loop below — the IR spec's §7 "near-close market
    # emulation" — picks it up, journaled here so the fallback is visible.
    moc_native_qty: dict = {}
    for t in moc_wants:
        dep_id, sym, qty, kind, price = t[:5]
        rule_id = t[5] if len(t) > 5 else None
        if qty == 0:
            continue
        note = capabilities.journal_note(adapter.caps, kind, sym)
        if note:
            # Not native here: leave the qty in `desired` (it is already
            # baked into the replay's holdings) so the market-delta loop
            # below performs the substitution. The wording comes from the
            # ladder so the same fallback reads the same everywhere.
            report["actions"].append(note)
            continue
        cid_prefix = f"sl-moc-{sym}-{dep_id[:8]}"
        if adapter.caps.supports_client_order_id:
            pending = [o for o in ours if o["symbol"] == sym and
                       (o.get("client_order_id") or "").startswith(cid_prefix)]
        else:
            pending = [o for o in ours if o["symbol"] == sym and
                       o.get("type") == kind]
        if pending:
            # a day-TIF MOC/LOC ticket already resting from an earlier sync
            # this session — nothing to resubmit; net it out of the
            # market-delta pass below since `positions` doesn't reflect it
            # yet (it hasn't filled).
            moc_native_qty[sym] = moc_native_qty.get(sym, 0.0) + qty
            continue
        # C2: `pending` only ever sees a still-OPEN ticket — once our native
        # MOC/LOC fills, it drops out of open_orders() and the check above
        # alone would resubmit a fresh one for the rest of the day. `moc_done`
        # is a (deployment_id, symbol) set the caller builds from today's
        # already-submitted broker_orders rows — a TERMINAL record, not just
        # an open one — so a submit already recorded today is never
        # resubmitted regardless of its current status. Deliberately NOT
        # added to moc_native_qty here: if it filled, `positions` already
        # reflects the executed qty, and crediting moc_native_qty again would
        # double-subtract it from the market-delta pass below, producing a
        # spurious compensating market order in the opposite direction —
        # exactly the bug this fix closes. If it was instead rejected/
        # canceled (not filled), the ordinary market-delta pass will still
        # pick up the remaining qty as a plain market order.
        if (dep_id, sym) in moc_done:
            continue
        qabs = _round_step(abs(qty), adapter.caps.qty_step)
        if qabs <= 0:
            continue
        side = "buy" if qty > 0 else "sell"
        cid = f"{cid_prefix}-{uuid.uuid4().hex[:8]}"

        def _submit(sym=sym, qabs=qabs, side=side, kind=kind, price=price,
                    cid=cid):
            return adapter.submit(creds, sym, qabs, side, order_type=kind,
                                  tif="day", limit_price=price,
                                  client_order_id=cid)

        o = act(_submit,
                {"action": "submit", "symbol": sym, "qty": qabs, "side": side,
                 "order_type": kind, "limit_price": price,
                 "broker_order_id": "", "client_order_id": cid, "status": "",
                 "deployment_id": dep_id, "rule_tag": rule_id})
        if o is not None:
            report["actions"].append(
                f"{'BUY' if side == 'buy' else 'SELL'} {qabs} {sym} "
                f"({kind}, {o.get('status', '?')})")
            moc_native_qty[sym] = moc_native_qty.get(sym, 0.0) + qty

    # 1) release stale resting orders whose deployment no longer wants them
    # — BEFORE any market delta is computed (C5). Previously these
    # cancels ran last: when a replay-driven exit releases a resting
    # protective stop (e.g. a simulated take-profit just triggered), the
    # market-delta pass below would try to sell the now-freed shares while
    # they were still committed to that still-resting stop, get rejected by
    # the broker, and only cancel the stop a sweep later — a broker
    # rejection plus a full sweep's worth of extra slippage on every such
    # exit. Running the release-cancels first means the shares are already
    # free by the time the market-delta pass looks at them.
    #
    # 1a) resting BUY entries
    if adapter.caps.supports_client_order_id:
        wanted_entries = []
        for t in entry_wants:
            dep_id, sym, kind = t[0], t[1], t[3]
            rule_id = t[6] if len(t) > 6 else None
            wanted_entries.append(
                (sym, _entry_cid_prefix(dep_id, kind, sym, rule_id)))
        for o in ours:
            cid = o.get("client_order_id") or ""
            if cid.startswith(_ENTRY_CID_PREFIXES) and not any(
                    o["symbol"] == sym and cid.startswith(prefix)
                    for sym, prefix in wanted_entries):
                def _cancel(o=o):
                    return adapter.cancel(creds, o["id"])

                act(_cancel,
                    {"action": "cancel", "symbol": o["symbol"],
                     "qty": o["qty"], "side": o["side"],
                     "order_type": o["type"],
                     "limit_price": o.get("limit_price"),
                     "broker_order_id": o["id"],
                     "client_order_id": cid, "status": "",
                     "deployment_id": None})
                report["actions"].append(
                    f"entry {o['type']} {o['symbol']} canceled (released)")
    else:
        def _entry_claimed(o):
            for t in entry_wants:
                _dep_id, sym, qty, kind, price, stop_px = t[:6]
                if o["symbol"] != sym or o.get("type") != kind:
                    continue
                qty_r = _round_step(qty, adapter.caps.qty_step)
                if abs(o["qty"] - qty_r) >= 1e-9:
                    continue
                if kind == "limit":
                    if abs((o.get("limit_price") or 0) - (price or 0)) <= 0.004:
                        return True
                elif kind in ("stop", "stop_limit"):
                    if abs((o.get("stop_price") or 0) - (stop_px or 0)) <= 0.004:
                        return True
            return False

        for o in ours:
            if o.get("type") in _ENTRY_CID_CODE and o.get("side") == "buy" \
                    and not _entry_claimed(o):
                def _cancel(o=o):
                    return adapter.cancel(creds, o["id"])

                act(_cancel,
                    {"action": "cancel", "symbol": o["symbol"],
                     "qty": o["qty"], "side": o["side"],
                     "order_type": o["type"],
                     "limit_price": o.get("limit_price"),
                     "broker_order_id": o["id"],
                     "client_order_id": "", "status": "",
                     "deployment_id": None})
                report["actions"].append(
                    f"entry {o['type']} {o['symbol']} canceled (released)")

    # 1b) resting SELL exits. A limit (take-profit) want whose sibling
    # protective leg made it native is simulated, not placed (see
    # _tp_simulated above) — it must be excluded here too (C4), or a TP
    # that rested in an earlier sweep (back when it was the lone rule)
    # never gets cancelled once its protective sibling shows up (e.g.
    # after a warm-up period), leaving the later stop rejected forever
    # against shares the stale TP still holds.
    if adapter.caps.supports_client_order_id:
        wanted_exits = []
        for t in exit_wants:
            dep_id, sym, kind = t[0], t[1], t[3]
            if _tp_simulated(dep_id, sym, kind):
                continue
            rule_id = t[7] if len(t) > 7 else None
            wanted_exits.append(
                (sym, _exit_cid_prefix(dep_id, kind, sym, rule_id)))
        for o in ours:
            cid = o.get("client_order_id") or ""
            if cid.startswith(_EXIT_CID_PREFIXES) and not any(
                    o["symbol"] == sym and cid.startswith(prefix)
                    for sym, prefix in wanted_exits):
                def _cancel(o=o):
                    return adapter.cancel(creds, o["id"])

                act(_cancel,
                    {"action": "cancel", "symbol": o["symbol"], "qty": o["qty"],
                     "side": o["side"], "order_type": o["type"],
                     "limit_price": o.get("limit_price"),
                     "broker_order_id": o["id"],
                     "client_order_id": cid, "status": "",
                     "deployment_id": None})
                label = "TP" if o["type"] == "limit" else o["type"]
                report["actions"].append(
                    f"{label} {o['symbol']} canceled (released)")
    else:
        # no client ids at this broker: an exit is "claimed" when an
        # exit_wants entry matches it by symbol, kind, qty and reference
        # price/stop/trail level (the same matching the exit-existing path
        # uses) — unclaimed sell exits are stale ones whose sleeve
        # released them. A simulated-TP sibling is excluded from claiming
        # anything too, for the same C4 reason as above.
        #
        # This path has no client_order_id to fall back on (that's the
        # branch condition), so it relies entirely on the invariant that
        # every no-client-order-id adapter populates stop_price/
        # trail_percent on open_orders() — today that's Schwab, and it
        # does. A future no-client-order-id adapter that DOESN'T populate
        # them would silently reopen the coerced-to-0 churn/mismatch bug
        # this whole change exists to close (a stop/trailing entry would
        # never match its resting order and get endlessly canceled as
        # "released"). Any such adapter must either populate these fields
        # or this matching needs its own None-safe fallback.
        def _claimed(o):
            for t in exit_wants:
                _dep_id, sym, qty, kind, price, stop_px, trail = t[:7]
                if _tp_simulated(_dep_id, sym, kind):
                    continue
                if o["symbol"] != sym or o.get("type") != kind:
                    continue
                qty_r = _round_step(qty, adapter.caps.qty_step)
                if abs(o["qty"] - qty_r) >= 1e-9:
                    continue
                if kind == "limit":
                    if abs((o.get("limit_price") or 0)
                           - (price or 0)) <= 0.004:
                        return True
                elif kind in ("stop", "stop_limit"):
                    if abs((o.get("stop_price") or 0)
                           - (stop_px or 0)) <= 0.004:
                        return True
                elif kind == "trailing_stop":
                    resting_trail = o.get("trail_percent")
                    resting_frac = (resting_trail / 100.0
                                    if resting_trail is not None else None)
                    if abs((resting_frac or 0) - (trail or 0)) <= 0.004:
                        return True
            return False

        for o in ours:
            # Without a cid, side is the only thing separating an EXIT from
            # a resting buy-stop ENTRY of the same kind. A sell is always an
            # exit; a buy is one only when the position it would close is
            # actually short -- otherwise it is an entry and not ours to
            # release here.
            o_side = o.get("side")
            is_exit = o_side == "sell" or (
                o_side == "buy" and positions.get(o.get("symbol"), 0.0) < 0)
            if o.get("type") in _EXIT_CID_CODE and is_exit and not _claimed(o):
                def _cancel(o=o):
                    return adapter.cancel(creds, o["id"])

                act(_cancel,
                    {"action": "cancel", "symbol": o["symbol"], "qty": o["qty"],
                     "side": o["side"], "order_type": o["type"],
                     "limit_price": o.get("limit_price"),
                     "broker_order_id": o["id"],
                     "client_order_id": "", "status": "",
                     "deployment_id": None})
                label = "TP" if o["type"] == "limit" else o["type"]
                report["actions"].append(
                    f"{label} {o['symbol']} canceled (released)")

    # 2) market deltas
    deferred_deltas: list = []
    for sym, want in desired.items():
        if sym in qb_cooldown:
            # C-QB: a quote-driven fast-path exit (execute_quote_breach_exit)
            # just sold this symbol directly at the broker, ahead of the
            # replay's own bar-driven accounting catching up (that needs a
            # closed minute bar showing the breach, up to ~60s away). `have`
            # below is live from the broker and already reflects the sale,
            # but `want` is still the STALE pre-exit replay desired -- acting
            # on that delta now would BUY BACK the shares we just sold for
            # safety. Skip this symbol's market-delta pass for a short
            # cooldown window (sync_broker_account computes membership from
            # recent "sl-qb-" BrokerOrder rows) and let the next bar close
            # bring the replay's own desired back in line, same as it always
            # does after a normal bar-driven exit.
            report["actions"].append(
                f"{sym} market-delta skipped (quote-breach exit cooldown)")
            continue
        # journal gates (spec: 2026-08-31-order-journal.md §"state-based
        # gates"): the write-ahead ledger outranks every cached view of
        # the account. No time windows -- each gate lifts on evidence.
        jg = journal_gates or {}
        if sym in (jg.get("unresolved") or ()):
            # an order we SENT but never got an ack for may or may not be
            # live at the venue: trading this symbol before recovery
            # resolves it risks doubling (or fighting) that order
            report["actions"].append(
                f"{sym} market-delta frozen (unacked order at the venue "
                f"— recovery pending)")
            continue
        if (jg.get("unfolded") or {}).get(sym):
            # the ledger holds fills the sleeve has not folded yet: `have`
            # is (or is about to be) ahead of `want` for a known reason.
            # The old enforce-only exe_sums rail, generalized to every
            # truth mode and fed from the journal.
            report["actions"].append(
                f"{sym} market-delta frozen (filled order not yet folded "
                f"into the sleeve)")
            continue
        # VISIBILITY RULE (2026-09-01 reversal incident): an open journal
        # row the venue/book view does NOT list is not "pending" -- it is
        # AMBIGUOUS (filled-but-unfolded, or lost). Netting it produced a
        # wrong order in both directions (pending counted + position
        # already updated -> reversal; pending cleared + position stale ->
        # duplicate). The only safe action for an ambiguous symbol is to
        # sit out until evidence resolves the row. A row that IS visible
        # is already counted by its own section (market pending / exits).
        # Staleness can only over-freeze for seconds, never mis-order.
        jrows = (jg.get("open_rows") or {}).get(sym, ())
        if jrows and adapter.caps.supports_client_order_id:
            # (cid-less venues cannot be matched; the rule is inert there)
            visible = {(o.get("client_order_id") or "") for o in ours
                       if o["symbol"] == sym}
            hidden = [c for c, _ in jrows if c not in visible]
            if hidden:
                report["actions"].append(
                    f"{sym} market-delta frozen (order {hidden[0]} not "
                    f"visible at the venue yet — awaiting fill evidence)")
                continue
        px = last_px.get(sym, 0.0)
        have = positions.get(sym, 0.0)
        if adapter.caps.supports_client_order_id:
            pending = [o for o in ours if o["symbol"] == sym and
                       (o.get("client_order_id") or "").startswith("sl-mkt-")]
        else:
            # no client ids at this broker: any open market order in this
            # symbol is ours (the account is executor-managed by contract)
            pending = [o for o in ours if o["symbol"] == sym and
                       o.get("type") == "market"]
        pending_qty = sum((1 if o["side"] == "buy" else -1) * o["qty"]
                          for o in pending)
        delta = _round_step(
            want - have - pending_qty - moc_native_qty.get(sym, 0.0),
            adapter.caps.qty_step)
        if delta == 0:
            continue
        if _fold_explains(sym, delta):
            # THE RAIL (lean-fills spec §3.4): the broker holds fills of
            # OUR OWN orders that the sleeve has not folded yet, and
            # folding them would shrink this very delta -- so `have` is
            # ahead of `want` for a reason we already know about.
            # "Correcting" that delta is how the buy-back churn
            # happened. Never fight the broker: sit this symbol out until
            # the replay folds the fills (normally the next tick). Checked
            # HERE, against the computed delta, rather than up front on
            # symbol membership alone -- see _fold_explains for the latch
            # that cost.
            report["actions"].append(
                f"{sym} market-delta frozen (broker fills of our own "
                f"orders not yet folded into the sleeve)")
            continue
        if px <= 0:
            report["actions"].append(
                f"{'BUY' if delta > 0 else 'SELL'} {abs(delta)} {sym} refused "
                f"(unknown price)")
            record({"action": "refused", "symbol": sym, "qty": abs(delta),
                    "side": "buy" if delta > 0 else "sell",
                    "order_type": "market", "limit_price": None,
                    "broker_order_id": "", "client_order_id": "",
                    "status": "no_price", "deployment_id": None})
            continue
        # A position cap must never block a trade that REDUCES exposure —
        # that would strand risk the strategy is trying to shed. Only refuse
        # when the target both breaches the cap and grows the position.
        if rails.max_position_notional is not None and px > 0 \
                and abs(want) * px > rails.max_position_notional \
                and abs(want) > abs(have):
            report["actions"].append(
                f"{sym} target {want} refused (position would be "
                f"{abs(want) * px:,.0f} > max_position_notional "
                f"{rails.max_position_notional:,.0f}) — size left as the "
                f"strategy set it; raise or clear the cap to trade it")
            record({"action": "refused", "symbol": sym, "qty": abs(delta),
                    "side": "buy" if delta > 0 else "sell",
                    "order_type": "market", "limit_price": None,
                    "broker_order_id": "", "client_order_id": "",
                    "status": "over_position_notional", "deployment_id": None})
            continue
        if rails.max_order_notional is not None \
                and abs(delta) * px > rails.max_order_notional:
            report["actions"].append(
                f"{'BUY' if delta > 0 else 'SELL'} {abs(delta)} {sym} refused "
                f"(order notional > {rails.max_order_notional:,.0f})")
            record({"action": "refused", "symbol": sym, "qty": abs(delta),
                    "side": "buy" if delta > 0 else "sell",
                    "order_type": "market", "limit_price": None,
                    "broker_order_id": "", "client_order_id": "",
                    "status": "over_notional", "deployment_id": None})
            continue
        side = "buy" if delta > 0 else "sell"
        # deterministic cid (spec: 2026-08-31-order-journal.md): two passes
        # computing the same unchanged intent produce the SAME cid, and the
        # journal's unique gate collapses them -- no uuid, no duplicates
        from dqengine.live import journal as journal_mod
        if conn_id is not None:
            epoch = ((journal_gates or {}).get("epoch") or {}).get(sym, 0)
            cid = journal_mod.market_cid(
                conn_id, now.date().isoformat(), sym, side, abs(delta),
                epoch)
        else:
            cid = f"sl-mkt-{sym}-{uuid.uuid4().hex[:10]}"

        def _submit(sym=sym, delta=delta, side=side, cid=cid):
            return adapter.submit(creds, sym, abs(delta), side,
                                   client_order_id=cid)

        entry = {"action": "submit", "symbol": sym, "qty": abs(delta),
                 "side": side, "order_type": "market", "limit_price": None,
                 "broker_order_id": "", "client_order_id": cid,
                 "status": "", "deployment_id": None}
        # BLIND LAUNCHER (2026-08-27): gates and the order budget run HERE,
        # in deterministic loop order; execution is deferred so the whole
        # batch launches in parallel below instead of one round-trip at a
        # time (the 15:59:00->15:59:02 staircase this replaces).
        if act(None, entry, gate_only=True) == "go":
            deferred_deltas.append({"fn": _submit, "entry": entry,
                                    "sym": sym, "side": side,
                                    "qty": abs(delta), "px": px})

    if deferred_deltas:
        _launch_market_batch(
            deferred_deltas, report, record, batch_lock, gateway_state,
            handle_rejected, buying_power,
            journal_hooks=(
                lambda e: _journal_before(conn_id, e),
                lambda e, out, note: _journal_after(conn_id, e, out,
                                                    rejected_note=note)))

    # 3) per-deployment exits: take-profit limits, stops, stop-limits,
    #    trailing stops — native where the broker supports the kind,
    #    simulated (skipped here, left to the replay to exit at market on
    #    breach) otherwise.
    # a bracket (e.g. stop + take-profit on the same shares) must never
    # rest both legs natively: every roster broker rejects a second GTC
    # sell against shares already committed to a sibling sell, and on any
    # venue that did accept both, a fast move through both levels inside
    # one sweep window can fill both and leave the account short. Policy:
    # rest only the protective leg (stop/stop_limit/trailing_stop)
    # natively; simulate the take-profit sibling exactly like the
    # existing no-broker-support fallback — the replay already detects
    # the take-profit and drives a market exit through `desired` when the
    # target is hit. (_sibling_counts / _tp_simulated computed up top.)
    for t in exit_wants:
        dep_id, sym, qty, kind, price, stop_px, trail = t[:7]
        rule_id = t[7] if len(t) > 7 else None
        # An exit closes what is held: sell a long, buy back a short. Older
        # tuples (and every IR payload) carry no side and mean "sell".
        side = t[8] if len(t) > 8 else "sell"
        label = "TP" if kind == "limit" else kind
        if _tp_simulated(dep_id, sym, kind):
            report["actions"].append(
                f"take-profit for {sym} simulated (its shares are "
                "committed to the protective stop) — the replay will "
                "exit at market when the target is hit")
            continue
        note = capabilities.journal_note(adapter.caps, kind, sym)
        if note:
            report["actions"].append(note)
            continue
        qty = _round_step(qty, adapter.caps.qty_step)
        px = last_px.get(sym, 0.0)
        level = price if kind in ("limit", "stop_limit") else stop_px
        # exits are sells: refuse one priced INTO the market (instant fill
        # below the bid), allow one resting above it however far — see
        # limit_price_refusal. Stops/trailing stops have their own checks
        # below; they are legitimately far from the last price by design.
        if kind == "limit":
            why = limit_price_refusal(side, level, px, rails)
            if why:
                report["actions"].append(
                    f"{label} {sym} @ {level:.2f} skipped — {why}")
                continue
        # stops/stop-limits get their own, much wider sanity checks: a sell
        # stop at or above the last price would trigger instantly as a
        # market sell (almost certainly a mistake), and an absurdly distant
        # stop is refused too rather than silently sent to the broker.
        if kind in ("stop", "stop_limit") and px > 0 and stop_px is not None:
            # A protective stop sits on the losing side of the position: a
            # long's sell-stop BELOW the market, a short's buy-stop ABOVE it.
            # On the wrong side it triggers instantly as a market order —
            # which is the same defect in both directions, mirrored.
            wrong_side = stop_px >= px if side == "sell" else stop_px <= px
            if wrong_side:
                rel = ">=" if side == "sell" else "<="
                report["actions"].append(
                    f"{kind} {sym} @ {stop_px:.2f} refused (stop price "
                    f"{rel} last price {px:.2f})")
                record({"action": "refused", "symbol": sym, "qty": qty,
                        "side": side, "order_type": kind,
                        "limit_price": level, "broker_order_id": "",
                        "client_order_id": "", "status": "invalid_stop",
                        "deployment_id": dep_id, "rule_tag": rule_id})
                continue
            if abs(stop_px / px - 1) * 100 > rails.stop_band_pct:
                report["actions"].append(
                    f"{kind} {sym} @ {stop_px:.2f} refused "
                    f"(>{rails.stop_band_pct}% from last {px:.2f})")
                record({"action": "refused", "symbol": sym, "qty": qty,
                        "side": side, "order_type": kind,
                        "limit_price": level, "broker_order_id": "",
                        "client_order_id": "", "status": "over_stop_band",
                        "deployment_id": dep_id, "rule_tag": rule_id})
                continue
        # `have` is signed at the broker: a short is negative. An exit
        # protects the magnitude, and only if the position is actually on
        # the side this exit closes -- a sell-exit against a short position
        # would DOUBLE it, not close it.
        signed_have = positions.get(sym, 0.0)
        have = abs(signed_have) if (
            (side == "sell" and signed_have > 0)
            or (side == "buy" and signed_have < 0)) else 0.0
        cid_prefix = _exit_cid_prefix(dep_id, kind, sym, rule_id)
        if adapter.caps.supports_client_order_id:
            # C1: match on symbol too, not just the cid prefix — take-profit
            # ("limit") keeps its original prefix text (no symbol embedded,
            # per the byte-identical constraint), so without this check two
            # different symbols' TPs under the same deployment would be
            # mistaken for one another.
            existing = [o for o in ours if o["symbol"] == sym and
                        (o.get("client_order_id") or "")
                        .startswith(cid_prefix)]
        else:
            # CRITICAL 3 / IMPORTANT 4: qty+level identity, consuming the
            # matched order — see _match_no_cid. Since a moved level no
            # longer matches here, the release pass's cancel above is the
            # only cancel this sweep issues for it; this placement path
            # falls straight to the "else: submit" branch below instead of
            # re-cancelling the same broker order id a second time.
            matched = _match_no_cid(ours, sym, side, kind, qty, price,
                                    stop_px, trail)
            existing = [matched] if matched else []
        if have < qty:
            # The shares this exit is meant to protect are not (all) at the
            # broker. That is the ONLY thing the unfolded-fill rail ever
            # guarded on this path -- "the model still wants a resting sell
            # for a position the broker already sold" -- and live broker
            # truth answers it directly, per symbol, with no day-long latch
            # (see _fold_explains). When the fills the sleeve has not folded
            # are what explains the shortfall, say so precisely.
            if sym in fold_pending:
                report["actions"].append(
                    f"{label} {sym} frozen (broker fills of our own orders "
                    f"not yet folded into the sleeve)")
            else:
                report["actions"].append(
                    f"{label} for {sym} deferred until shares arrive")
            continue
        notional_ref = level if level is not None else px
        if rails.max_order_notional is not None and notional_ref \
                and qty * notional_ref > rails.max_order_notional:
            report["actions"].append(
                f"{label} {sym} @ {notional_ref:.2f} refused "
                f"(order notional > {rails.max_order_notional:,.0f})")
            record({"action": "refused", "symbol": sym, "qty": qty,
                    "side": side, "order_type": kind,
                    "limit_price": level, "broker_order_id": "",
                    "client_order_id": "", "status": "over_notional",
                    "deployment_id": dep_id, "rule_tag": rule_id})
            continue
        base = {"symbol": sym, "qty": qty, "side": side,
                "order_type": kind, "limit_price": level,
                "broker_order_id": "", "client_order_id": "",
                "status": "", "deployment_id": dep_id, "rule_tag": rule_id}

        def _submit(sym=sym, qty=qty, kind=kind, price=price,
                    stop_px=stop_px, trail=trail, cid=None):
            return adapter.submit(
                creds, sym, qty, side, order_type=kind,
                tif=pick_tif(adapter.caps, kind),
                limit_price=price, stop_price=stop_px,
                trail_percent=(trail * 100 if trail is not None else None),
                client_order_id=cid)

        if existing:
            o = existing[0]
            if kind == "trailing_stop":
                # a resting trailing stop's broker-reported stop_price
                # RATCHETS with the high-water mark — it is expected to
                # move every sweep and is not a signal of anything we need
                # to change. The only thing that identifies "this trailing
                # stop needs updating" is its configured trail_percent
                # itself; comparing against stop_price here would
                # cancel+resubmit (and re-anchor the trail) every sync.
                resting_trail_pct = o.get("trail_percent")
                resting_trail = (resting_trail_pct / 100.0
                                  if resting_trail_pct is not None else None)
                cmp_trail = trail
                if resting_trail is None and adapter.caps.supports_client_order_id:
                    # the adapter doesn't expose the resting trail_percent
                    # (e.g. Webull) — fall back to the level token we
                    # embedded in the client_order_id when this order was
                    # placed, so a real trail change still gets picked up
                    # instead of only ever noticing a qty change.
                    cid_pct = _decode_level(o.get("client_order_id") or "")
                    resting_trail = (cid_pct / 100.0
                                      if cid_pct is not None else None)
                    # the decoded value is quantized to cents-of-percent by
                    # construction (see _quantize_level) — quantize `trail`
                    # the same way, once, before comparing. Otherwise an
                    # unchanged trail whose distance to the nearest
                    # quantized step lands just past the 0.0005 tolerance
                    # would look "changed" every sweep purely from
                    # encoding rounding.
                    if trail is not None:
                        cmp_trail = _quantize_level(trail * 100) / 100.0
                if resting_trail is None:
                    # still unknown (no broker field AND no parseable
                    # token, e.g. an order placed before this code
                    # existed) — unknown is safer treated as unchanged
                    # than coerced to 0, which would cancel+resubmit (and
                    # re-anchor the trail's high-water mark) every sweep.
                    changed = o["qty"] != qty
                else:
                    changed = abs(resting_trail - (cmp_trail or 0)) > 0.0005 \
                        or o["qty"] != qty
            elif kind == "stop_limit":
                # stop_limit has TWO independent legs — the stop trigger
                # and the limit price — and either can go stale on its
                # own. The original bug compared the resting order's
                # stop_price against `level`, which for stop_limit was
                # the LIMIT leg: stop != limit by construction, so any
                # resting stop_limit at a broker that reports stop_price
                # (Alpaca, Schwab) looked "changed" every single sweep.
                # Compare both legs independently and treat either
                # differing as changed.
                resting_stop = o.get("stop_price")
                resting_limit = o.get("limit_price")
                cmp_stop, cmp_limit = stop_px, price
                if (resting_stop is None or resting_limit is None) \
                        and adapter.caps.supports_client_order_id:
                    tok_stop, tok_limit = _decode_stop_limit_legs(
                        o.get("client_order_id") or "")
                    if resting_stop is None:
                        resting_stop = tok_stop
                        if stop_px is not None:
                            cmp_stop = _quantize_level(stop_px)
                    if resting_limit is None:
                        resting_limit = tok_limit
                        if price is not None:
                            cmp_limit = _quantize_level(price)
                # each leg unknown (no broker field AND no parseable
                # token) is treated as unchanged on that leg alone —
                # same "unknown != changed" safety as the other kinds —
                # rather than forcing the whole order to look unchanged
                # just because one leg happens to be unreported.
                stop_changed = resting_stop is not None and \
                    abs(resting_stop - (cmp_stop or 0)) > 0.004
                limit_changed = resting_limit is not None and \
                    abs(resting_limit - (cmp_limit or 0)) > 0.004
                changed = stop_changed or limit_changed or o["qty"] != qty
            else:
                # a resting stop carries its reference level in
                # stop_price (not limit_price) — compare against that,
                # falling back to limit_price for plain limits (which is
                # exactly the original take-profit comparison, unchanged).
                resting_ref = o.get("stop_price")
                if resting_ref is None:
                    resting_ref = o.get("limit_price")
                cmp_level = level
                if resting_ref is None and adapter.caps.supports_client_order_id:
                    # the adapter doesn't expose a resting reference level
                    # for this order at all (e.g. Webull — a real,
                    # live-verified STOP_LOSS/STOP_LOSS_LIMIT order there
                    # whose level goes stale would otherwise sit
                    # unprotected forever unless qty also changes). Fall
                    # back to the level token embedded in the
                    # client_order_id at placement time.
                    resting_ref = _decode_level(o.get("client_order_id") or "")
                    # quantize `level` to the same cents precision the
                    # decoded token already carries, once, before
                    # comparing — see _quantize_level's docstring for why
                    # comparing raw vs. quantized would churn an unchanged
                    # sub-cent level (e.g. 380.0045) every sweep.
                    if level is not None:
                        cmp_level = _quantize_level(level)
                if resting_ref is None:
                    # still unknown (no broker field AND no parseable
                    # token, e.g. an order placed before this code
                    # existed) — unknown is safer treated as unchanged
                    # than coerced to 0, which would cancel+resubmit a
                    # live protective stop every sweep.
                    changed = o["qty"] != qty
                else:
                    changed = abs(resting_ref - (cmp_level or 0)) > 0.004 \
                        or o["qty"] != qty
            if changed:
                cancel_resubmit = not (kind == "limit"
                                       and adapter.caps.supports_replace)
                if cancel_resubmit and gateway_state["tripped"]:
                    # I7 hardening: cancel itself isn't gated by act() (a
                    # standalone release-cancel of an unwanted exit must
                    # keep working out of hours), but THIS cancel is only
                    # ever safe together with its resubmit -- if
                    # submissions are suppressed, cancelling now would
                    # delete the resting protective order and then have
                    # the resubmit blocked underneath it, stranding the
                    # position with NO order at the broker until the
                    # backoff clears. Skip the whole pair: a stale level
                    # on a resting order beats no order at all.
                    report["actions"].append(
                        f"{label} {sym} left resting at its current level "
                        "-- order submissions are paused (market gateway "
                        "not ready), not cancel+resubmit")
                    result = None
                elif kind == "limit" and adapter.caps.supports_replace:
                    def _replace(o=o, qty=qty, price=price):
                        return adapter.replace(creds, o["id"], qty=int(qty),
                                                limit_price=price)

                    result = act(_replace,
                                {**base, "action": "replace",
                                 "broker_order_id": o["id"]})
                else:
                    # non-limit kinds have no stop_price/trail_percent on
                    # adapter.replace() — always cancel + resubmit so the
                    # new level actually reaches the broker instead of
                    # leaving the resting order stale.
                    def _cancel(o=o):
                        return adapter.cancel(creds, o["id"])

                    act(_cancel,
                        {**base, "action": "cancel",
                         "broker_order_id": o["id"], "qty": o["qty"],
                         "limit_price": o.get("limit_price")})
                    cid = _cid_with_level(cid_prefix, kind, price, stop_px, trail)

                    # the resubmit, not the cancel, is what actually lands
                    # the new level at the broker -- gate the "updated"
                    # line on IT (I7: a suppressed submit must never read
                    # as a placed/updated order, see act()'s gateway gate).
                    result = act(lambda cid=cid: _submit(cid=cid),
                                {**base, "action": "submit",
                                 "client_order_id": cid})
                if result is not None:
                    lvl_s = f"{level:.2f}" if level is not None else "trail"
                    report["actions"].append(
                        f"{label} {sym} updated -> {qty} @ {lvl_s}")
        else:
            cid = _cid_with_level(cid_prefix, kind, price, stop_px, trail)

            result = act(lambda cid=cid: _submit(cid=cid),
                        {**base, "action": "submit", "client_order_id": cid})
            if result is not None:
                lvl_s = f"{level:.2f}" if level is not None else "trail"
                report["actions"].append(
                    f"{label} {sym} placed {qty} @ {lvl_s}")

    # 4) per-deployment resting BUY entries (breakout stop / pullback
    # limit / stop_limit — strategy-ir-spec.md §15.5): native where the
    # broker's Caps.order_types allow the kind, simulated (skipped here,
    # left to the replay to enter at market on breach) otherwise.
    #
    # CRITICAL — the stop-band rail INVERTS from the sell-exit block above:
    # these are BUY-side orders, so a buy stop must sit ABOVE the last
    # price (a buy stop AT or BELOW market would trigger an instant,
    # unintended market buy the moment it reached the broker), whereas a
    # sell stop must sit below the last price. Get this backwards and a
    # deploy sends a live market buy disguised as a resting order.
    for t in entry_wants:
        dep_id, sym, qty, kind, price, stop_px = t[:6]
        rule_id = t[6] if len(t) > 6 else None
        # IR entries are long by construction and carry no side, so the
        # default preserves that path exactly.
        e_side = t[7] if len(t) > 7 else "buy"
        note = capabilities.journal_note(adapter.caps, kind, sym)
        if note:
            report["actions"].append(f"entry {note}")
            continue
        qty = _round_step(qty, adapter.caps.qty_step)
        if _fold_explains(sym, qty):
            # a resting BUY of `qty` is the delta here: freeze only while
            # folding the outstanding fills would shrink it (same rail, same
            # latch fix as the market-delta pass above)
            report["actions"].append(
                f"entry {kind} {sym} frozen (broker fills of our own "
                f"orders not yet folded into the sleeve)")
            continue
        if qty <= 0:
            continue
        px = last_px.get(sym, 0.0)
        level = price if kind in ("limit", "stop_limit") else stop_px
        # C6: a breakout entry is by construction placed while FLAT, so
        # `last_px` (built only from HELD symbols plus the primary) is
        # empty for it — px == 0 here is the normal case, not the
        # exception. The invalid-stop refusal and stop-band check below
        # both live inside `px > 0` and silently no-op when it's 0, which
        # would let a buy stop at/below an unknown market reach the broker
        # and fire instantly as a market buy. Refuse instead, the same way
        # the market-delta pass already refuses on unknown price.
        if kind in ("stop", "stop_limit") and px <= 0:
            report["actions"].append(
                f"entry {kind} {sym} refused (unknown price — cannot "
                f"validate the {e_side}-stop rail)")
            if not _already_refused(dep_id, sym, kind, "no_price", level):
                record({"action": "refused", "symbol": sym, "qty": qty,
                        "side": e_side, "order_type": kind,
                        "limit_price": level, "broker_order_id": "",
                        "client_order_id": "", "status": "no_price",
                        "deployment_id": dep_id, "rule_tag": rule_id})
            continue
        # entries are buys: the mirror image — refuse one priced above the
        # market (instant fill through the offer), allow a pullback limit
        # resting below it at any distance.
        if kind == "limit":
            why = limit_price_refusal(e_side, level, px, rails)
            if why:
                report["actions"].append(
                    f"entry {sym} @ {level:.2f} skipped — {why}")
                continue
        if kind in ("stop", "stop_limit") and px > 0 and stop_px is not None:
            # The rail INVERTS with the side. A buy stop must rest ABOVE the
            # market (below it would trigger an instant unintended market buy
            # the moment it reached the broker); a SHORT entry's sell stop is
            # the mirror and must rest BELOW. Applying the buy rule to a
            # short entry would refuse every legitimate one and accept the
            # dangerous one.
            bad_stop = (stop_px <= px) if e_side == "buy" else (stop_px >= px)
            if bad_stop:
                report["actions"].append(
                    f"entry {kind} {sym} @ {stop_px:.2f} refused (stop "
                    f"price {'<=' if e_side == 'buy' else '>='} last price "
                    f"{px:.2f} — a {e_side} stop must rest "
                    f"{'above' if e_side == 'buy' else 'below'} the market)")
                if not _already_refused(dep_id, sym, kind, "invalid_stop",
                                        level):
                    record({"action": "refused", "symbol": sym, "qty": qty,
                            "side": e_side, "order_type": kind,
                            "limit_price": level, "broker_order_id": "",
                            "client_order_id": "", "status": "invalid_stop",
                            "deployment_id": dep_id, "rule_tag": rule_id})
                continue
            if abs(stop_px / px - 1) * 100 > rails.stop_band_pct:
                report["actions"].append(
                    f"entry {kind} {sym} @ {stop_px:.2f} refused "
                    f"(>{rails.stop_band_pct}% from last {px:.2f})")
                if not _already_refused(dep_id, sym, kind, "over_stop_band",
                                        level):
                    record({"action": "refused", "symbol": sym, "qty": qty,
                            "side": e_side, "order_type": kind,
                            "limit_price": level, "broker_order_id": "",
                            "client_order_id": "", "status": "over_stop_band",
                            "deployment_id": dep_id, "rule_tag": rule_id})
                continue
        cid_prefix = _entry_cid_prefix(dep_id, kind, sym, rule_id)
        if adapter.caps.supports_client_order_id:
            existing = [o for o in ours if o["symbol"] == sym and
                        (o.get("client_order_id") or "")
                        .startswith(cid_prefix)]
        else:
            # CRITICAL 3 / IMPORTANT 4 — same fix as the exits placement
            # path above, entries have no trailing_stop kind so trail=None.
            matched = _match_no_cid(ours, sym, e_side, kind, qty, price,
                                    stop_px, None)
            existing = [matched] if matched else []
        notional_ref = level if level is not None else px
        if rails.max_order_notional is not None and notional_ref \
                and qty * notional_ref > rails.max_order_notional:
            report["actions"].append(
                f"entry {kind} {sym} @ {notional_ref:.2f} refused "
                f"(order notional > {rails.max_order_notional:,.0f})")
            if not _already_refused(dep_id, sym, kind, "over_notional",
                                    level):
                record({"action": "refused", "symbol": sym, "qty": qty,
                        "side": e_side, "order_type": kind,
                        "limit_price": level, "broker_order_id": "",
                        "client_order_id": "", "status": "over_notional",
                        "deployment_id": dep_id, "rule_tag": rule_id})
            continue
        base = {"symbol": sym, "qty": qty, "side": e_side,
                "order_type": kind, "limit_price": level,
                "broker_order_id": "", "client_order_id": "",
                "status": "", "deployment_id": dep_id, "rule_tag": rule_id}

        def _submit(sym=sym, qty=qty, kind=kind, price=price,
                    stop_px=stop_px, cid=None, e_side=e_side):
            # An ENTRY increases exposure by definition, so a sell entry is
            # an opening short. The venue decides whether it can take one
            # (Caps.supports_short, checked inside submit) — locally, before
            # anything is sent.
            return adapter.submit(
                creds, sym, qty, e_side, order_type=kind,
                tif=pick_tif(adapter.caps, kind),
                limit_price=price, stop_price=stop_px,
                client_order_id=cid, opens_short=(e_side == "sell"))

        if existing:
            o = existing[0]
            if kind == "stop_limit":
                resting_stop = o.get("stop_price")
                resting_limit = o.get("limit_price")
                cmp_stop, cmp_limit = stop_px, price
                if (resting_stop is None or resting_limit is None) \
                        and adapter.caps.supports_client_order_id:
                    tok_stop, tok_limit = _decode_stop_limit_legs(
                        o.get("client_order_id") or "")
                    if resting_stop is None:
                        resting_stop = tok_stop
                        if stop_px is not None:
                            cmp_stop = _quantize_level(stop_px)
                    if resting_limit is None:
                        resting_limit = tok_limit
                        if price is not None:
                            cmp_limit = _quantize_level(price)
                stop_changed = resting_stop is not None and \
                    abs(resting_stop - (cmp_stop or 0)) > 0.004
                limit_changed = resting_limit is not None and \
                    abs(resting_limit - (cmp_limit or 0)) > 0.004
                changed = stop_changed or limit_changed or o["qty"] != qty
            else:
                resting_ref = o.get("stop_price")
                if resting_ref is None:
                    resting_ref = o.get("limit_price")
                cmp_level = level
                if resting_ref is None and adapter.caps.supports_client_order_id:
                    resting_ref = _decode_level(o.get("client_order_id") or "")
                    if level is not None:
                        cmp_level = _quantize_level(level)
                if resting_ref is None:
                    changed = o["qty"] != qty
                else:
                    changed = abs(resting_ref - (cmp_level or 0)) > 0.004 \
                        or o["qty"] != qty
            if changed:
                cancel_resubmit = not (kind == "limit"
                                       and adapter.caps.supports_replace)
                if cancel_resubmit and gateway_state["tripped"]:
                    # I7 hardening -- see the matching exit-leg comment
                    # above: skip the whole cancel+resubmit pair rather
                    # than delete the resting entry order and then have
                    # the resubmit blocked underneath it.
                    report["actions"].append(
                        f"entry {kind} {sym} left resting at its current "
                        "level -- order submissions are paused (market "
                        "gateway not ready), not cancel+resubmit")
                    result = None
                elif kind == "limit" and adapter.caps.supports_replace:
                    def _replace(o=o, qty=qty, price=price):
                        return adapter.replace(creds, o["id"], qty=int(qty),
                                                limit_price=price)

                    result = act(_replace,
                                {**base, "action": "replace",
                                 "broker_order_id": o["id"]})
                else:
                    def _cancel(o=o):
                        return adapter.cancel(creds, o["id"])

                    act(_cancel,
                        {**base, "action": "cancel",
                         "broker_order_id": o["id"], "qty": o["qty"],
                         "limit_price": o.get("limit_price")})
                    cid = _cid_with_level(cid_prefix, kind, price, stop_px,
                                          None)

                    # gate on the resubmit result, not the cancel -- see
                    # the matching exit-leg comment above.
                    result = act(lambda cid=cid: _submit(cid=cid),
                                {**base, "action": "submit",
                                 "client_order_id": cid})
                if result is not None:
                    lvl_s = f"{level:.2f}" if level is not None else "?"
                    report["actions"].append(
                        f"entry {kind} {sym} updated -> {qty} @ {lvl_s}")
        else:
            cid = _cid_with_level(cid_prefix, kind, price, stop_px, None)

            result = act(lambda cid=cid: _submit(cid=cid),
                        {**base, "action": "submit", "client_order_id": cid})
            if result is not None:
                lvl_s = f"{level:.2f}" if level is not None else "?"
                report["actions"].append(
                    f"entry {kind} {sym} placed {qty} @ {lvl_s}")

    # I7: a completed pass with no active/newly-tripped backoff explicitly
    # says so (rather than just never touching the key), so the caller can
    # tell "nothing to back off from" apart from "this pass never reached
    # the point of deciding" (the rails.paused / live_allowed early returns
    # above, or an exception escaping reconcile() entirely) -- the latter
    # must leave a real backoff alone rather than clear it on no evidence.
    report.setdefault("submit_backoff", None)


def broker_order_row(connection_id: str, entry: dict):
    from dqengine.live.persistence import BrokerOrder
    return BrokerOrder(
        connection_id=connection_id,
        deployment_id=entry.get("deployment_id"),
        broker_order_id=entry.get("broker_order_id") or None,
        client_order_id=entry.get("client_order_id") or None,
        symbol=entry["symbol"], qty=float(entry["qty"]),
        side=entry["side"], order_type=entry["order_type"],
        limit_price=entry.get("limit_price"),
        status=entry.get("status") or None, action=entry["action"],
        # C3: the rule that wanted this order. `executions.attribute()`
        # copies it onto the Execution, and it is spec §6 matching
        # precedence 1 -- the only thing that tells two same-symbol rules
        # on one day apart. Market deltas have no rule and pass None.
        rule_tag=entry.get("rule_tag"))


def _submit_backoff_kv_key(conn_id: str) -> str:
    return f"broker_submit_backoff:{conn_id}"


def _get_submit_backoff(conn_id: str) -> Optional[dict]:
    """I7: the still-active order-submission backoff persisted for this
    connection, if any -- KV (not BrokerConnection.settings, which is
    user-facing rails config) so restarts/redeploys keep it (see the KV
    model's docstring)."""
    from dqengine.live.persistence import KV, SessionLocal
    with SessionLocal() as s:
        row = s.get(KV, _submit_backoff_kv_key(conn_id))
        return row.value if row else None


def _save_submit_backoff(conn_id: str, backoff: Optional[dict]) -> None:
    """Persist (or clear, when `backoff` is None) this sync's backoff
    verdict from reconcile()'s report["submit_backoff"].

    sync_broker_account runs concurrently for the same connection from
    more than one caller (the driver's ticker, the quote feed, and the
    host's HTTP-triggered syncs) -- two sweeps that both trip a fresh
    backoff in the same instant can both see `row is None` and both try
    to INSERT, so the loser's read-then-insert race is caught here and
    retried as an UPDATE rather than allowed to raise. The whole call is
    additionally never allowed to escape to the caller: this write is
    best-effort persistence of a report line, and letting it raise would
    skip the execution-ledger poll immediately after it in
    sync_broker_account -- self-healing on the next sweep is fine, silently
    dropping fill truth for this one is not."""
    from sqlalchemy.exc import IntegrityError
    from dqengine.live.persistence import KV, SessionLocal
    key = _submit_backoff_kv_key(conn_id)
    try:
        with SessionLocal() as s:
            row = s.get(KV, key)
            if backoff is None:
                if row is not None:
                    s.delete(row)
                    s.commit()
                return
            if row is not None:
                row.value = backoff
                s.commit()
                return
            try:
                s.add(KV(key=key, value=backoff))
                s.commit()
            except IntegrityError:
                s.rollback()
                row = s.get(KV, key)
                if row is not None:
                    row.value = backoff
                    s.commit()
    except Exception as e:
        print(f"[broker_exec] submit-backoff persist failed for "
              f"{conn_id}: {e}", flush=True)


def sync_broker_account(conn_id: str, fast: bool = False) -> str:
    """Mirror executor entry point: one broker connection, any broker.

    fast=False (the AUDITOR): the full sweep -- real broker fetches,
    executions poll, and it TRANSMITS only while the fast path does not
    own the connection; when the fast path owns, mutations are recorded
    (never sent) and any un-explained would-be action freezes the book
    (direct-submit spec §6: one transmitter, drift is evidence).
    fast=True (the FAST PATH): book-backed reconcile -- the same gates,
    the same code, fed from memory; no poll (auditor cadence covers it);
    submits immediately. Returns 'submitted' | 'audited' | 'fallback' |
    'skipped' for callers and tests."""
    from dqengine.live import vault
    from dqengine.live import frames
    from dqengine.adapters import catalog as registry
    from dqengine.live.book import book_for
    from dqengine.live.persistence import (BrokerConnection, BrokerOrder,
                                           Deployment, SessionLocal,
                                           managed_deployments)

    book = book_for(conn_id)
    if fast:
        ok, why = book.fast_path_ok()
        if not ok:
            return "fallback"
        gate = _SYNC_GATE.get(conn_id, {})
        if gate.get("cooldown_until", 0.0) > time.time():
            return "fallback"          # broker rate limit: no fast submits

    # Sweep pacing (lean-fills spec §3.5): a rate-limit cooldown pauses the
    # whole sweep; the floor collapses per-bar sync bursts. Both skips are
    # safe -- another sync event follows within seconds (worker per bar)
    # and tick_all's 60s poll is the standing safety net.
    #
    # EXCEPT inside the close window: the 15:59 rebalance sync arrives ~5s
    # after another deployment's routine sync on the same connection, and a
    # skipped sync there is not "retried in a few seconds" -- the next
    # natural sync lands after the gateway closes and the day's orders are
    # lost (the 2026-08-25 miss, re-armed by the pacing layer itself). In
    # the final minutes before the close, every sync runs.
    now_s = time.time()
    if not fast:
        if not _in_close_window():
            gate = _SYNC_GATE.get(conn_id, {})
            if gate.get("cooldown_until", 0.0) > now_s:
                return "skipped"
            if now_s - gate.get("last", 0.0) < SYNC_FLOOR_S:
                return "skipped"
        # fast passes do not touch gate.last: they must never postpone
        # the auditor cadence
        _SYNC_GATE[conn_id] = {**_SYNC_GATE.get(conn_id, {}),
                               "last": now_s}

    with SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None or not conn.creds_encrypted:
            return "skipped"
        if conn.status in ("reconnect_needed", "error", "pending"):
            return "skipped"
        try:
            adapter = registry.get_adapter(conn.broker)
        except (KeyError, LookupError) as exc:
            # A connection with no deployments on it is an empty slot and
            # has nothing to say. A connection that is RUNNING a deployment
            # and has no adapter never sends an order and never says why:
            # every sweep returns "skipped", the tick reports success, and
            # the order path is dead with a green status. Say it instead --
            # in the log and on the deployment row `dqengine status` reads,
            # once per sweep, for as long as it is true.
            deps = managed_deployments(s, conn_id)
            if deps:
                why = (f"no usable adapter for broker {conn.broker!r}: "
                       f"{exc}. Nothing was sent, and nothing will be until "
                       f"this connection names a broker installed here — "
                       f"`dqengine brokers` lists them.")
                print(f"[exec] connection {conn_id}: {why}", flush=True)
                rep = {"synced_at": datetime.now(timezone.utc).isoformat(),
                       "mode": "fast" if fast else "audit",
                       "actions": [], "errors": [why],
                       "executions": {
                           "new": 0, "skipped": 0,
                           "error": "no adapter: the account was not read"}}
                for d in deps:
                    pos = dict(d.position or {})
                    pos["execution"] = rep
                    d.position = pos
                s.commit()
            return "skipped"                # roster slot without an adapter
        creds = vault.decrypt_creds(conn.creds_encrypted)
        deps = managed_deployments(s, conn_id)
        from dqengine.live.driver.deployment import _dep_universe
        dep_states = [(d.id, d.position or {}, _dep_universe(d))
                      for d in deps]
        # Rail input (lean-fills spec §3.4): what the sleeves have FOLDED
        # today -- confirmed fills only. Unconfirmed model fills are the
        # normal model-ahead-of-broker state of a just-signaled order and
        # must not freeze anything. The fills arrive through the owners in
        # payload order and are summed here exactly as they always were:
        # sequentially and signed, one fill at a time.
        today_iso = datetime.now(ET).date().isoformat()
        owners = [owner_of(d, today_iso) for d in deps]
        model_folded: dict = {}
        model_folded_side: dict = {}     # {SYM: {buy, sell}} for fold-lift
        for o in owners:
            for fsym, fq in o.fills_today:
                model_folded[fsym] = model_folded.get(fsym, 0.0) + fq
                ms = model_folded_side.setdefault(
                    fsym, {"buy": 0.0, "sell": 0.0})
                ms["buy" if fq > 0 else "sell"] += abs(fq)
        rails = rails_from(conn.settings, conn.mode,
                           all(o.live_confirmed for o in owners)
                           if owners else False)
        truth_mode = conn.execution_truth or "off"
        # buying-power proof for the blind launcher: a conservative read of
        # the cached balance (a margin account's real intraday BP is higher;
        # under-claiming only costs one ack-gap, never a reject)
        bal = conn.balance or {}
        try:
            buying_power = max(float(bal.get("cash") or 0.0),
                               float(bal.get("equity") or 0.0) * 0.95)
        except (TypeError, ValueError):
            buying_power = 0.0
        buying_power = buying_power or None

        # C2: build the (deployment_id, symbol) set of native MOC/LOC
        # submits already recorded TODAY, regardless of their current
        # broker status — reconcile()'s in-flight check alone only sees a
        # still-OPEN ticket, so once ours fills (the normal, successful
        # case) it drops out of open_orders() and would otherwise get
        # resubmitted, plus a compensating market order, every sweep for
        # the rest of the day. "Today" is the US market calendar day (ET),
        # matching the replay's own day boundary.
        _gc_entry = _GATHER_CACHE.get(conn_id)
        _use_cache = bool(fast and _gc_entry
                          and time.time() - _gc_entry["at"] < GATHER_TTL_S)
        today_start_et = datetime.combine(
            datetime.now(ET).date(), datetime.min.time(), tzinfo=ET)
        moc_done = set()
        if _use_cache:
            moc_done = _gc_entry["moc_done"]
        elif deps:
            rows = (s.query(BrokerOrder.deployment_id, BrokerOrder.symbol)
                    .filter(BrokerOrder.connection_id == conn_id,
                            BrokerOrder.action == "submit",
                            BrokerOrder.order_type.in_(
                                ["market_on_close", "limit_on_close"]),
                            # IMPORTANT 5: a dry-run preview never reached the
                            # broker — it must not count as "today's real
                            # MOC/LOC already submitted", or previewing a
                            # morning in dry-run and then switching dry-run
                            # off silently suppresses that day's REAL
                            # market-on-close order.
                            BrokerOrder.status != "dry_run",
                            BrokerOrder.created_at >= today_start_et)
                    .all())
            moc_done = {(dep_id, sym) for dep_id, sym in rows}

        # I6: today's already-recorded `refused` rows, fingerprinted by
        # (deployment, symbol, order_type, status, level) — reconcile()
        # uses this to skip re-recording an identical refusal for an
        # unchanged standing want every sweep (see _already_refused).
        recent_refusals = set()
        if _use_cache:
            recent_refusals = _gc_entry["refusals"]
        elif deps:
            refused_rows = (
                s.query(BrokerOrder.deployment_id, BrokerOrder.symbol,
                        BrokerOrder.order_type, BrokerOrder.status,
                        BrokerOrder.limit_price)
                .filter(BrokerOrder.connection_id == conn_id,
                        BrokerOrder.action == "refused",
                        BrokerOrder.created_at >= today_start_et)
                .all())
            recent_refusals = {
                (dep_id, sym, kind, status,
                 round(lp, 2) if lp is not None else None)
                for dep_id, sym, kind, status, lp in refused_rows}

        # C-QB: symbols with a quote-driven fast-path exit
        # (execute_quote_breach_exit) submitted in the last QB_COOLDOWN_S --
        # see reconcile()'s "2) market deltas" comment for why the
        # market-delta pass must sit this symbol out until the replay's own
        # bar-driven accounting has had a chance to catch up.
        qb_cooldown = set()
        if _use_cache:
            qb_cooldown = _gc_entry["qb"]
        else:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=QB_COOLDOWN_S))
            qb_rows = (s.query(BrokerOrder.symbol)
                      .filter(BrokerOrder.connection_id == conn_id,
                              BrokerOrder.action == "submit",
                              BrokerOrder.client_order_id.like("sl-qb-%"),
                              BrokerOrder.created_at >= cutoff)
                      .all())
            qb_cooldown = {sym for (sym,) in qb_rows}
        # fold-pending inputs (rail): cached with the same TTL
        exe_sums: dict = {}
        if truth_mode == "enforce":
            if _use_cache and _gc_entry.get("exe_sums") is not None:
                exe_sums = _gc_entry["exe_sums"]
            else:
                exe_sums = _exe_sums_read(s, conn_id)
        # journal gates (spec: 2026-08-31-order-journal.md): per-symbol
        # open/unfolded/unresolved/epoch from the write-ahead ledger. Safe
        # to serve from the 2s cache -- the UNIQUE(conn, cid) constraint,
        # not this read, is the duplicate gate.
        from dqengine.live import journal as journal_mod
        if _use_cache \
                and _gc_entry.get("journal_gates") is not None:
            journal_gates = _gc_entry["journal_gates"]
        else:
            journal_gates = _journal_gates_read(
                s, conn_id, truth_mode, today_start_et, model_folded_side)
        # first-sync preflight input: the symbols this executor has ever
        # ordered on this connection. Read HERE, in the gather block, which
        # runs before the executions poll below -- a poll that ingests the
        # account's own history must not be able to answer the question
        # "have we traded this symbol" for the sweep that is asking it.
        if _use_cache and _gc_entry.get("known_symbols") is not None:
            known_symbols = _gc_entry["known_symbols"]
        else:
            known_symbols = _known_symbols_read(s, conn_id)
        if not _use_cache:
            _GATHER_CACHE[conn_id] = {
                "at": time.time(), "moc_done": moc_done,
                "refusals": recent_refusals, "qb": qb_cooldown,
                "exe_sums": (exe_sums if truth_mode == "enforce"
                             else None),
                "known_symbols": known_symbols,
                "journal_gates": journal_gates}

    owner_fast = (not fast) and book.fast_path_ok()[0]
    report = {"synced_at": datetime.now(timezone.utc).isoformat(),
              "mode": "fast" if fast else "audit",
              "actions": [], "errors": []}
    entries = []
    # The reconcile frame (frames.py): `out` holds the live objects this
    # sweep is about to fill, read once at the tail after the last mutation.
    frame = {"v": 1, "conn_id": conn_id, "mode": report["mode"],
             "now": report["synced_at"],
             "out": {"entries": entries, "report": report}}
    pass_ok = False        # a fast pass that blew up must report it so
    try:                   # handle_intent falls back to the full sweep
      # our own threads first, then the other processes: never hold the
      # database lock while waiting on a lock of our own
      with _conn_lock(conn_id), conn_sweep_lock(conn_id) as sweep_held:
        if not sweep_held:
            print(f"[exec] connection {conn_id}: another process holds the "
                  f"sweep lock — this process sent nothing", flush=True)
            # same shape as the pacing skips above: nothing was sent, so
            # there is nothing to record
            return "fallback" if fast else "skipped"
        if fast and book.creds is not None \
                and time.time() - book.creds_at < 60:
            # session freshness is the auditor's job on its cadence; the
            # hot path must not spend a network call re-proving it
            creds = book.creds
        else:
            updated = adapter.ensure_session(creds)
            if updated:
                creds = updated
                with SessionLocal() as s:
                    c = s.get(BrokerConnection, conn_id)
                    c.creds_encrypted = vault.encrypt_creds(creds)
                    s.commit()
            with book.lock:
                book.creds = creds
                book.creds_at = time.time()
        # The ledger is polled BEFORE reconcile (moved 2026-08-25): the
        # rail below compares broker fills against what the sleeves have
        # folded, and polling after reconcile left it blind in exactly the
        # window that mattered (a resting order's fill seconds before the
        # sweep). EXCEPT inside the close window (2026-08-26 latency work):
        # there the submit path runs FIRST -- the before-close orders are
        # racing the gateway cutoff, and a ~0.5s poll ahead of them buys
        # nothing (the rail still reads the previous sweep's rows, seconds
        # old at close-window sync cadence). Fills created by THIS sweep's
        # own submissions still land next sweep. A polling failure is
        # recorded, never fatal: an unreachable broker means UNKNOWN, and
        # unknown must not abort order management.
        poll_deferred = _in_close_window() or fast
        report["executions"] = {"new": 0, "skipped": 0, "error": None}
        if truth_mode in ("observe", "enforce") and not poll_deferred:
            _poll_into(report, adapter, creds, conn_id)
            # Re-read the journal gates AND the exe_sums rail AFTER the
            # poll (2026-09-01: gathering them before it let a row the poll
            # had just folded read as still open -> the BUY 52 reversal;
            # the rail was one poll behind since 8/25). Fresh into the
            # gather cache too, so a fast pass seconds later sees them.
            try:
                with SessionLocal() as js:
                    journal_gates = _journal_gates_read(
                        js, conn_id, truth_mode, today_start_et,
                        model_folded_side)
                    if truth_mode == "enforce":
                        exe_sums = _exe_sums_read(js, conn_id)
                gc = _GATHER_CACHE.get(conn_id)
                if gc is not None:
                    gc["journal_gates"] = journal_gates
                    gc["exe_sums"] = (exe_sums if truth_mode == "enforce"
                                      else None)
            except Exception as e:
                print(f"[journal] post-poll refresh failed {conn_id}: "
                      f"{e!r}", flush=True)

        # THE RAIL (lean-fills spec §3.4): symbols where today's fills of
        # OUR OWN orders (sl- client ids; manual trades excluded) differ
        # from what the sleeves have folded. reconcile() sits these out
        # entirely -- never fight the broker over a divergence we can
        # already explain.
        # {SYM: signed unfolded qty} -- the MAGNITUDE matters, not just
        # membership: reconcile freezes a trade only when folding these
        # shares would actually shrink it (_fold_explains). A plain set of
        # symbols is what latched a symbol for a whole session.
        fold_pending = {}
        if truth_mode == "enforce":
            for fsym in set(exe_sums) | set(model_folded):
                fdiff = (exe_sums.get(fsym, 0.0)
                         - model_folded.get(fsym, 0.0))
                if abs(fdiff) > 1e-6:
                    fold_pending[fsym] = fdiff

        # one state per deployment, folded by the private combiner when this
        # connection has more than one of them. Built HERE, inside the try:
        # a malformed payload is a report error with the tail still running
        # (H26), not a sweep that vanishes before it writes anything down.
        inputs = connection_inputs(dep_states, conn_id, owners)
        desired = inputs.state.desired
        exit_wants = inputs.state.exit_wants
        last_px = inputs.state.last_px
        moc_wants = inputs.state.close_wants
        entry_wants = inputs.state.entry_wants
        if fast:
            wrapped = BookAdapter(adapter, book)
        else:
            wrapped = AuditAdapter(adapter, book,
                                   transmit=not owner_fast)
        # read once and handed to both branches below, so the frame records
        # the same backoff reconcile() was given
        submit_backoff_in = _get_submit_backoff(conn_id)
        frame.update(dep_states=dep_states, desired=desired,
                     exit_wants=exit_wants, entry_wants=entry_wants,
                     close_wants=moc_wants, last_px=last_px,
                     held_px=inputs.state.held_px, rails=rails,
                     truth_mode=truth_mode, moc_done=moc_done,
                     recent_refusals=recent_refusals, qb_cooldown=qb_cooldown,
                     fold_pending=fold_pending, journal_gates=journal_gates,
                     submit_backoff_in=submit_backoff_in,
                     known_symbols=known_symbols,
                     buying_power=buying_power, adapter=wrapped)
        if owner_fast:
            # the fast path owns transmission: this pass audits. Run the
            # SAME reconcile read-only; a would-be action the book's own
            # recent activity cannot explain is drift -- freeze, loudly.
            shadow = {"synced_at": report["synced_at"], "actions": [],
                      "errors": []}
            reconcile(wrapped, creds, desired, exit_wants, last_px, rails,
                      shadow, lambda e: None, moc_wants=moc_wants,
                      entry_wants=entry_wants, moc_done=moc_done,
                      recent_refusals=recent_refusals,
                      qb_cooldown=qb_cooldown, fold_pending=fold_pending,
                      buying_power=buying_power,
                      submit_backoff=submit_backoff_in,
                      conn_id=None,   # shadow: never transmits, never journals
                      journal_gates=journal_gates,
                      known_symbols=known_symbols)
            settle = book.recent_symbols()
            drift = [r for r in wrapped.recorded
                     if r["symbol"] and r["symbol"] not in settle]
            if wrapped.fetched_positions is not None:
                book.apply_audit(wrapped.fetched_positions,
                                 wrapped.fetched_open_orders or [])
            if drift:
                why = (f"auditor drift: {len(drift)} would-be action(s), "
                       f"e.g. {drift[0]}")
                book.freeze(why)
                report["errors"].append(why[:300])
            report["actions"].append(
                "fast path owns transmission — audit only")
            # ...and carry WHAT the read-only pass decided (2026-09-02): the
            # shadow's reasoning used to be dropped on the floor, so a want
            # that a rail was quietly suppressing every sweep showed up in
            # the UI as this one content-free line. Whichever of the two
            # passes wrote the deployment's report last, the operator now
            # sees why nothing happened.
            report["actions"].extend(shadow["actions"][:20])
            report["errors"].extend(shadow["errors"][:5])
        else:
            reconcile(wrapped, creds, desired, exit_wants, last_px, rails,
                      report, entries.append, moc_wants=moc_wants,
                      entry_wants=entry_wants, moc_done=moc_done,
                      recent_refusals=recent_refusals,
                      qb_cooldown=qb_cooldown, fold_pending=fold_pending,
                      buying_power=buying_power,
                      submit_backoff=submit_backoff_in,
                      conn_id=conn_id, journal_gates=journal_gates,
                      known_symbols=known_symbols)
            if (not fast and isinstance(wrapped, AuditAdapter)
                    and wrapped.fetched_positions is not None):
                book.apply_audit(wrapped.fetched_positions,
                                 wrapped.fetched_open_orders or [])
        if truth_mode in ("observe", "enforce") and poll_deferred \
                and not fast:
            _poll_into(report, adapter, creds, conn_id)
        # journal recovery (spec: 2026-08-31-order-journal.md): resolve
        # aged `sending` rows against this sweep's own venue evidence --
        # the audit's open-orders fetch and the executions poll verdict.
        # Auditor-only (the fast path fetches nothing) and never fatal.
        if not fast:
            try:
                poll_ok = (truth_mode in ("observe", "enforce")
                           and (report.get("executions") or {})
                           .get("error") is None)
                with SessionLocal() as js:
                    acts = journal_mod.resolve_stale(
                        js, conn_id,
                        getattr(wrapped, "fetched_open_orders", None),
                        poll_ok)
                    js.commit()
                for a in acts:
                    key = "errors" if "ABANDONED" in a else "actions"
                    report[key].append(f"[journal] {a}")
                    print(f"[journal] {conn_id}: {a}", flush=True)
            except Exception as e:
                print(f"[journal] recovery failed {conn_id}: {e!r}",
                      flush=True)
        pass_ok = True
    except BrokerAuthExpired as e:
        report["errors"].append(str(e)[:300])
        with SessionLocal() as s:
            c = s.get(BrokerConnection, conn_id)
            c.status = "reconnect_needed"
            c.balance_error = str(e)[:300]
            s.commit()
    except BrokerUnavailable as e:
        report["errors"].append(str(e)[:300])
    except Exception as e:
        report["errors"].append(str(e)[:300])

    # I7: persist reconcile()'s backoff verdict, but only when it actually
    # reached one (see reconcile()'s trailing setdefault) -- a pass that
    # never got that far (paused, live-gate closed, or an exception above)
    # must leave any real, still-active backoff alone rather than clear it
    # on no evidence.
    if "submit_backoff" in report:
        _save_submit_backoff(conn_id, report["submit_backoff"])

    # (The executions poll runs BEFORE reconcile now -- see inside the try
    # block above. An exception before it leaves report["executions"]
    # unset; keep the key present so the UI panel and _unknown_symbols
    # read a coherent shape: no poll happened, state is unknown.)
    report.setdefault("executions",
                      {"new": 0, "skipped": 0,
                       "error": "sweep aborted before the executions poll"})

    if _rate_limited(report):
        _SYNC_GATE.setdefault(conn_id, {})["cooldown_until"] = (
            time.time() + RATE_LIMIT_COOLDOWN_S)
        report["actions"].append(
            f"sync paused {RATE_LIMIT_COOLDOWN_S}s — broker rate limit")
        print(f"[broker_exec] rate-limited by broker; sync for {conn_id} "
              f"paused {RATE_LIMIT_COOLDOWN_S}s", flush=True)

    with SessionLocal() as s:
        for entry in entries:
            s.add(broker_order_row(conn_id, entry))
        for dep_id, _, _ in dep_states:
            d = s.get(Deployment, dep_id)
            if d is not None:
                pos = dict(d.position or {})
                rep = report
                if fast:
                    # THE FAST PATH NEVER POLLS: overwriting the last
                    # audit's executions block with {error: None} would
                    # erase a live poll-error -- and with it the UNKNOWN
                    # protection that keeps absence from reading as a
                    # confirmed no-fill (the duplicate-position class).
                    # Carry the previous poll verdict forward untouched.
                    prev = ((pos.get("execution") or {})
                            .get("executions"))
                    if prev is not None:
                        rep = {**report, "executions": prev}
                pos["execution"] = rep
                d.position = pos
        s.commit()
    try:               # off both locks, after the last commit: bookkeeping
        frames.emit(frame)
    except Exception as e:
        print(f"[frames] emit failed {conn_id}: {e!r}", flush=True)
    if fast:
        return "submitted" if pass_ok else "error"
    return "audited"


def handle_intent(conn_id: str, sweep) -> str:
    """Direct-submit entry (spec §4): try the fast path -- the book-backed
    reconcile, ~150-300ms signal->broker. Anything short of 'submitted'
    falls back to `sweep`, the caller's full pass, i.e. today's exact path.
    The worst case of the fast lane is the current system, by code."""
    import time as _time
    t0 = _time.monotonic()
    try:
        res = sync_broker_account(conn_id, fast=True)
    except Exception as e:
        print(f"[oms] fast path error {conn_id}: {e!r}", flush=True)
        res = "error"
    if res == "submitted":
        print(f"[oms] intent->submit ms={(_time.monotonic() - t0) * 1000:.0f} "
              f"conn={conn_id}", flush=True)
        return res
    sweep(conn_id)
    return f"fallback:{res}"


def _journal_health() -> dict:
    """Counters for /api/health: open rows (sending/submitted/open),
    unresolved (sending past the ack deadline), abandoned today. Any
    non-zero abandoned count is an operator alarm."""
    from dqengine.live import persistence
    from dqengine.live import journal as journal_mod
    out = {"enabled": True, "open": None,
           "unresolved": None, "abandoned_today": None}
    try:
        from datetime import datetime, time as dtime, timedelta
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        today0 = datetime.combine(datetime.now(et).date(), dtime.min,
                                  tzinfo=et)
        stale_before = (datetime.now(timezone.utc)
                        - timedelta(seconds=journal_mod.ACK_DEADLINE_S))
        with persistence.SessionLocal() as s:
            q = s.query(persistence.OrderJournal)
            out["open"] = q.filter(persistence.OrderJournal.state.in_(
                ["sending", "submitted", "open"])).count()
            out["unresolved"] = q.filter(
                persistence.OrderJournal.state == "sending",
                persistence.OrderJournal.created_at < stale_before).count()
            out["abandoned_today"] = q.filter(
                persistence.OrderJournal.state == "abandoned",
                persistence.OrderJournal.created_at >= today0).count()
    except Exception as e:
        print(f"[journal] health read failed: {e!r}", flush=True)
    return out
