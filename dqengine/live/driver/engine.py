"""Live replay for python-code deployments.

The live replay, and since Phase 3 of the one-engine cleanup the only one:
every running deployment ticks through here, blocks (compiled by
dqengine.codegen) and python alike. `tick` routes and supplies the broker
ledger; the payload shape returned here is what the executor consumes and
nothing else — a divergence here is a divergence in what reaches the broker.

Two refusals worth naming, because both failure modes look like a flat
sleeve and a flat sleeve is an instruction to the executor to LIQUIDATE:
a sandbox error raises, and a result without a position block raises. The
caller (tick_deployment) freezes the deployment on either, keeping the last
good payload.

Resting orders are projected into the executor's existing channels by
project_orders: an order that REDUCES exposure is an exit (open_orders), one
that increases it is an entry (entry_orders). close_orders carries the
at-close orders a DAILY deployment has resting, and is empty at every other
resolution — a minute strategy's market_on_close ticket is still refused at
deploy (capabilities.PYTHON_UNTRANSMITTED), because nothing gives it a
moment to be placed in. daily_preview below is where the timing comes from.
"""
import math
import os
import time
from datetime import date, datetime

from dqengine.live import determinism
from dqengine.sandbox import pyrunner
from dqengine.live.driver.deployment import (
    _dep_resolution, _dep_universe, sleeve_equity_now)
from dqengine.live.driver import ports
from dqengine.runtime.identity import intent_id

LIVE_TICK_TIMEOUT_S = 25        # well inside the 60s tick cadence

# A live tick is a full sandboxed replay. Measured 2026-09-05 on a 6-month
# single-symbol minute strategy:
#
#   cold container (PYRUN_WARM_POOL=0)   11-12 s
#   pooled warm container                 0.36 s
#
# The simulation was never the cost — container spawn plus the numpy/pandas
# import is, and the warm pool already solves it. So live python REQUIRES
# PYRUN_WARM_POOL >= 1 in prod: without it a real multi-symbol deployment
# blows LIVE_TICK_TIMEOUT_S and every tick fails. This threshold makes that
# visible in the logs rather than silent.
SLOW_TICK_WARN_S = 3.0


# Same pad the backtest job uses and the IR live path used for its history
# backfill. The python engine's
# warm-up -- PyBacktester's and the Allocator's -- reads the sessions BEFORE
# start_date from the sandbox store, and a sandbox exported from start_date
# alone has none of them: indicators start cold, `best` ranks on nothing,
# and the executor trades real shares to match a model the strategy never
# had. Deterministic, so the fingerprint never fires.
WARMUP_CAL_DAYS = 420


def _tick_symbols(dep) -> list:
    """Every symbol the replay can TOUCH, not just universe.static.

    For blocks that is collect_ir_symbols: weight-tree `asset` nodes select
    symbols outside `static`, and the Allocator now feeds the union -- so
    the sandbox must hold live bars for the union too, or those assets rank
    on stale prices. For python the manifest-derived universe column is
    already the traded set."""
    if getattr(dep, "kind", "blocks") == "blocks" and dep.ir:
        try:
            from dqengine.runtime.core import collect_ir_symbols
            return sorted({str(s).upper() for s in collect_ir_symbols(dep.ir)})
        except Exception:                       # noqa: BLE001
            pass
    return [str(s).upper() for s in _dep_universe(dep)]


# (dep_id, day) -> monotonic time of the last export. One tick can reach the
# export three times (warm path, replay fallback, nightly audit replay); the
# feed does not change inside a tick, so the second and third are pure cost
# -- on a 29-symbol universe, a zip per symbol each time.
_EXPORTED: dict = {}
EXPORT_DEDUPE_S = 5.0


_HISTORY_DONE: dict = {}


def _live_rows(dep, day) -> dict:
    """Today's bars to push, straight from the store (~2 ms). Never fatal:
    an empty dict pushes nothing and the engine steps nothing."""
    try:
        syms = _tick_symbols(dep)
        return ports.bars().live_rows(syms, day) if syms else {}
    except Exception as e:                          # noqa: BLE001
        print(f"[live] live rows failed for dep={dep.id}: {e}", flush=True)
        return {}


def _export_bars_for(dep, end, collect: dict | None = None,
                     force: bool = False) -> None:
    """Put the deployment's bars where the sandbox can read them.

    The sandbox has no DB and no network: it reads /pydata through a plain
    DataStore. Nothing on the tick path used to write there, so a live
    replay ended at whatever day some BACKTEST last exported -- the payload
    was stale, and the executor traded real shares to match it.

    Two exports, because they answer different questions. The history export
    is the backtest view (curated zips over adjusted history, total-return
    adjusted) and is idempotent per symbol-day, so it costs nothing once
    warm. The live export then overlays the feed's raw-price days on top, in
    CompositeStore's precedence, and rewrites today unconditionally because
    today's bars accrue through the session.

    Never fatal: a failure here means the replay runs on the bars already
    exported, which is the behaviour that shipped. It is logged loudly
    because a SILENT stale replay is the failure this function exists to
    prevent -- and the caller's determinism check would not see it, since a
    replay that cannot see new data reproduces itself perfectly.
    """
    key = (getattr(dep, "id", None), end)
    last = _EXPORTED.get(key)
    # `force`: the warm-tick paths. The engine was just pushed rows read
    # fresh from the store; a zip that skipped the write (two ticks within
    # the dedupe window -- the forced close-60s tick and the 15:58 bar
    # event land ~0.3 s apart every day) would let a same-tick replay
    # decide the before-close bar without the bar the engine saw.
    if not force and last is not None and time.monotonic() - last < EXPORT_DEDUPE_S:
        return
    _EXPORTED[key] = time.monotonic()
    try:
        syms = _tick_symbols(dep)
        if not syms:
            return
        from datetime import timedelta
        fetch_start = dep.start_date - timedelta(days=WARMUP_CAL_DAYS)
        # The history pieces (Alpaca backfill check, hist_bars overlay) are
        # idempotent per day and cost ~7 ms of DB reads: once per
        # (deployment, day), not per bar. The live overlay below runs every
        # call because today's bars accrue.
        hkey = (getattr(dep, "id", None), end)
        if _HISTORY_DONE.get(hkey) is None:
            # backfill the ADJUSTED history the warm-up needs, then export
            # the backtest view over it. The once-per-day key is set ONLY on
            # a clean backfill: a failed one is retried on the next call, as
            # it always was, or a rebuild later that day would warm on a
            # hole all day.
            if ports.bars().export_history(syms, fetch_start, end):
                _HISTORY_DONE[hkey] = time.monotonic()
        ports.bars().export_live(syms, fetch_start, end, collect=collect)
        if _dep_resolution(dep) == "daily":
            # A daily deployment reads the DAILY zips as its bars. They are
            # written by backtest jobs and nothing else, so without this the
            # replay ends at whatever day some backtest last exported --
            # which for a curated symbol is the day the curated file ends.
            # The port carries those files past that day with rows derived
            # from the minute tree above, and the derivation skips a session
            # still in progress.
            ports.bars().export_daily(syms)
    except Exception as e:                          # noqa: BLE001
        print(f"[live] bar export failed for dep={dep.id}: {e} — the replay "
              f"will run on whatever is already exported", flush=True)


def _now_et():
    """Injectable clock for the warm tick (tests freeze it)."""
    from dqengine.live.driver.deployment import ET
    return datetime.now(ET)


_QUOTE_FAIL_LOGGED: dict = {}


