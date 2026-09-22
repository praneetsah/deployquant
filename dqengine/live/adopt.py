"""Adopting a broker account: its history into the ledger, and the mode
ladder that lets its fills drive the accounting.

Both halves were operator scripts on the hosted platform, run by hand
against one connection at a time. They are here because a self-hosted
install has no operator and needs the same two steps, and because the
command that walks them (`dqengine adopt`) must not be a second
implementation of either: what it does to an account is what the platform
has been doing to its own.

The order is not arbitrary. `plan` reads the broker's history and says what
would change; `apply` stores it and moves each deployment's
`reconciled_from` boundary; only then does the ladder go `observe` and then
`enforce`, and `enforce` is the mode in which the broker's own execution
rows -- not the replay's model fills -- decide what the account holds.
"""
import sys

from dqengine.adapters import catalog as registry
from dqengine.live import executions as ex
from dqengine.live import persistence
from dqengine.live import vault
from dqengine.live.driver.deployment import ET
from dqengine.live.persistence import BrokerConnection, Deployment

# --------------------------------------------------------- the mode ladder

MODES = ("off", "observe", "enforce")
# off <-> observe <-> enforce. Skipping observe is refused: `enforce` drives
# accounting from the ledger, and observe is how you find out the ledger is
# right first.
ALLOWED = {("off", "observe"), ("observe", "enforce"),
           ("observe", "off"), ("enforce", "observe")}


