"""The deployment a tick is about: its session clock, its readers, its code.

A leaf. The only other driver module it reads is `ports`, which imports
nothing itself, so the tick, the warm engine and the worker loop can all
read this one without a cycle.

Three groups, and they are unrelated to each other beyond belonging to one
deployment: when the session is open and when the next tick is due; the
column-first readers that answer what a deployment trades, at what resolution,
and on what source; and the convergence marker the executor sets when broker
truth lands under a warm engine (spec 2026-09-19 §3.6).
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dqengine.runtime.core.data import SessionCalendar

ET = ZoneInfo("America/New_York")


OPEN_S = 9 * 3600 + 30 * 60   # 09:30 ET, seconds since midnight


def market_is_open_now() -> bool:
    """Holiday- and early-close-aware (LEAN's exchange hours, our rules)."""
    from dqengine.runtime.core.data import is_market_holiday
    now = datetime.now(ET)
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    s = now.hour * 3600 + now.minute * 60
    return OPEN_S <= s <= SessionCalendar.close_time_ms(now.date()) / 1000


def _session_anchors_s(day) -> list:
    """Seconds-since-midnight-ET of today's schedule anchors (session-timing-
    and-realtime-design.md §2): shortly after the open, close_time - 10
    minutes -- the tick that makes an on-close market/limit order reachable,
    since it lands inside every broker's acceptance window (Alpaca cuts off
    ~15:55) -- and just after the close, to settle end-of-day state. Uses
    the session clock (`SessionCalendar.close_time_ms`), so early-close days
    (13:00 ET) schedule correctly, not just normal 16:00 ET days."""
    close_s = SessionCalendar.close_time_ms(day) / 1000.0
    # close-45s: the bar ending at close-60s is persisted a few seconds
    # after it closes; this anchor steps it with ~45s left for the executor
    # to reach the venue. The python warm path has no quote-primed
    # before-close fire, so without it a before_market_close(1) rule would
    # be stepped by the close+60 tick -- after the venue's cutoff.
    anchors = [OPEN_S + 60, close_s - 600, close_s - 45, close_s + 60]
    return anchors


def next_tick_delay_s(now: datetime = None, interval_open_s: int = 60,
                      interval_closed_s: int = 1800) -> float:
    """How long to sleep before the next ticker wake-up. Session-aware
    (§2a): a 60s cadence spans the regular session (tight enough that the
    close-10-minutes anchor below is never missed by more than a few
    seconds), a slow cadence outside market hours/weekends, and the sleep
    is additionally capped so a wake-up lands AT (not after) each schedule
    anchor -- most importantly close_time - 10 minutes, since overshooting
    that window is exactly the "structurally late" bug this schedule
    replaces (before_close/at_close/MOC orders landing after the broker's
    acceptance window closes)."""
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return float(interval_closed_s)
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    close_s = SessionCalendar.close_time_ms(now.date()) / 1000.0
    if now_s < OPEN_S:
        # cap the overnight sleep so the first tick of the day lands AT the
        # open anchor (09:31), never past it — an uncapped 1800s sleep taken
        # shortly before the open pushed the day's first tick (and with it
        # every open-driven order) up to ~30 minutes into the session
        # (observed live: 2026-08-19 first tick at 09:50:30)
        return max(min(float(interval_closed_s),
                       (OPEN_S + 60) - now_s), 1.0)
    if now_s > close_s + 60:
        return float(interval_closed_s)
    delay = float(interval_open_s)
    upcoming = [a - now_s for a in _session_anchors_s(now.date()) if a > now_s]
    if upcoming:
        delay = min(delay, min(upcoming))
    return max(delay, 1.0)


def sleeve_equity_now(equity_values, equity_days, cash_initial, events):
    """Current sleeve worth, counting deposits the replay hasn't reached.

    The engine only applies a deposit at a session open, so one dated after
    the last replayed session — or made before the sleeve's first session ever
    runs — is absent from the curve. It's still real money in the account that
    belongs to this sleeve; leave it out and the allocation math shows the
    top-up as "not managed by a strategy" and the add-cash dialog offers the
    same dollars as free room again."""
    last_day = equity_days[-1] if equity_days else None
    pending = sum(e.amount for e in events
                  if e.kind == "deposit"
                  and (last_day is None or e.effective_date > last_day))
    base = equity_values[-1] if equity_values else cash_initial
    return base + pending


def mark_warm_stale(dep_id: str, reason: str) -> None:
    """Broker truth arrived (new execution rows ingested for an enforce
    deployment): the warm engine's live-day state was built on unconfirmed
    model fills and must be rebuilt from the ledger. Sets a bus marker for
    the process that holds the engine (a worker owns its deployment's
    engine; the sweep that ingests the executions may run elsewhere), which
    the warm tick consumes via _consume_stale_marker on its
    next tick. Never raises — the sweep must not die because a convergence
    signal could not be sent; the post-roll audit is the backstop when a
    marker is lost.

    Until Phase 3 this ALSO poked the IR warm registry directly, for the
    same-process case. That registry is gone, and the python warm engine
    never had such a poke: it has only ever converged through the marker,
    in-process and in a worker alike. Nothing changes for it here."""
    try:
        from dqengine.live import bus as bus_mod
        if bus_mod.BUS is not None:
            bus_mod.BUS.set_ex(f"warm:stale:{dep_id}", reason, 3600)
    except Exception as e:
        print(f"[warm] stale marker publish failed dep={dep_id}: {e!r}",
              flush=True)


def _consume_stale_marker(dep_id: str):
    """The marker's reason if one is set (deleting it), else None."""
    try:
        from dqengine.live import bus as bus_mod
        if bus_mod.BUS is None:
            return None
        reason = bus_mod.BUS.get(f"warm:stale:{dep_id}")
        if reason is not None:
            bus_mod.BUS.delete(f"warm:stale:{dep_id}")
        return reason
    except Exception as e:
        # an unreachable bus must not take the warm path down with it;
        # convergence falls back to the nightly audit
        print(f"[warm] stale marker check failed dep={dep_id}: {e!r}",
              flush=True)
        return None


def _deposit_count(events: list) -> int:
    return sum(1 for e in events if e.kind == "deposit")


POST_CLOSE_DECIDABLE_S = 600     # 10 min of clean sweeps after the close


def _capped_live_from() -> date:
    """The first day whose ABSENCE is still unknowable (LiveCappedLedger's
    live_from). During the session that is today: an order can fill any
    second and the sweep lags fills by seconds. From close+10min, today is
    DECIDABLE -- every genuine fill has long since been polled, so a fill
    row that never appeared is a real miss and must fold as one. Without
    this, a day whose orders were refused kept its provisional model fills
    alive all evening (2026-08-25: the page showed six 'pending' fills for
    orders the gateway had refused at the close, and a stale want could
    have queued them at the 04:00 gateway window)."""
    now = datetime.now(ET)
    today = now.date()
    if now.weekday() < 5:
        try:
            close_ms = SessionCalendar.close_time_ms(today)
        except Exception:
            return today
        now_ms = (now.hour * 3600 + now.minute * 60 + now.second) * 1000
        if now_ms >= close_ms + POST_CLOSE_DECIDABLE_S * 1000:
            return today + timedelta(days=1)
    return today


def _dep_universe(dep) -> list:
    """What this deployment trades. The column is the source of truth for
    both kinds; the IR fallback covers rows written before the migration.
    Never raises -- an IR that is malformed (or not a dict at all) degrades
    to an empty universe, the same guard the unknown-symbol reader on the
    execution side shipped with."""
    if dep.universe:
        return list(dep.universe)
    uni = dep.ir.get("universe") if isinstance(dep.ir, dict) else None
    return [str(u) for u in ((uni.get("static") or [])
                             if isinstance(uni, dict) else [])]


def _row_resolution(column, ir) -> str:
    if column:
        return column
    meta = ir.get("meta") if isinstance(ir, dict) else None
    return (meta or {}).get("resolution") or "minute"


def _dep_resolution(dep) -> str:
    """Bar resolution, column first. The IR fallback covers rows written
    before the migration and in-memory rows whose column default has not
    been applied yet (SQLAlchemy applies it on INSERT, not on construct)."""
    if dep.resolution:
        return dep.resolution
    meta = dep.ir.get("meta") if isinstance(dep.ir, dict) else None
    return (meta or {}).get("resolution") or "minute"


# compiled block strategies, keyed by (deployment id, ir fingerprint). The
# compile is pure and cheap, but it runs on every tick otherwise, and an IR
# edit must invalidate it -- editing a strategy never mutates a RUNNING
# deployment's snapshot, so in practice this is compiled once per deployment.
_COMPILED: dict[tuple, str] = {}


def _resolution_of(dep) -> str:
    """dep resolution without assuming the row shape (test doubles and
    older rows may lack the column): column, then ir.meta, else minute."""
    r = getattr(dep, "resolution", None)
    if r:
        return r
    ir = getattr(dep, "ir", None)
    meta = ir.get("meta") if isinstance(ir, dict) else None
    return (meta or {}).get("resolution") or "minute"


def daily_broker_refusal(conn) -> str | None:
    """Why this brokerage connection cannot take a DAILY deployment, or None.

    Asked at deploy (the hosted deploy endpoint, and `dqengine live`) and
    again on every tick, from one place, so the dialog and the router cannot
    answer differently.

    One reason remains:

    * The connection must be in `enforce`. A daily deployment's at-close
      order is sent a minute before the close and the model settles the
      ticket a minute AFTER it, so between those two moments only the
      broker's own execution rows say whether the order went out. Outside
      `enforce` the ledger never reaches accounting, the model books its own
      fill either way, and a missed order would be indistinguishable from a
      filled one -- which is the difference between no order and an
      after-hours one.
    """
    if conn is None:
        return ("this deployment points at a brokerage connection that no "
                "longer exists")
    from dqengine.adapters import catalog as _registry
    try:
        _registry.get_adapter(conn.broker)
    except LookupError as e:
        return str(e)
    if (conn.execution_truth or "off") != "enforce":
        return ("a daily strategy needs this connection set to `enforce`, so "
                "the broker's own fills drive the account. Its orders fill at "
                "the session's close and the next session's open, and only "
                "the broker can say whether one of them went out.")
    return None


def _python_code_for(dep) -> str:
    """The python source this deployment ticks on. Raises; never None.

    A python deployment is already python. A block deployment compiles
    through dqengine.codegen. There is no third answer: the IR engine that used to
    receive everything codegen could not express was deleted in Phase 3 of
    the one-engine cleanup, measured against production first -- every
    template, saved strategy and deployment snapshot compiled (31/31,
    2026-09-14).

    Why this raises rather than returning None. The caller's next statement
    writes a payload to the row. A falsy return one line above that is a
    silent path to a tick that produces nothing, and an empty position is
    how the executor is told to sell everything. A raise lands in
    tick_deployment's handler, which records tick_error and leaves the last
    good payload alone.

    The compiled code tags its resting orders with their IR rule ids
    (dqengine.runtime.identity), so the cid prefixes at the broker are unchanged.
    """
    if _resolution_of(dep) == "second":
        # Neither the engine container nor the replay carries the second-bar
        # store config (Phase 4). Deploy refuses these rows; if one exists
        # anyway, fail the tick loudly rather than step minute bars.
        raise RuntimeError("second-resolution deployments are not routable "
                           "yet (sandbox store config)")
    conn_id = getattr(dep, "broker_connection_id", None)
    if _resolution_of(dep) == "daily" and conn_id:
        # The payload carries the at-close order to the executor and holds
        # the at-open one until 09:31 (engine.daily_preview), so most
        # destinations work. The one that does not is named in one place;
        # fail the tick loudly rather than reconcile a real account against
        # a model whose orders cannot be sent. The row itself comes through
        # the store port -- the driver never reaches a table by name.
        from dqengine.live.driver import ports
        reason = daily_broker_refusal(ports.store().connection(conn_id))
        if reason:
            raise RuntimeError(reason)
    if getattr(dep, "kind", "blocks") == "python":
        return dep.code
    if not dep.ir:
        raise RuntimeError(
            f"deployment {getattr(dep, 'id', '?')} is a blocks deployment "
            f"with no strategy document — there is nothing to compile")
    key = (getattr(dep, "id", None), json.dumps(dep.ir, sort_keys=True))
    hit = _COMPILED.get(key)
    if hit is not None:
        return hit
    from dqengine import codegen
    # NOT wrapped. A CodegenUnsupported names the block the user has to
    # change, and it is the caller's job to surface it; swallowing it here
    # is what the fallback used to do, and the fallback is gone. Not cached
    # either: a refusal is cheap to recompute and caching it would outlive
    # the edit that fixes it.
    code = codegen.generate_python(
        dep.ir, margin_max=getattr(dep, "margin_max", None))
    _COMPILED[key] = code
    return code