def _usable_last(last) -> bool:
    """A usable quote is an int/float (not bool) that is finite and > 0 --
    the same predicate the streamer publishes by and the engine's prime()
    prices by, so no layer can pass a value the next one would misuse."""
    return (isinstance(last, (int, float)) and not isinstance(last, bool)
            and math.isfinite(last) and last > 0)


def _quote_prices(dep) -> dict:
    """The streamer's realtime snapshot ({SYM: {"last", "at_ms"}}, Redis
    key quotes:last) filtered to the symbols this deployment can touch,
    dropping entries whose `last` is not a finite positive number -- a zero
    or negative mark would make set_holdings a silent no-op (price <= 0
    returns None) and collapse total_portfolio_value for every other symbol.
    {} on ANY failure -- no bus, no key, bad JSON: the engine then primes
    off the prices it already holds, so a dead quote publisher costs
    latency (today's timing), never a wrong or missing order. Logged at
    most once per 5 min per deployment."""
    try:
        import json
        from dqengine.live import bus as bus_mod
        if bus_mod.BUS is None:
            return {}
        raw = bus_mod.BUS.get("quotes:last")
        if not raw:
            return {}
        snap = json.loads(raw)
        syms = set(_tick_symbols(dep))
        return {s: v for s, v in snap.items()
                if s in syms and isinstance(v, dict) and _usable_last(v.get("last"))}
    except Exception as e:                          # noqa: BLE001
        now = time.time()
        if now - _QUOTE_FAIL_LOGGED.get(dep.id, 0) > 300:
            _QUOTE_FAIL_LOGGED[dep.id] = now
            print(f"[warm-py] quote snapshot unavailable dep={dep.id}: {e!r} "
                  f"-- priming off held prices", flush=True)
        return {}


def _max_stale_ms(prices: dict, now_epoch_ms: int):
    """Staleness is the DRIVER's measurement (wall clock minus the oldest
    at_ms among the symbols sent), never the engine's -- the engine has no
    wall clock of its own to trust. None when no at_ms was available."""
    # Numeric at_ms only: this runs AFTER the engine advanced and primed, so
    # a raise here would drop a snapshot in which the fire already placed
    # orders. A malformed at_ms is skipped, never fatal.
    ats = [v.get("at_ms") for v in prices.values()
           if isinstance(v.get("at_ms"), (int, float))]
    return max(now_epoch_ms - int(a) for a in ats) if ats else None


def _stalest(prices: dict, now_epoch_ms: int):
    """(symbol, ms) of the oldest print among the symbols sent, or None --
    the number `max_stale_ms` reports is useless without the name: on the
    58-symbol switcher it read 5.3 h on 2026-09-18 because ONE thin ETF had
    not printed since 10:41, and nothing said which."""
    best = None
    for sym, v in prices.items():
        a = v.get("at_ms")
        if isinstance(a, (int, float)):
            age = now_epoch_ms - int(a)
            if best is None or age > best[1]:
                best = (sym, age)
    return best


# --- deferred zip export (worker path) --------------------------------------
# The post-decision export (export_live_bars: one DB query over weeks of rows
# plus one zip per symbol) took ~1 s on the 58-symbol switcher and sat
# BETWEEN the decision and the intent publish: the wall-clock fire primed at
# 15:59:00.1 and the order reached the venue at 15:59:02 (2026-09-18). A
# worker process owns one deployment; it registers it here, the tick then
# records what it owes instead of exporting, and the worker flushes AFTER the
# intent is on the bus. Callers that never flush (a fleet poll loop, an
# operator-triggered tick) do not register and keep the inline export, so a
# later replay never reads a stale zip. The exception path in
# warm_tick_python exports inline regardless: THAT tick's replay needs the
# zip now.
_DEFER_EXPORT_FOR: set = set()
_PENDING_EXPORT: dict = {}           # dep_id -> the date owed


def defer_exports_for(dep_id: str) -> None:
    _DEFER_EXPORT_FOR.add(dep_id)


def undefer_exports_for(dep_id: str) -> None:
    _DEFER_EXPORT_FOR.discard(dep_id)
    _PENDING_EXPORT.pop(dep_id, None)


def exports_deferred(dep_id: str) -> bool:
    return dep_id in _DEFER_EXPORT_FOR


def pending_export(dep_id: str):
    return _PENDING_EXPORT.get(dep_id)


def flush_deferred_export(dep_id: str, dep=None) -> None:
    """Write the zips the last acting tick owed; a no-op when none. `dep` may
    be passed by a caller already holding the row, otherwise it is loaded.
    Never fatal: _export_bars_for logs its own failures, and a failure here
    must not take the worker loop down -- the next acting tick owes again."""
    day = _PENDING_EXPORT.pop(dep_id, None)
    if day is None:
        return
    try:
        if dep is not None:
            _export_bars_for(dep, day, force=True)
            return
        with ports.store().open(dep_id) as tx:
            if tx.dep is not None:
                _export_bars_for(tx.dep, day, force=True)
    except Exception as e:                          # noqa: BLE001
        print(f"[warm-py] deferred export failed dep={dep_id}: {e!r}", flush=True)


# Daily deployments have no warm engine (they tick on the replay path), so
# their next fire is recorded from the replay's result instead. Same shape,
# same meaning, read by the same next_fire_ms below -- the worker's
# precision wake and its fire_due need no change.
_DAILY_FIRE: dict = {}


def next_fire_ms(dep_id: str):
    """The engine's next scheduled fire for this deployment (ms since
    midnight ET), as reported by its last tick; None when nothing is left
    to fire today. The worker's precision wake targets this."""
    entry = _WARM_PY.get(dep_id)
    if entry is not None:
        return entry.get("next_fire_ms")
    return _DAILY_FIRE.get(dep_id)


def _today_et() -> date:
    """The trading day, in the market's timezone. Server-local date.today()
    on a UTC host rolls to tomorrow at 20:00 ET; a replay `end` and a
    history fingerprint keyed on that disagree with the ET session the bars
    and the ledger are keyed on, and the fingerprint mismatch that produces
    is a PERMANENT freeze -- history_fp is only rewritten on success."""
    from dqengine.live.driver.deployment import ET
    return datetime.now(ET).date()


class RunnerUnavailable(RuntimeError):
    """The sandbox service is not answering — infrastructure, not the user's
    strategy. Most often the ~3-4 minute window after an engine deploy, when
    the sandbox service rebuilds its sandbox image and /run returns 503
    (dqengine/sandbox/service.py). The deployment must NOT be marked errored for this:
    nothing is wrong with the strategy, the tick simply did not happen."""


def _ledger_payload(ledger) -> dict | None:
    """An ExecutionLedger as plain JSON for the sandbox.

    User code runs in a container with no DB and no network, so broker truth
    cannot be handed over as an object — it crosses as rows and is rebuilt
    on the far side (dqengine/runtime/sandbox_entry.py) into the SAME ExecutionLedger
    the IR engine uses, so the three-valued semantics are identical.

    A LiveCappedLedger proxies everything else by __getattr__, so reading
    `_fills`/`unknown`/`reconciled_from` off it reaches the inner ledger while
    `live_from` is its own — which is exactly the capped view we want to
    reproduce, and why `live_from` is carried explicitly.
    """
    if ledger is None:
        return None
    live_from = getattr(ledger, "live_from", None)
    return {
        "fills": [{"day": f.day.isoformat(), "ms": int(f.time_ms),
                   "sym": f.symbol, "qty": int(f.qty), "px": float(f.price),
                   "fees": float(f.fees), "rule_tag": f.rule_tag,
                   "broker_order_id": f.broker_order_id}
                  for f in ledger._fills],
        "unknown": sorted(ledger.unknown),
        "reconciled_from": (ledger.reconciled_from.isoformat()
                            if ledger.reconciled_from else None),
        "unknown_from": (ledger.unknown_from.isoformat()
                         if getattr(ledger, "unknown_from", None) else None),
        "live_from": live_from.isoformat() if live_from else None,
    }