def set_mode(conn_id: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    with persistence.SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None:
            raise ValueError(f"no connection {conn_id}")
        current = conn.execution_truth or "off"
        if current != mode and (current, mode) not in ALLOWED:
            raise ValueError(
                f"{current} -> {mode} is not an allowed transition; "
                f"go through 'observe'")
        conn.execution_truth = mode
        s.commit()
        return mode


# ----------------------------------------------------- the history backfill


class SkippedRowsError(Exception):
    """apply() refuses to run: the adapter could not parse every raw row in
    this fetch. Writing anyway would set `reconciled_from` to the earliest
    fetched execution, asserting the whole backfilled range is fully known
    when it is not -- a skipped row's absence would then read as a
    permanent, confirmed no-fill instead of unknown."""


def _fetch_history(adapter, creds):
    """Everything the broker will give us. `since=None` means the adapter's
    own maximum window — Alpaca activities go back years (now genuinely
    paginated to walk the full history, not just the first page), Schwab
    orders ~60 days, Webull less. Whatever is older than what comes back
    stays modeled, behind the deployment's reconciled_from boundary.

    Returns the adapter's own return value (an `ExecutionBatch` when the
    adapter reports skips, a plain list otherwise) UNCOERCED -- callers
    must read `.skipped` off it before doing `rows or []`, the same
    ordering `executions.poll` uses, so an adapter that skipped rows but
    fetched nothing else doesn't fall through `or []` and lose the count."""
    return adapter.executions(creds, since=None)


def _load(conn_id):
    with persistence.SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None or not conn.creds_encrypted:
            raise SystemExit(f"no usable connection {conn_id}")
        adapter = registry.get_adapter(conn.broker)
        creds = vault.decrypt_creds(conn.creds_encrypted)
        deps = (s.query(Deployment)
                .filter(Deployment.broker_connection_id == conn_id).all())
        return adapter, creds, [(d.id, d.start_date, dict(d.position or {}))
                                 for d in deps]


def _holdings(pos: dict) -> list:
    return pos.get("holdings") or ([pos] if pos.get("symbol") else [])


def plan(conn_id: str) -> dict:
    """Read-only: fetches the broker's history and the connection's current
    deployments, computes what a backfill WOULD change, and returns it.
    Writes nothing — no session in this function ever calls add/commit."""
    adapter, creds, deps = _load(conn_id)
    fetched = _fetch_history(adapter, creds)
    skipped = int(getattr(fetched, "skipped", 0) or 0)
    rows = fetched or []

    latest_buy_price = {}
    for r in sorted(rows, key=lambda x: x["filled_at"]):
        if r["side"] == "buy":
            latest_buy_price[r["symbol"]] = r["price"]   # last_fill entry semantics
    earliest = min((r["filled_at"] for r in rows), default=None)
    earliest_date = earliest.astimezone(ET).date() if earliest else None

    entry_price_changes = []
    reconciled_from = []
    coverage_gaps = []
    for dep_id, start_date, pos in deps:
        for h in _holdings(pos):
            sym = (h.get("symbol") or "").upper()
            old = h.get("entry_price")
            new = latest_buy_price.get(sym)
            if sym and new is not None and old is not None \
                    and abs(float(old) - float(new)) > 1e-9:
                entry_price_changes.append({
                    "deployment_id": dep_id, "symbol": sym,
                    "from": round(float(old), 4), "to": round(float(new), 4)})
        if earliest_date is not None:
            reconciled_from.append({"deployment_id": dep_id,
                                    "date": str(earliest_date)})
            if start_date is not None and start_date < earliest_date:
                # Broker history does not reach back to when this
                # deployment started — everything before earliest_date
                # stays model-priced. Say so plainly rather than implying
                # a complete backfill.
                coverage_gaps.append({
                    "deployment_id": dep_id, "start_date": str(start_date),
                    "earliest_execution": str(earliest_date)})

    return {"connection_id": conn_id, "executions_found": len(rows),
            "earliest": earliest, "entry_price_changes": entry_price_changes,
            "reconciled_from": reconciled_from, "coverage_gaps": coverage_gaps,
            "skipped": skipped}


def apply(conn_id: str) -> dict:
    """Stores the broker's history into the execution ledger and moves each
    of the connection's deployments' reconciled_from boundary to the
    earliest fetched fill. NEVER call this against a real connection without
    having first read plan()'s output — it restates entry prices that real
    resting exit orders are keyed off of.

    Refuses (raises SkippedRowsError, writes nothing) if the fetch skipped
    any raw rows -- see SkippedRowsError's docstring."""
    adapter, creds, deps = _load(conn_id)
    fetched = _fetch_history(adapter, creds)
    skipped = int(getattr(fetched, "skipped", 0) or 0)
    if skipped:
        raise SkippedRowsError(
            f"{skipped} raw row(s) could not be parsed by the adapter -- "
            "refusing to apply. Applying anyway would set reconciled_from "
            "to the earliest fetched execution, asserting the whole "
            "backfilled range is fully known, which it is not: the "
            "skipped row(s) would become a permanent phantom no-fill on a "
            "reconciled day. Re-run once the adapter parses them.")
    rows = fetched or []
    earliest = min((r["filled_at"] for r in rows), default=None)
    with persistence.SessionLocal() as s:
        stored = ex.store(s, conn_id, rows)
        if earliest is not None:
            boundary = earliest.astimezone(ET).date()
            for dep_id, _start_date, _pos in deps:
                dep = s.get(Deployment, dep_id)
                if dep is not None:
                    dep.reconciled_from = boundary
        s.commit()
    return {"stored": stored,
            "reconciled_from": str(earliest.astimezone(ET).date())
            if earliest is not None else None}


# ------------------------------------------------------------ the comparison

# The one number the command cannot decide for you: whether the shares the
# account holds are the shares the strategy thinks it holds. Everything on
# the screen is there to answer that, and the typed confirmation is where a
# person says they read it.
MISMATCH = "MISMATCH"


def raise_to_enforce(conn_id: str) -> list:
    """Climb the ladder to `enforce`, one rung at a time, and say which
    rungs were climbed.

    Every step goes through `set_mode`, so the no-skip rule is applied
    rather than worked around: an `off` connection takes two calls. A
    connection already at a rung is not stepped back down to reach it --
    walking `enforce -> observe -> enforce` would leave a real account
    momentarily not enforcing for no reason at all."""
    with persistence.SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None:
            raise ValueError(f"no connection {conn_id}")
        current = conn.execution_truth or "off"
    steps = []
    if current != "enforce":
        if current != "observe":
            steps.append("observe")
        steps.append("enforce")
    for mode in steps:
        set_mode(conn_id, mode)
    return steps


def account_label(conn_id: str) -> str:
    """What the confirmation asks to be typed back: this connection's own
    label. Not a fixed phrase -- the mistake worth catching is adopting the
    wrong account, and only the account's own name catches that."""
    with persistence.SessionLocal() as s:
        conn = s.get(BrokerConnection, conn_id)
        if conn is None:
            raise ValueError(f"no connection {conn_id}")
        return conn.label or conn.broker


def broker_view(conn_id: str, fills: int = 8) -> dict:
    """What the account says about itself right now: positions, working
    orders, and its most recent fills.

    The session refresh runs because a token-based venue answers nothing
    without it, and the refreshed credentials are deliberately NOT written
    back: this is a read-only command, and the next sweep refreshes again
    from the stored pair.
    """
    adapter, creds, _deps = _load(conn_id)
    updated = adapter.ensure_session(creds)
    if updated:
        creds = updated
    fetched = _fetch_history(adapter, creds)
    skipped = int(getattr(fetched, "skipped", 0) or 0)
    rows = sorted(fetched or [], key=lambda r: r["filled_at"], reverse=True)
    return {"positions": {str(k).upper(): float(v) for k, v
                          in (adapter.positions(creds) or {}).items()},
            "open_orders": list(adapter.open_orders(creds) or []),
            "fills": rows[:fills], "executions_found": len(rows),
            "skipped": skipped}


def replay_view(dep_id: str, refresh_bars: bool = True) -> dict:
    """What the strategy holds when it is replayed from its start date.

    The same replay a tick runs -- same code, same ledger, same bar source
    -- and nothing is committed: the transaction is left without either
    commit method, which is how the driver already leaves a row it did not
    finish. The bar refresh is on for the same reason the poll path leaves
    it on: a replay reads the bars the store has, and an install that has
    never streamed has none.
    """
    from dqengine.live.driver import deployment as driver
    from dqengine.live.driver import engine
    from dqengine.live.driver import ports
    from dqengine.runtime.core.ledger import LiveCappedLedger
    store = ports.store()
    with store.open(dep_id) as tx:
        dep = tx.dep
        if dep is None:
            raise ValueError(f"no deployment {dep_id}")
        universe = driver._dep_universe(dep)
        if refresh_bars:
            for sym in universe:
                try:
                    ports.bars().refresh(sym)
                except Exception as e:                       # noqa: BLE001
                    print(f"[adopt] bar refresh failed for {sym}: {e}",
                          flush=True)
        ledger = store.ledger(dep)
        if ledger is not None:
            ledger = LiveCappedLedger(
                ledger, live_from=driver._capped_live_from())
        out = engine.replay_python_deployment(
            dep, tx.events, ledger=ledger,
            code=driver._python_code_for(dep))
    pos = out.get("position") or {}
    held = {}
    for h in pos.get("holdings") or []:
        sym = (h.get("symbol") or "").upper()
        if sym:
            held[sym] = held.get(sym, 0.0) + float(h.get("qty") or 0.0)
    return {"holdings": held, "universe": [u.upper() for u in universe],
            "start_date": str(getattr(dep, "start_date", "")),
            "open_orders": pos.get("open_orders") or []}


def comparison(broker_pos: dict, model: dict, universe) -> list:
    """One row per symbol either side holds, or the universe names:
    (symbol, broker qty, strategy qty, note).

    A symbol outside the universe is listed because the person reading the
    screen should see the whole account, and marked as not this strategy's
    business -- the executor never trades it."""
    syms = sorted(set(broker_pos) | set(model) | {u.upper() for u in universe})
    uni = {u.upper() for u in universe}
    out = []
    for sym in syms:
        have = float(broker_pos.get(sym) or 0.0)
        want = float(model.get(sym) or 0.0)
        if not have and not want:
            continue
        if sym not in uni:
            note = "outside the universe — never traded by this strategy"
        elif abs(have - want) < 1e-9:
            note = "agrees"
        else:
            note = f"{MISMATCH}: the first sweep will refuse this symbol" \
                if not want else f"{MISMATCH}: the executor would trade " \
                                 f"{want - have:+g}"
        out.append((sym, have, want, note))
    return out


def _table(rows: list) -> str:
    head = ("SYMBOL", "AT BROKER", "STRATEGY", "")
    cells = [head] + [(s, f"{h:g}", f"{w:g}", n) for s, h, w, n in rows]
    widths = [max(len(c[i]) for c in cells) for i in range(3)]
    return "\n".join(
        "  " + "  ".join(c[i].ljust(widths[i]) for i in range(3))
        + ("  " + c[3] if c[3] else "")
        for c in cells)


def screen(rows: list, view: dict, broker: dict, preview: dict,
           label: str) -> str:
    """The comparison screen, as one string. Everything a person needs to
    decide, and nothing they have to go and look up."""
    out = [f"Adopting {label}",
           "",
           f"The strategy replays from {view['start_date']}. What it holds "
           f"at the end of that replay is what the executor will hold the "
           f"account to.",
           "",
           _table(rows) if rows else "  (both sides are flat)",
           ""]
    mismatched = [r[0] for r in rows if r[3].startswith(MISMATCH)]
    if mismatched:
        out += [f"{len(mismatched)} symbol(s) do not agree: "
                f"{', '.join(mismatched)}.",
                "A symbol the strategy holds none of is refused by the "
                "first-sync check, and that refusal stops the WHOLE sweep "
                "until the two agree. Move --start back until the replay "
                "reproduces what the account holds, and run this again.",
                ""]
    orders = broker["open_orders"]
    out.append(f"Working orders at the broker: {len(orders)}")
    for o in orders[:10]:
        out.append(f"  {o.get('symbol')} {o.get('side')} {o.get('qty')} "
                   f"{o.get('type')} {o.get('client_order_id') or ''}".rstrip())
    out.append("")
    out.append(f"Execution history the broker returned: "
               f"{broker['executions_found']} fill(s)"
               + (f", {broker['skipped']} unparsed" if broker["skipped"]
                  else ""))
    for f in broker["fills"]:
        out.append(f"  {f['filled_at']:%Y-%m-%d %H:%M}  {f['symbol']:<6} "
                   f"{f['side']:<4} {f['qty']:g} @ {f['price']:g}")
    out.append("")
    if preview["entry_price_changes"]:
        out.append("Entry prices this backfill restates (every stop and "
                   "take-profit derived from them moves on the next sync):")
        for c in preview["entry_price_changes"]:
            out.append(f"  {c['symbol']}  {c['from']} -> {c['to']}")
        out.append("")
    if preview["reconciled_from"]:
        out.append(f"Fills from {preview['reconciled_from'][0]['date']} "
                   f"onwards become broker-priced; everything before that "
                   f"stays model-priced.")
    for g in preview["coverage_gaps"]:
        out.append(f"  the broker's history starts at "
                   f"{g['earliest_execution']}, after this deployment's "
                   f"start date {g['start_date']} — the gap stays modeled")
    return "\n".join(out)


CONFIRM = ("\nThis sets the account's execution truth to `enforce`: the "
           "broker's own fills, not the replay's, decide what it holds, and "
           "`dqengine live --live` will then trade it with real money.\n"
           "Type the account label to confirm, anything else to stop.\n"
           "  {label}\n> ")

NO_TTY = ("dqengine adopt needs a terminal: it asks for a typed "
          "confirmation before it puts a real account under a strategy, and "
          "there is no --yes. Run it by hand, or use --dry-run, which prints "
          "the same screen and writes nothing.")

NO_ROWS = ("--dry-run has nothing to compare: {what}. Run `dqengine adopt` "
           "without --dry-run -- it creates the two rows and still stops at "
           "the confirmation -- or run `dqengine live` once first.")

NO_ADAPTER = ("{broker} has no usable adapter in this install ({why}). "
              "`dqengine brokers` lists the ones that are installed. "
              "Nothing was written to the ledger.")

SKIPPED = ("the broker returned {n} execution row(s) its adapter could not "
           "parse. Adopting now would record a boundary claiming the whole "
           "backfilled range is known, and a row nobody could read would "
           "become a permanent phantom no-fill on a reconciled day. Nothing "
           "was written. Try again once the adapter parses them.")


def run(algorithm: str, broker: str, *, start=None, cash=None, margin=None,
        max_order_usd=None, max_position_usd=None, dry_run: bool = False,
        ask=None, out=print, refresh_bars: bool = True,
        init: bool = True) -> int:
    """`dqengine adopt`: put an account that already holds shares under a
    strategy, in one command. Exit code: 0 adopted (or the screen printed
    under --dry-run), 1 stopped by the person, 2 refused.

    Order is the discipline the operator scripts documented, kept: plan
    before apply, and the apply after the confirmation, so a run that is
    stopped -- or refused -- has written nothing to the ledger and moved no
    boundary. `--dry-run` writes nothing at all, which is why it needs the
    two rows to exist already rather than creating them.
    """
    from dqengine.live import setup
    if ask is None:
        ask = _tty_ask
    try:
        rows = _rows(algorithm, broker, dry_run=dry_run, start=start,
                     cash=cash, margin=margin, max_order_usd=max_order_usd,
                     max_position_usd=max_position_usd, init=init)
    except setup.DeploymentRefused as e:
        out(f"refused: {e}")
        return 2
    conn_id, dep_id = rows["conn_id"], rows["dep_id"]
    # Everything from here to the confirmation is a read. A failure in any
    # of it is a refusal with the reason on one line, never a traceback:
    # this command is run by a person looking at an account.
    from dqengine.adapters.base import BrokerAuthExpired, BrokerUnavailable
    try:
        preview = plan(conn_id)
        broker_now = broker_view(conn_id)
    except (KeyError, LookupError) as e:
        out(f"refused: {NO_ADAPTER.format(broker=broker, why=e)}")
        return 2
    except (BrokerAuthExpired, BrokerUnavailable) as e:
        out(f"refused: the broker could not be read: {e}. Nothing was "
            f"written.")
        return 2
    if preview["skipped"]:
        out(f"refused: {SKIPPED.format(n=preview['skipped'])}")
        return 2
    try:
        view = replay_view(dep_id, refresh_bars=refresh_bars)
    except Exception as e:                                   # noqa: BLE001
        out(f"refused: the strategy could not be replayed, so there is "
            f"nothing to compare: {e}. Nothing was written.")
        return 2
    rows_out = comparison(broker_now["positions"], view["holdings"],
                          view["universe"])
    label = account_label(conn_id)
    out(screen(rows_out, view, broker_now, preview, label))
    if dry_run:
        out("--dry-run: nothing was written.")
        return 0
    typed = ask(CONFIRM.format(label=label))
    if typed is None:
        out(f"refused: {NO_TTY}")
        return 2
    if typed.strip() != label:
        out("stopped: that is not the account label. Nothing was written.")
        return 1
    applied = apply(conn_id)
    steps = raise_to_enforce(conn_id)
    boundary = applied["reconciled_from"] or "unchanged (no fills returned)"
    climbed = " -> ".join(steps) if steps else "already enforce"
    out(f"\nstored {applied['stored']} execution(s) into the ledger")
    out(f"reconciled_from: {boundary}")
    out(f"execution truth: {climbed}")
    out(f"\nNext: dqengine live {algorithm} --broker {broker} --live")
    return 0


def _rows(algorithm: str, broker: str, *, dry_run: bool, start, cash, margin,
          max_order_usd, max_position_usd, init: bool) -> dict:
    """The connection and the deployment this adoption is about.

    A real run creates or refreshes them through `setup.prepare`, which is
    the same call `dqengine live` makes and carries the same refusals. A
    dry run writes nothing, so it can only look up what is already there."""
    from dqengine.live import setup
    if not dry_run:
        return setup.prepare(algorithm, broker, live=False, dry_run=False,
                             cash=cash, start=start, margin=margin,
                             max_order_usd=max_order_usd,
                             max_position_usd=max_position_usd, init=init,
                             adopting=True)
    import os

    from sqlalchemy.exc import SQLAlchemyError
    name = os.path.splitext(os.path.basename(algorithm))[0]
    try:
        with persistence.SessionLocal() as s:
            conn = setup.find_connection(s, broker)
            dep = None if conn is None else next(
                (d for d in persistence.managed_deployments(s, conn.id)
                 if d.name == name), None)
    except SQLAlchemyError as e:
        # an unreachable database, or one this install has never built:
        # either way there are no rows to compare, and a dry run may not
        # create the schema to find that out
        raise setup.DeploymentRefused(
            f"could not read the database: "
            f"{str(e).splitlines()[0]}. A --dry-run adoption reads the two "
            f"rows a real run creates; it never writes them.") from None
    if conn is None:
        raise setup.DeploymentRefused(NO_ROWS.format(
            what=f"this install has no {broker} connection yet"))
    if dep is None:
        raise setup.DeploymentRefused(NO_ROWS.format(
            what=f"nothing named {name!r} runs on that connection"))
    return {"conn_id": conn.id, "dep_id": dep.id}


def _tty_ask(prompt: str):
    """The typed confirmation, or None when there is nobody to ask. A
    non-interactive caller gets the refusal, never a default answer:
    `--yes` does not exist for this command on purpose."""
    if not sys.stdin.isatty():
        return None
    return input(prompt)


def _cli(argv, prog: str = "python -m dqengine.live.adopt") -> int:
    """The CLI entry point (not `apply` itself) is where the confirmation
    gate belongs: `apply(conn_id)` stays a plain, directly-callable function
    for other code/tests, but a human typing a command line gets one more
    checkpoint before the write path runs. `plan` never needs the flag --
    it writes nothing, so there is nothing to confirm.

    `prog` is how the caller was invoked, so the hint names a command that
    exists where it is read: the hosted platform's operator script has its
    own name for this."""
    cmd, conn = argv[1], argv[2]
    if cmd == "plan":
        print(plan(conn))
        return 0
    if cmd == "apply":
        if "--yes" not in argv[3:]:
            print("Refusing to apply without --yes -- this restates entry "
                  "prices that move real resting stop/take-profit orders "
                  "at the broker on the next sync. Here is what plan() "
                  "sees (nothing has been written):")
            print(plan(conn))
            print(f"\nTo actually apply: {prog} apply {conn} --yes")
            return 1
        try:
            print(apply(conn))
        except SkippedRowsError as exc:
            print(f"Refusing to apply: {exc}")
            return 1
        return 0
    raise SystemExit(f"unknown command {cmd!r} (expected plan|apply)")


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