def _cash_events(events) -> list:
    """Deposits in the IR engine's cfg.cash_events shape: [(iso_day, amt)].
    Both replays are built from THIS list, so a sleeve that received cash
    after it started sizes identically on both engines."""
    return [(e.effective_date.isoformat(), float(e.amount))
            for e in (events or []) if getattr(e, "kind", None) == "deposit"]


def _run_replay(dep, events, ledger=None, code=None) -> dict:
    """Sandboxed replay from start_date to now. User code NEVER runs in this
    process."""
    end = dep.paused_at or _today_et()
    _export_bars_for(dep, end)          # deduped per (dep, day) within a tick
    t0 = time.monotonic()
    out = _dispatch(dep, end, ledger, code, events=events)
    took = time.monotonic() - t0
    if took > SLOW_TICK_WARN_S:
        pooled = int(os.environ.get("PYRUN_WARM_POOL", "0"))
        print(f"[live] slow python tick dep={dep.id} {took:.1f}s"
              + ("" if pooled else " — PYRUN_WARM_POOL is 0, so every tick "
                                  "pays ~10s of container start"),
              flush=True)
    return out


def _dispatch(dep, end, ledger=None, code=None, events=None) -> dict:
    # `code` is what this deployment ticks on: its own source, or the IR
    # compiled to python. Never read off dep here.
    cfg = {
        "mode": "full",
        "start": dep.start_date.isoformat(),
        "end": end.isoformat(),
        "cash": dep.cash_initial,
        "cash_events": _cash_events(events),
        "project_calendar": True,
        "resolution": _dep_resolution(dep),
        "bar_ms": 1000 if _dep_resolution(dep) == "second" else 60_000,
        # None outside `enforce` — the engine then keeps its own model fills,
        # which is the pre-execution-truth behaviour.
        "ledger": _ledger_payload(ledger),
    }
    if _dep_resolution(dep) == "daily":
        # The clock a daily run needs. A daily session is one bar and it
        # lands at the close, so between 09:30 and close+60s the day exists
        # and its bar does not, and nothing in the run could otherwise tell
        # an open session from a settled one. Added HERE and only here, so a
        # minute or second deployment's config is what it always was.
        cfg["live_today"] = end.isoformat()
        cfg["live_now_ms"] = _live_now_ms(end)
    return pyrunner.run(code if code is not None else dep.code, cfg,
                        timeout_s=LIVE_TICK_TIMEOUT_S)


def _live_now_ms(end: date) -> int:
    """Milliseconds since midnight ET, for the day the replay ends on.

    A paused deployment replays a day that is over; its clock is the end of
    that day, so the run treats it as settled rather than as a session in
    progress it can never finish."""
    now = _now_et()
    if end != now.date():
        return 86_400_000
    return ((now.hour * 3600 + now.minute * 60 + now.second) * 1000
            + now.microsecond // 1000)


# open_orders speaks "limit_sell" for a plain limit so the executor's
# take-profit path is untouched; the other kinds carry their own name.
_EXIT_TYPE = {"limit": "limit_sell", "stop": "stop", "stop_limit": "stop_limit",
              "trailing_stop": "trailing_stop"}
_ENTRY_TYPE = {"limit": "limit", "stop": "stop", "stop_limit": "stop_limit",
               "trailing_stop": "trailing_stop"}

# Order types that genuinely never rest: nothing to project, and no journal
# line owed. Everything ELSE that reaches project_orders and is not in the
# maps above is a resting ticket the executor cannot carry yet, and is
# deferred loudly rather than dropped -- see project_orders.
_NEVER_RESTS = frozenset({"market"})

# The two kinds a DAILY strategy's orders become. They are not "an order the
# executor has no channel for": they are where a daily order fills, in the
# backtest and in the model, and the paper deployment is complete without
# them ever reaching a venue. The broker legs are the next commits.
_DAILY_EDGE = {"market_on_close": "at this session's close",
               "market_on_open": "at the next session's open"}


# The identity of a resting python order is defined in the ENGINE
# (dqengine.runtime.identity) because both sides must spell it identically: this
# layer turns it into the broker cid prefix and stores it on
# BrokerOrder/Execution.rule_tag, while the engine passes the same string to
# ExecutionLedger.take(), which prefers rows whose rule_tag matches. A
# divergence would let one order's fill be taken by another, or make a real
# fill read as a confirmed no-fill. Import it; never re-format it here.
_intent_id = intent_id


def project_orders(open_orders: list, holdings: list,
                   daily: bool = False) -> tuple:
    """Resting python orders -> (open_orders, entry_orders) in the payload
    vocabulary the executor already speaks.

    The split is by EXPOSURE, not by direction: an order that reduces a
    position is an exit (open_orders), one that increases it is an entry
    (entry_orders). That is the same `abs(prev + qty) - abs(prev)` predicate
    the margin check uses, and it is what makes the two channels mean
    something for a short book as well as a long one.

    Returns (exits, entries, deferred) -- `deferred` carries human lines for
    intents this layer deliberately does NOT hand the executor, so they land
    in the journal instead of disappearing.

    `daily` changes only what the journal SAYS about a market-on-close or
    market-on-open ticket. On a daily strategy those are not an order the
    executor cannot carry: they are where the order fills, in the model and
    in the backtest, and on paper nothing more is owed. Saying "the strategy
    is NOT protected by it" there would be false. The default is the minute
    wording, unchanged.
    """
    held = {h["symbol"]: int(h["qty"]) for h in holdings}
    exits, entries, deferred = [], [], []
    for o in open_orders:
        sym = (o.get("symbol") or "").upper()
        qty = int(o.get("qty") or 0)
        kind = o.get("type")
        if not sym or not qty:
            continue
        if kind not in _ENTRY_TYPE:
            if daily and kind in _DAILY_EDGE:
                deferred.append(
                    f"{sym} {qty:+d} fills {_DAILY_EDGE[kind]}, which is "
                    f"where this strategy's backtest fills it. On paper "
                    f"that is the whole trade; a brokerage account cannot "
                    f"take this order yet.")
            elif kind not in _NEVER_RESTS:
                # A RESTING ticket the executor cannot carry. Dropping it
                # silently is the worst failure available here: the engine
                # rests a protective order, the model believes the position
                # is protected, and nothing is at the broker. Under enforce
                # it is self-sustaining -- the model fills its own stop on a
                # breach, the ledger returns [] because no such broker order
                # exists, the ticket stays resting, and the executor sees
                # zero delta while the position rides the drawdown.
                deferred.append(
                    f"{sym} {kind} order is not sent to the broker — the "
                    f"executor has no channel for it yet; the strategy is "
                    f"NOT protected by it")
            continue
        order_id = o.get("order_id")
        if order_id is None:
            # No identity means no safe cid: a synthesised one would collide
            # with a real order's prefix on the next tick. Defer it loudly
            # rather than hand the executor something it cannot round-trip.
            deferred.append(f"{sym} {kind} order has no order_id — not sent")
            continue
        side = "buy" if qty > 0 else "sell"
        prev = held.get(sym, 0)
        reduces = abs(prev + qty) < abs(prev)
        rule = _intent_id(sym, order_id, o.get("tag") or "")
        trail = o.get("trail_pct")
        if kind == "trailing_stop" and not trail:
            # no trail is no level: resting it would put an order at a price
            # nobody computed
            deferred.append(f"{sym} trailing_stop has no trail percent — "
                            f"not sent")
            continue
        if reduces:
            exits.append({
                "type": _EXIT_TYPE[kind], "symbol": sym,
                "price": o.get("limit_price"), "stop": o.get("stop_price"),
                "trail_pct": trail, "rule": rule, "side": side,
                "qty": abs(qty),
            })
        else:
            entries.append({
                "type": _ENTRY_TYPE[kind], "symbol": sym, "qty": abs(qty),
                "price": o.get("limit_price"), "stop": o.get("stop_price"),
                "rule": rule, "side": side,
            })
    return exits, entries, deferred


# ------------------------------------------------ the daily payload preview
#
# A daily strategy's orders fill at two clock moments: this session's close
# and the next session's open. The MODEL fills them when the day's bar lands,
# a minute past the close, because that is when a backtest of the same day
# fills them and backtest = live is the whole point. The executor has no
# clock of its own -- reconcile() acts on whatever `holdings` say, the moment
# it is called -- so left alone it would send the close's order a minute
# AFTER the close, and the open's order eight hours early.
#
# The preview supplies that timing, and only that. It moves no fill and
# touches no model: it publishes the want a minute before the close, and
# again from 09:31 the next morning, so the order reaches the broker at the
# moment the strategy meant it to. The numbers come from the SAME replay
# result the holdings come from, so when the bar lands and the model takes
# the quantity over, the published want does not move by a share.

# Both windows in ms since midnight ET. 09:31 is one minute after the open:
# the opening print is in the book by then, and it is where every other
# deployment's morning market delta already goes out.
OPEN_PREVIEW_MS = (9 * 3600 + 31 * 60) * 1000
# A minute before the close is where the worker already forces a tick
# (worker.close_fire_due) and what the executor's near-close market
# emulation was written for.
CLOSE_PREVIEW_LEAD_MS = 60_000
# A venue that takes a market-on-close order itself gets it when the ticket
# RESTS, because the exchange stops accepting on-close orders ten minutes
# before the close (NYSE and Nasdaq, and ten minutes before the EARLY close
# on a half day). Publishing at 15:59 there would be a rejection, not an
# order.
#
# Twelve minutes leaves two minutes of margin ahead of that cutoff, and the
# window is usable: LEAN's 15.5-minute submission buffer means a
# market-on-close ticket is never born later than close-15:30, so there are
# three and a half minutes between the earliest a ticket can exist and the
# moment this closes. Past it the deployment falls back to the emulation
# timing -- a plain market order a minute before the close.
NATIVE_MOC_CUTOFF_LEAD_MS = 12 * 60_000


_NATIVE_MOC: dict = {}       # connection id -> does its venue take a MOC


def venue_takes_moc(conn_id) -> bool:
    """Does this connection's venue take a market-on-close order itself?

    Cached per connection: a connection's broker never changes, and the
    answer is a property of the adapter's `Caps`, not of the account.

    A failure to look it up answers False, which is the emulation timing --
    a market order a minute before the close. That is the conservative
    direction: a native venue treated as an emulation venue trades a
    slightly worse price, while an emulation venue treated as native would
    have the platform publish an order type the broker cannot take.
    """
    if not conn_id:
        return False
    hit = _NATIVE_MOC.get(conn_id)
    if hit is not None:
        return hit
    hit = False
    try:
        from dqengine.adapters import base, catalog
        from dqengine.live import capabilities
        conn = ports.store().connection(conn_id)
        if conn is not None:
            caps = catalog.get_adapter(conn.broker).caps
            hit = capabilities.resolve(
                caps, base.MARKET_ON_CLOSE).mode == capabilities.NATIVE
    except Exception as e:                              # noqa: BLE001
        print(f"[live] could not read the on-close capability of "
              f"connection {conn_id}: {e} — using the near-close market "
              f"emulation", flush=True)
        return False
    _NATIVE_MOC[conn_id] = hit
    return hit


def _session_close_ms(day: date):
    """The day's real close in ms since midnight ET, or None when the day is
    not a session. Every window below measures against this, so a half-day
    closes the windows at 13:00 with no special case anywhere."""
    from dqengine.runtime.core.data import close_time_ms, is_market_holiday
    if day.weekday() >= 5 or is_market_holiday(day):
        return None
    return close_time_ms(day)


class DailyEdgeCollision(RuntimeError):
    """Two at-close tickets on one symbol, pointed at a broker.

    They net into ONE broker order -- the executor's MOC channel is keyed
    per (deployment, symbol) and a second entry would be counted as pending
    without ever being sent. One order then comes back as one execution row,
    and the first ticket's take() groups every row of that broker order into
    itself: the second ticket reads as unknown, then as a confirmed no-fill,
    and the model and the account part company on a real position.

    Refused at the PAYLOAD, not in the model: the model keeps filling both
    tickets exactly as the backtest does, so the deployment freezes with its
    last good payload and the backtest still says what it always said.
    Paper deployments never reach this -- there is no executor to confuse.
    """


def daily_preview(res: dict, holdings: list, open_orders: list, now,
                  broker: bool = False, native_moc: bool = False) -> tuple:
    """(holdings, close_orders, journal lines) for a daily deployment.

    A pure function of the replay result and the clock. It holds no state
    between ticks, so a restart inside either window changes nothing and two
    ticks a second apart publish the same numbers.

    Three rules, all measured against `_session_close_ms` so early closes
    need no special case:

    * AT CLOSE -- from close-60s until the day's bar lands, the resting
      market-on-close tickets are netted per symbol into `holdings` and
      published as one `close_orders` entry per symbol. The executor
      journals the substitution and its market-delta loop sends a market
      order at about 15:59:01, which is the near-close emulation. When the
      bar lands the model holds that same quantity and the ticket is gone,
      so the want does not move: one number from 15:59 to 09:31.

      `native_moc` says the venue takes an on-close order itself. The window
      then opens the moment the ticket RESTS instead, because the exchange
      stops accepting on-close orders ten minutes before the close and a
      ticket published at 15:59 would be rejected rather than filled. Past
      NATIVE_MOC_CUTOFF_LEAD_MS the quantity still goes into `holdings` at
      close-60s but no `close_orders` entry is published, so a plain market
      order goes out and nothing is sent to a window that has shut.

    * AT OPEN -- from 09:31 on a session day, a market-on-open ticket placed
      on an EARLIER day is added to `holdings`. Its fill has happened at the
      exchange; the model books it when the bar lands at 16:01. No
      `close_orders` entry and no new order type: the ordinary market delta
      sends it, and journal.market_cid makes that idempotent.

    * HOLD THE LAST PRE-CLOSE WANT -- a fill the model booked at or after a
      session's close that the BROKER has not confirmed is held out of the
      want until 09:31 of the next session. That is exactly the quantity a
      missed 15:59 tick leaves behind: the model fills its own at-close
      ticket at 16:01, and without this the executor would read the
      difference as a delta and send a market order after hours (Alpaca
      queues it to the next open, Webull refuses it and retries at the 04:00
      pre-market window). The same rule holds back anything else the model
      settled on its own authority at the bar -- a stop it filled itself, say
      -- so nothing the market is closed for turns into an order.

      Under `enforce` a fill the executor's own 15:59 order produced is
      stamped at 15:59:0x and confirmed, so it is NOT held and the delta is
      zero. That is the whole distinction, and it is why a daily deployment
      on a broker requires `enforce`.
    """
    dl = res.get("daily_live") or {}
    run_day = date.fromisoformat(dl["today"])
    bar_applied = bool(dl.get("bar_applied"))
    today = now.date()
    now_ms = ((now.hour * 3600 + now.minute * 60 + now.second) * 1000
              + now.microsecond // 1000)
    close_ms = _session_close_ms(today)

    add: dict = {}
    close_orders: list = []
    notes: list = []

    if run_day == today and close_ms is not None and not bar_applied:
        # A native venue takes the ticket the moment it rests; the emulation
        # timing is a minute before the close. Past the venue cutoff a native
        # destination falls back to the emulation timing, and `publish` goes
        # false so the order goes out at market rather than into a window the
        # exchange has already shut.
        native_open = (native_moc
                       and now_ms < close_ms - NATIVE_MOC_CUTOFF_LEAD_MS)
        emulation_open = now_ms >= close_ms - CLOSE_PREVIEW_LEAD_MS
        publish = native_open or not native_moc
        if native_open or emulation_open:
            per_sym: dict = {}
            for o in open_orders:
                if o.get("type") != "market_on_close":
                    continue
                s = (o.get("symbol") or "").upper()
                q = int(o.get("qty") or 0)
                if not s or not q:
                    continue
                net, rules = per_sym.get(s, (0, []))
                per_sym[s] = (net + q,
                              rules + [_intent_id(s, o.get("order_id"),
                                                  o.get("tag") or "")])
            for s, (net, rules) in sorted(per_sym.items()):
                if len(rules) > 1 and broker:
                    raise DailyEdgeCollision(
                        f"{s} has {len(rules)} at-close orders resting for "
                        f"this session. They would net into ONE order at the "
                        f"broker and one of them would read as a no-fill, so "
                        f"the model and the account would disagree about a "
                        f"real position. This deployment is frozen on its "
                        f"last payload; place one at-close order per symbol "
                        f"per session, or run it on paper.")
                if net == 0:
                    continue
                add[s] = add.get(s, 0) + net
                if publish:
                    close_orders.append(
                        {"type": "market_on_close", "symbol": s, "qty": net,
                         "price": None,
                         "rule": rules[0] if len(rules) == 1 else None})
                notes.append(
                    f"{s} {net:+d} at this session's close — sent to the "
                    f"broker now" if publish else
                    f"{s} {net:+d} at this session's close — past the "
                    f"exchange's on-close cutoff, so it goes out at market "
                    f"just before the close instead")
        if now_ms >= OPEN_PREVIEW_MS:
            for o in open_orders:
                if o.get("type") != "market_on_open":
                    continue
                s = (o.get("symbol") or "").upper()
                q = int(o.get("qty") or 0)
                created = o.get("created_day")
                if not s or not q or not created:
                    continue
                if date.fromisoformat(created) >= today:
                    # placed today: it fills at TOMORROW's open, and asking
                    # the broker for it now would be a day early
                    continue
                add[s] = add.get(s, 0) + q
                notes.append(f"{s} {q:+d} at this session's open — sent to "
                             f"the broker now")

    released = close_ms is not None and now_ms >= OPEN_PREVIEW_MS
    for f in reversed(res.get("fills") or []):
        fday = date.fromisoformat(f["day"])
        if (today - fday).days > 10:
            break                       # settled long ago; nothing to hold
        if f.get("confirmed", True):
            continue
        fclose = _session_close_ms(fday)
        if fclose is None or int(f["ms"]) < fclose:
            continue
        if fday < today and released:
            continue
        s = (f.get("sym") or "").upper()
        add[s] = add.get(s, 0) - int(f["qty"])
        notes.append(
            f"{s} {int(f['qty']):+d} filled at the {fday} close in the model "
            f"and the broker has not confirmed it — held out of the target "
            f"until the next session opens, so it cannot become an "
            f"after-hours order")

    if not add:
        return holdings, close_orders, notes

    last_px = (res.get("position") or {}).get("last_prices") or {}
    out = []
    for h in holdings:
        s = (h.get("symbol") or "").upper()
        q = int(h.get("qty") or 0) + add.pop(s, 0)
        if q == 0:
            continue
        px = float(h.get("last_price") or 0.0)
        out.append({**h, "qty": q, "market_value": round(q * px, 2)})
    for s, q in sorted(add.items()):       # symbols the model does not hold
        if q == 0:
            continue
        px = float(last_px.get(s) or 0.0)
        out.append({"symbol": s, "qty": q, "last_price": round(px, 2),
                    "market_value": round(q * px, 2), "entry_price": 0.0})
    return out, close_orders, notes


def replay_python_deployment(dep, events: list, ledger=None, code=None) -> dict:
    res = _run_replay(dep, events, ledger, code)
    if "error" in res:
        err = res["error"]
        if err.get("type") == "RunnerUnavailable":
            raise RunnerUnavailable(err.get("message", "runner unreachable"))
        raise RuntimeError(f"{err.get('type', 'Error')}: "
                           f"{err.get('message', '')}")
    return _payload_from_result(dep, events, res)


def _payload_from_result(dep, events: list, res: dict) -> dict:
    """The deployment payload from an engine result -- shared VERBATIM by
    the replay path and the warm path, so the two cannot drift in shape.
    The executor consumes payload shape and nothing else; a divergence here
    is a divergence in what reaches the broker."""
    if "position" not in res:
        # an older sandbox image, mid-rollout. Reporting flat would liquidate
        # the account; refusing the tick freezes it instead.
        raise RuntimeError("sandbox result carries no position block — "
                           "the runner image is older than this API")

    daily = _dep_resolution(dep) == "daily"
    if daily and (res.get("resolution") != "daily"
                  or res.get("daily_live") is None):
        # The engine says what it ran as. A daily deployment whose run comes
        # back without the daily-live block ran on the OLD rule -- its checks
        # saw a close that had not printed and its orders filled at the wrong
        # edge. That is an image older than this API, and the right answer is
        # to refuse the tick, loudly, rather than write those fills down.
        raise RuntimeError(
            "this daily deployment's run did not come back as a daily live "
            "run — the runner image is older than this API. Refusing the "
            "tick rather than trading on the previous rule.")

    pos = res["position"]
    universe = _dep_universe(dep)
    sym = universe[0] if universe else ""
    holdings = pos.get("holdings") or []
    primary = next((h for h in holdings if h["symbol"] == sym), None)
    qty = int(primary["qty"]) if primary else 0
    last = (pos.get("last_prices") or {}).get(sym, 0.0)

    exits, entries, deferred = project_orders(
        pos.get("open_orders") or [], holdings, daily=daily)
    close_orders: list = []
    if daily:
        # The engine's next scheduled fire, for the worker's precision wake.
        # A daily deployment has no warm engine to report it, so the replay
        # records it here and next_fire_ms serves it; the worker needs no
        # change at all.
        _DAILY_FIRE[dep.id] = (res.get("daily_live") or {}).get("next_fire_ms")
        # The timing the executor has no clock for. Computed from THIS
        # result, so the quantity the preview publishes and the quantity the
        # model takes over when the bar lands are the same number -- there is
        # no moment where the want goes back and forth. project_orders runs
        # FIRST, on the model's own holdings, so an exit is still classified
        # against the position the strategy actually holds.
        conn_id = getattr(dep, "broker_connection_id", None)
        holdings, close_orders, previewed = daily_preview(
            res, holdings, pos.get("open_orders") or [], _now_et(),
            broker=bool(conn_id), native_moc=venue_takes_moc(conn_id))
        deferred += previewed
        primary = next((h for h in holdings if h["symbol"] == sym), None)
        qty = int(primary["qty"]) if primary else 0

    # Settled history can never legitimately change. If it moved since the
    # last tick the strategy is not reproducing itself, and the executor
    # would read the difference as a position change and trade on it.
    today = dep.paused_at or _today_et()
    fp = determinism.history_fingerprint(res["equity_days"], res["equity"],
                                         today)
    pos_prev = dep.position or {}
    prior = pos_prev.get("history_fp")
    prior_day = pos_prev.get("history_fp_day")
    if prior and prior_day:
        # Compare at the HORIZON the prior was taken at. The fingerprint
        # hashes sessions strictly before its `today`; the first tick after
        # midnight would otherwise hash one more settled day than the stored
        # value and freeze every python-path deployment on its second
        # morning. The settled history through prior_day must not have
        # moved -- that is the determinism check.
        check = determinism.history_fingerprint(res["equity_days"], res["equity"],
                                                prior_day)
        if check != prior:
            raise RuntimeError(
                "this strategy did not reproduce itself: its settled history "
                "changed between ticks. Live trading is frozen until the "
                "strategy is deterministic — see the python docs on what makes "
                "a run repeatable.")
    # (a prior without a day is from before this field existed: it cannot
    # be verified at its horizon; it is replaced below and verified from
    # the next tick on)
    today_iso = today.isoformat() if hasattr(today, "isoformat") else str(today)

    contributed = dep.cash_initial + sum(
        e.amount for e in events if e.kind == "deposit")
    # sleeve_equity_now compares a deposit's effective_date against the last
    # equity day, so the days must be real dates — the sandbox hands them
    # over as ISO strings and a str/date comparison raises the moment a
    # deposit lands after the last replayed session.
    equity_days = [date.fromisoformat(d) for d in res["equity_days"]]
    equity_now = sleeve_equity_now(res["equity"], equity_days,
                                   dep.cash_initial, events)
    return {
        "stats": {
            **res["stats"],
            "end_equity": round(equity_now, 2),
            "contributed": round(contributed, 2),
            "pnl": round(equity_now - contributed, 2),
            "return_pct": round((equity_now / contributed - 1) * 100, 2)
            if contributed > 0 else 0.0,
        },
        "equity": {"days": res["equity_days"],
                   "values": [round(v, 2) for v in res["equity"]]},
        "position": {
            "symbol": sym, "qty": qty, "last_price": last,
            "market_value": round(qty * last, 2),
            "cash": pos.get("cash", 0.0),
            "entry_price": primary["entry_price"] if primary else 0.0,
            "holdings": holdings,
            "last_prices": pos.get("last_prices") or {},
            "open_orders": exits,
            "entry_orders": entries,
            # At-close orders, in the shape the executor's close-order
            # channel reads. Non-empty only inside a DAILY deployment's
            # window (daily_preview): that is the one place the python
            # runtime has a resting on-close ticket and a moment to place it
            # in. Empty everywhere else, minute and second included.
            "close_orders": close_orders,
            # per-tick signal; the warm path overwrites it with the engine's primed dict when a wall-clock fire ran this tick (spec 2026-09-18 §5.1)
            "primed": None,
            "history_fp": fp,
            "history_fp_day": today_iso,
        },
        "fills": res["fills"][-200:],
        "journal": ([{"log": line} for line in (res.get("logs") or [])[-300:]]
                    + [{"log": d} for d in deferred]),
    }


# ------------------------------------------------------------ warm path
# One long-lived engine container per deployment (pyrunner engine sessions),
# stepped by the bars that closed since the last call instead of replayed
# from start_date every tick. The sub-second path. Any anomaly discards the
# engine and serves the replay THIS tick; the next tick rebuilds. Dying is
# cheap; diverging is not. (The IR warm engine held the same posture until
# Phase 3 deleted it; this is the surviving copy.)

_WARM_PY: dict[str, dict] = {}       # dep_id -> {deposits, at, day_open, last_completed, fresh}

# Zero, deliberately. A 2s lag was the first defence against the feed's
# in-progress minute being stepped as closed -- but on the worker path,
# which owns every prod deployment and wakes only on the NEXT bar event,
# "now - 2s" excluded the bar that had just closed, and every bar was
# stepped a full minute late. The partial-minute hazard is now removed at
# its source (the bar source drops the current minute's row), so
# the engine's clock is the wall clock.
FEED_LAG_MS = 0


def warm_health() -> dict:
    """Python warm engines in this process, and the transport each is on
    (from the entry cached at build). A prod entry on 'file' with
    PYRUN_ENGINE_TRANSPORT=socket is the fallback the svc logged as ERROR."""
    by: dict = {}
    for e in list(_WARM_PY.values()):
        t = str(e.get("transport") or "unknown")
        by[t] = by.get(t, 0) + 1
    return {"engines": len(_WARM_PY), "transports": by}


def _drop_engine(dep_id: str, reason: str) -> None:
    _WARM_PY.pop(dep_id, None)
    try:
        pyrunner.engine_stop(dep_id)
    except Exception:                                   # noqa: BLE001
        pass
    print(f"[warm-py] dropped dep={dep_id}: {reason}", flush=True)


def _engine_cfg(dep, ledger, code, end, events=None) -> dict:
    """run.json for the engine container. Same fields _dispatch sends for a
    replay, so a warm engine and a replay of the same deployment are built
    from the same inputs.

    `end` is NOT optional: without it the run keeps the algorithm's own
    set_end_date -- for compiled blocks a placeholder (2026-06-09) -- and
    the warm engine warmed to THAT and stepped the live day on a months-
    stale state. The replay path always passed it; the warm path did not."""
    return {
        "mode": "serve",
        "start": dep.start_date.isoformat(),
        "end": end.isoformat(),
        "cash": dep.cash_initial,
        "cash_events": _cash_events(events),
        # live: project the calendar past the data horizon (IR's
        # cfg.project_calendar) so week-boundary rules see the real week
        "project_calendar": True,
        "resolution": _dep_resolution(dep),
        "bar_ms": 1000 if _dep_resolution(dep) == "second" else 60_000,
        "ledger": _ledger_payload(ledger),
        # a compiled block strategy may use the reserved `ir:` tag prefix
        "generated": getattr(dep, "kind", "blocks") == "blocks",
    }


def _bars_to_push(entry: dict) -> dict:
    """Today's bars for the engine, per symbol: only the rows after what the
    ENGINE reported holding (entry["pushed"], from its last tick response),
    plus the day's first-bar and count anchors it reconciles against. The
    engine, not the driver, is the authority on what was pushed: a tick
    that failed after the engine consumed its bars drops the engine, and a
    tick the engine declined (open grace) reports {} so the whole day is
    pushed again."""
    from dqengine.runtime.core.data import REG_OPEN_MS, REG_CLOSE_MS
    pushed = entry.get("pushed") or {}
    out = {}
    for sym, rows in (entry.get("rows") or {}).items():
        # the anchors must count what the ENGINE will count: it masks to
        # the regular session, so a pre-market or 16:00 row a future writer
        # stores must not make every push fail reconciliation
        rows = [r for r in rows if REG_OPEN_MS <= int(r[0]) < REG_CLOSE_MS]
        if len(rows) < 2:
            continue
        last = pushed.get(sym, -1)
        out[sym] = {"first_ms": int(rows[0][0]), "n_total": len(rows),
                    "rows": [r for r in rows if r[0] > last]}
    return out


def _since(full: dict | None) -> dict | None:
    if not full:
        return None
    c = full["counts"]
    return {"fills": c["fills"], "equity": c["equity"], "logs": list(c["logs"])}


def _merge_snapshot(full: dict | None, delta: dict) -> dict:
    """Fold a delta snapshot into the driver's full one. The engine's
    `counts` are the totals it holds; a merge that does not land on them
    is a driver/engine disagreement and raises (the caller drops the
    engine) rather than building a payload from a list with a hole in it."""
    if full is None:
        if delta.get("logs") is None:
            raise RuntimeError("first snapshot from an engine was a delta")
        return delta
    out = dict(delta)
    for k in ("fills", "equity_days", "equity", "flows"):
        out[k] = full[k] + delta[k]
    # order statuses mutate in place, so a delta cannot carry them; the
    # merged snapshot has no orders list (nothing on the live path reads it)
    out["orders"] = None
    parts = {k: full["log_parts"][k] + delta["log_parts"][k]
             for k in ("algo", "refused", "missed")}
    out["log_parts"] = parts
    out["logs"] = parts["algo"] + parts["refused"] + parts["missed"]
    c = delta["counts"]
    got = {"fills": len(out["fills"]), "equity": len(out["equity"]),
           "logs": [len(parts["algo"]), len(parts["refused"]), len(parts["missed"])]}
    want = {"fills": c["fills"], "equity": c["equity"], "logs": list(c["logs"])}
    if got != want:
        raise RuntimeError(f"snapshot merge does not reconcile: have {got}, "
                           f"engine holds {want}")
    return out


def warm_tick_python(dep, events: list, ledger=None, code=None) -> dict:
    """Advance (building if needed) the deployment's engine and return a
    payload. Raises on ANY anomaly -- the caller drops the engine and serves
    the replay this tick.

    `ledger` rides along with every advance so live-day fills consult
    today's broker truth. Fills already applied unconfirmed are not revised
    in place; new broker rows trigger the stale marker (mark_warm_stale ->
    the shared bus key, consumed here by _consume_stale_marker) and the
    engine is rebuilt."""
    from dqengine.live.driver.deployment import (
        SessionCalendar, _consume_stale_marker, _deposit_count)
    from datetime import timedelta

    marker = _consume_stale_marker(dep.id)
    if marker is not None:
        _drop_engine(dep.id, f"stale marker: {marker}")
        raise RuntimeError(f"stale marker: {marker}")

    now_et = _now_et()
    today = now_et.date()
    # Millisecond-precise: the tick's now_ms_et is what the engine primes
    # a wall-clock schedule fire against, so a second-truncated clock would
    # place every fire up to 999 ms early. want_roll below still compares
    # against a whole-second boundary (close+60s); its semantics are unchanged.
    now_ms = ((now_et.hour * 3600 + now_et.minute * 60 + now_et.second) * 1000
              + now_et.microsecond // 1000)

    entry = _WARM_PY.get(dep.id)
    if entry is not None and entry.get("last_completed") is not None \
            and entry["last_completed"] < today:
        _WARM_PY.pop(dep.id, None)                 # yesterday's; rebuild
        entry = None
    if entry is not None and entry["deposits"] != _deposit_count(events):
        _drop_engine(dep.id, "cash event since warm-up")
        raise RuntimeError("cash event since warm-up; history changed")
    if entry is not None and entry.get("last_completed") == today:
        # Post-roll: today is done for this engine. Advancing it would be an
        # anomaly (a completed day) and would kill it -- and the driver would
        # then rebuild it on the next tick, kill it again on the one after,
        # all evening. Serve the last payload instead; the day is settled.
        res = entry.get("snapshot")
        if res is None:
            res = pyrunner.engine_call(dep.id, "snapshot", {}, timeout_s=30)
            entry["snapshot"] = res
        payload = _payload_from_result(dep, events, res)
        payload["position"]["engine"] = "warm"
        if entry.get("audit") is None:
            # the roll's audit did not run (the sandbox service was down at
            # 16:01, say). It is NOT labelled ok by default: retry it here,
            # every post-roll tick, until it has actually run.
            return _audit_and_retire(dep, events, entry, payload, ledger, code)
        payload["position"]["warm_audit"] = entry["audit"]
        return payload

    if entry is not None and entry.get("transport") not in ("socket", "inproc") \
            and not pyrunner.engine_alive(dep.id):
        # (on the socket/in-process transports the tick call IS the
        # liveness check -- it fails loud on EOF/timeout; an alive probe
        # per bar is an HTTP call + docker inspect in prod)
        # (a retired post-roll engine never reaches here: handled above)
        _WARM_PY.pop(dep.id, None)
        entry = None

    fresh_build = False
    if entry is None:
        src = code if code is not None else dep.code
        t0 = time.monotonic()
        # today's bars must be on disk BEFORE the engine opens the session
        # (it reads the store once at open; every later bar is pushed)
        rows: dict = {}
        _export_bars_for(dep, today, collect=rows)
        started = pyrunner.engine_start(dep.id, src,
                                        _engine_cfg(dep, ledger, src, today,
                                                    events=events)) or {}
        pyrunner.engine_call(dep.id, "warm",
                             {"through": (today - timedelta(days=1)).isoformat()},
                             timeout_s=120)
        entry = _WARM_PY[dep.id] = {"deposits": _deposit_count(events),
                                    "at": time.time(), "day_open": False,
                                    "last_completed": None,
                                    "rows": rows, "pushed": {}, "full": None,
                                    "transport": str(started.get("transport") or "unknown")}
        fresh_build = True
        print(f"[warm-py] warmed dep={dep.id} in {time.monotonic() - t0:.2f}s",
              flush=True)
    else:
        # the feed persisted its closed bars: read today's rows straight
        # from the store (~2 ms) to PUSH. The zip (~15 ms: history checks
        # + BarDay query + deflate) is written AFTER the tick -- only the
        # replay fallback and the roll audit read it, never the engine.
        rows = _live_rows(dep, today)
        if rows:
            entry["rows"] = rows

    # The ledger is NOT sent here. It was set at build; a fresh ledger per
    # advance would lose the rows already taken, and a later sell could
    # re-take the earlier buy's row and book itself as a buy. New broker
    # rows reach the engine through the stale marker -> rebuild, exactly as
    # on the IR path.
    #
    # The first advance after a build steps the WHOLE day so far -- on a
    # wide universe that can exceed a normal call budget, and an advance
    # that times out kills the engine, which then dies on every rebuild.
    # One round trip per bar: advance, roll if past close+60s and a session
    # is open (the engine knows, the driver's day_open is a mirror), then
    # snapshot. Was three RPCs (advance / end_session / snapshot).
    # `prices` rides on EVERY tick: the engine primes a due schedule fire off
    # them after advancing. {} (no bus / no key / bad JSON) is passed as-is --
    # the engine then primes unpriced, off the prices it already holds.
    prices = _quote_prices(dep)
    want_roll = now_ms >= SessionCalendar.close_time_ms(today) + 60_000
    t_rpc = time.monotonic()
    try:
        tick = pyrunner.engine_call(
            dep.id, "tick",
            {"now_ms_et": max(0, now_ms - FEED_LAG_MS), "today": today.isoformat(),
             "roll": want_roll, "bars": _bars_to_push(entry),
             "since": _since(entry.get("full")), "prices": prices},
            timeout_s=120 if fresh_build else 30)
    except Exception:
        # the caller will serve a REPLAY this tick, and the replay reads
        # the zip: make sure today's is current before it runs
        if not fresh_build:
            _export_bars_for(dep, today, force=True)
            _PENDING_EXPORT.pop(dep.id, None)       # nothing owed: it just happened
        raise
    # A day with no session (feed outage, holiday the calendar missed) still
    # ENDS at close+60s: mark it completed so tomorrow rebuilds rather than
    # this engine skipping a calendar day a replay would have carried.
    rolled = bool(tick.get("rolled")) or (want_roll and not tick.get("session_open"))
    if not tick.get("session_open") and not rolled:
        # The engine has not opened today's session (its first bar cannot be
        # stepped until a second one arrives), so its snapshot is YESTERDAY's
        # close: yesterday's position with yesterday's resting orders. That is
        # the truth until something acts on today -- and a lie once something
        # has. A replay tick steps a lone first bar; if one already recorded
        # fills for today, publishing this snapshot over it moves the
        # deployment BACKWARDS: the executor is told a take-profit that filled
        # at the open is missing from the broker and re-places it below the
        # market, where it fills at once -- the position is sold seconds after
        # it was bought. Refuse; the caller serves the replay this tick and
        # the engine is rebuilt on the next, by when the session can open.
        today_iso = today.isoformat()
        if any((f or {}).get("day") == today_iso
               for f in (getattr(dep, "fills", None) or [])):
            _drop_engine(dep.id, "has not opened today's session, and the "
                                 "stored payload already has today's fills")
            raise RuntimeError(
                "warm engine has not opened today's session but the stored "
                "payload already has fills for today -- serving the replay")
    entry["day_open"] = bool(tick.get("session_open"))
    entry["pushed"] = tick.get("pushed") or {}
    # Read AFTER the roll inside the engine: a roll=True tick reports None,
    # which is right -- no fires remain that day. The worker's precision
    # wake (next_fire_ms below) targets this.
    entry["next_fire_ms"] = tick.get("next_fire_ms")
    primed = tick.get("primed")
    if primed is not None:
        # the epoch reference is taken AFTER the RPC returned, not from the
        # now_et captured at tick start: the engine call is the tick's
        # long pole and staleness measured from before it under-reports
        primed = {**primed,
                  "max_stale_ms": _max_stale_ms(prices, int(_now_et().timestamp() * 1000))}
    if rolled:
        entry["last_completed"] = today
    res = entry["full"] = _merge_snapshot(entry.get("full"), tick["snapshot"])
    payload = _payload_from_result(dep, events, res)
    if not fresh_build:
        if dep.id in _DEFER_EXPORT_FOR:
            # worker path: publish first, export after. "~8 ms" was the
            # single-symbol sleeve; on the 58-symbol switcher this export is
            # ~1 s of DB queries and deflates, and it sat between the primed
            # decision and the intent publish (2026-09-18: fire at
            # 15:59:00.1, venue at 15:59:02). The worker flushes it once the
            # intent is on the bus (flush_deferred_export).
            _PENDING_EXPORT[dep.id] = today
        else:
            # after the order is decided (still before the caller commits):
            # keep the zip fresh for a later fallback and for the roll audit
            # below, which replays
            _export_bars_for(dep, today, force=True)
    # the audit trail names the path that produced these targets
    payload["position"]["engine"] = f"warm:{entry.get('transport', 'unknown')}"
    # the ONLY payload addition for the wall-clock fire: dict or None
    payload["position"]["primed"] = primed
    if primed is not None:
        # logged only once the primed snapshot has REACHED the payload:
        # _payload_from_result / _export_bars_for above can raise and hand
        # this tick to the replay, and an operator must never read
        # "primed fire" followed by "falling back to replay"
        stalest = _stalest(prices, int(_now_et().timestamp() * 1000))
        print(f"[warm-py] primed fire dep={dep.id} "
              f"events={[f.get('name') for f in primed.get('fired', [])]} "
              f"priced={primed.get('priced')} unpriced={primed.get('unpriced')} "
              f"stale_ms={primed.get('max_stale_ms')}"
              + (f" stalest={stalest[0]}@{stalest[1]}ms" if stalest else "")
              + f" rpc_to_payload_ms={round((time.monotonic() - t_rpc) * 1000)}",
              flush=True)

    if rolled:
        entry["snapshot"] = res
        return _audit_and_retire(dep, events, entry, payload, ledger, code)
    return payload


def _audit_ledger(dep):
    """The deployment's full broker ledger (None outside enforce) for the
    roll audit -- the same port the tick's capped ledger comes from, asked
    for the uncapped one."""
    return ports.store().ledger(dep)


def _audit_and_retire(dep, events, entry, payload, ledger, code) -> dict:
    """The nightly audit, the convergence backstop: a model fill whose order
    the broker never filled is held unconfirmed by the warm engine and
    dropped by a fresh replay -- the only case stale-on-ingest cannot
    signal. A mismatch discards the engine and serves the replay.

    Three outcomes, none silent:
      * RunnerUnavailable: propagate (the row is left alone), `audit` stays
        None, and the next post-roll tick retries -- a skipped audit is
        never labelled ok;
      * any other replay failure: recorded as a FAILED audit on the entry
        and the engine retired; no rebuild loop, because the day is settled
        and the engine is not consulted again today;
      * ran: ok or mismatch, recorded, engine retired either way.
    """
    try:
        # UNCAPPED: the point of the audit is to drop a model fill the
        # broker never made, and a ledger capped to today's live rows
        # cannot see that
        replay = replay_python_deployment(dep, events, ledger=_audit_ledger(dep),
                                          code=code)
    except RunnerUnavailable:
        raise
    except Exception as e:                              # noqa: BLE001
        entry["audit"] = {"ok": False, "error": f"audit replay failed: "
                                                f"{type(e).__name__}: {e}"[:300]}
        payload["position"]["warm_audit"] = entry["audit"]
        pyrunner.engine_stop(dep.id)
        return payload
    diff = _audit_diff(payload, replay)
    if diff:
        entry["audit"] = {"ok": False, "diff": diff[:500]}
        _drop_engine(dep.id, f"audit mismatch: {diff}")
        replay["position"]["warm_audit"] = entry["audit"]
        return replay
    entry["audit"] = {"ok": True}
    payload["position"]["warm_audit"] = entry["audit"]
    # The day is settled and audited: retire the container now rather than
    # let it idle until DQENGINE_SERVE_IDLE_S. Tomorrow's first tick rebuilds from
    # scratch, which also folds overnight broker truth. The registry entry
    # stays so post-roll ticks serve this payload.
    pyrunner.engine_stop(dep.id)
    return payload


def _audit_diff(warm: dict, replay: dict):
    """Where a warm payload and a fresh replay disagree, or None."""
    a, b = warm.get("position") or {}, replay.get("position") or {}
    ha = {h["symbol"]: h["qty"] for h in a.get("holdings") or []}
    hb = {h["symbol"]: h["qty"] for h in b.get("holdings") or []}
    if ha != hb:
        return f"holdings warm={ha} replay={hb}"
    if abs(float(a.get("cash") or 0) - float(b.get("cash") or 0)) > 0.01:
        return f"cash warm={a.get('cash')} replay={b.get('cash')}"
    ea, eb = warm.get("equity") or {}, replay.get("equity") or {}
    if (ea.get("days") or [])[-5:] != (eb.get("days") or [])[-5:]:
        return "equity days differ"
    va, vb = (ea.get("values") or [])[-5:], (eb.get("values") or [])[-5:]
    if len(va) != len(vb) or any(abs(x - y) > 0.01 for x, y in zip(va, vb)):
        return f"equity tail warm={va} replay={vb}"
    return None
