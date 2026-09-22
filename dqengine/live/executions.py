"""The broker-execution ledger: poll what the broker actually did, attribute
it to a deployment, store it idempotently.

Lives apart from the executor deliberately — that module is already 1500+
lines of order reconciliation, and the ledger is a separate concern with a
separate failure mode (missing data is UNKNOWN, never "no fill").
"""
from datetime import timedelta

from dqengine.live import persistence                      # noqa: E402
from dqengine.live.persistence import (BrokerOrder,        # noqa: E402
                                       Execution,
                                       managed_deployments)
from dqengine.runtime.core import collect_tradeable_symbols      # noqa: E402

# Re-ask slightly before the newest stored fill: brokers can report an
# execution after one with a later timestamp (out-of-order settlement), and
# a hard `> last` cursor would drop it forever. Duplicates are free — the
# (connection_id, broker_exec_id) uniqueness makes re-reading a no-op.
OVERLAP_S = 300


def _deployment_universes(session, conn_id) -> dict:
    """{deployment_id: {SYMBOLS}} for deployments on this connection that the
    executor still manages.

    `running` and `paused` both count for step 3 of the precedence, which is
    why the scope comes from `managed_deployments` — the same call the
    sweep and the OMS make, so attribution cannot drift apart from what the
    executor actually manages. Paused means "stop opening new positions",
    not "this sleeve no longer owns its shares" — the executor still
    maintains a paused deployment's resting exits, so its fills (including
    ones backfilled with no matching broker_orders row) must still land in
    its ledger. `stopped` is excluded: that one genuinely is not managed.

    I4: the symbol set is `collect_ir_symbols`, the same one the driver's
    unknown-symbol set uses — NOT `ir.universe.static`. Weight-tree
    `asset` nodes select symbols outside `static`, and a fill on one of
    those landed here as `source="manual"` with a NULL deployment_id.
    Under `enforce` a manual-bucketed fill is INDISTINGUISHABLE from no
    fill, since `build_ledger` filters on `Execution.deployment_id ==
    dep.id` — so a rotation/switcher strategy's real fill read as "the
    broker did not fill this", the same flattening as C1. If these two
    symbol sets ever drift apart again the gap reopens; keep them identical.

    UPDATE: that symbol set is now `collect_tradeable_symbols`, not
    `collect_ir_symbols` — this function answers "which deployment could
    have TRADED this fill", so a deployment whose IR only reads a symbol
    inside an expression (an RSI gate, say) must not count as a holder of
    it. Two deployments sharing a connection where one genuinely trades a
    symbol and the other only gates on it were both showing up as
    candidates, forcing a real fill to `manual`. The unknown-symbol set
    answers a different question ("which symbols' fill state can we not
    currently vouch for") where the wider `collect_ir_symbols` set is
    still correct — do not unify the two call sites.
    """
    from dqengine.live.driver.deployment import _dep_universe

    out = {}
    for d in managed_deployments(session, conn_id):
        try:
            # A python deployment has NO IR (the model's `ir` is nullable,
            # and the python deploy path leaves it NULL), so
            # collect_tradeable_symbols
            # would answer {} and the deployment would hold nothing. Its
            # `universe` column IS its traded set — the host derives it from
            # a manifest pass over the code — which is precisely the question
            # this function asks. Without this a real python fill lands
            # `manual`, and a manual row is indistinguishable from no fill
            # under enforce: the sleeve flattens at close+10min and the next
            # sweep sells a position the account actually holds.
            syms = collect_tradeable_symbols(d.ir) if d.ir else _dep_universe(d)
        except Exception:
            # An IR that no longer expands (an old doc, a bad edit) must not
            # abort this connection's whole poll — every OTHER deployment's
            # fills would go unattributed, which under `enforce` reads as
            # "the broker did not fill them". Degrade to the universe column
            # (which falls back to universe.static itself) when it is readable.
            syms = _dep_universe(d)
        out[d.id] = {str(u).upper() for u in syms}
    return out


def attribute(session, conn_id: str, rows: list) -> list:
    """Resolve each normalized row to a deployment. Never split, never
    guess past a unique match — an unattributable fill is `manual`, which
    the reconciliation panel surfaces, rather than silently landing in
    somebody's sleeve.

    Deliberately does NOT catch per-row shape errors. Rows arrive already
    validated by dqengine.adapters.base.normalize_execution, so a KeyError or
    similar here is an internal pipeline bug, not vendor noise — unlike an
    adapter's raw broker payload, where skipping one bad row to save the
    batch is correct. Swallowing it here would make a real fill silently
    vanish from the ledger once the OVERLAP_S poll window passes it by
    (only stored rows advance the cursor), leaving a sleeve quietly short
    shares it actually holds. Let it raise — poll()'s caller already treats
    an exception as `unknown`, which is the loud, correct behavior.
    """
    orders = (session.query(BrokerOrder)
              .filter(BrokerOrder.connection_id == conn_id,
                      BrokerOrder.deployment_id.isnot(None)).all())
    by_order_id = {o.broker_order_id: o for o in orders if o.broker_order_id}
    by_cid = {o.client_order_id: o for o in orders if o.client_order_id}
    universes = _deployment_universes(session, conn_id)

    out = []
    for r in rows:
        sym = r["symbol"]
        match = ((by_order_id.get(r["broker_order_id"])
                 if r["broker_order_id"] else None)
                 or (by_cid.get(r["client_order_id"])
                     if r["client_order_id"] else None))
        dep_id = rule_tag = None
        if match is not None:
            dep_id = match.deployment_id
            rule_tag = match.rule_tag
        else:
            holders = [d for d, syms in universes.items() if sym in syms]
            if len(holders) == 1:
                dep_id = holders[0]
        sign = 1.0 if r["side"] == "buy" else -1.0
        out.append(Execution(
            connection_id=conn_id, deployment_id=dep_id,
            broker_order_id=r["broker_order_id"] or None,
            broker_exec_id=r["broker_exec_id"],
            client_order_id=r["client_order_id"] or None,
            symbol=sym, signed_qty=sign * r["qty"], price=r["price"],
            fees=r["fees"], filled_at=r["filled_at"], rule_tag=rule_tag,
            source="broker" if dep_id else "manual",
            order_level_avg=r["order_level_avg"]))
    return out


def store(session, conn_id: str, rows: list, out: list | None = None) -> int:
    """Attribute and insert, skipping executions already recorded. Returns
    how many were new; when `out` is given, the newly inserted Execution
    objects are appended to it (poll uses this to note fills into the
    live book)."""
    if not rows:
        return 0
    seen = {e for (e,) in session.query(Execution.broker_exec_id)
            .filter(Execution.connection_id == conn_id,
                    Execution.broker_exec_id.in_(
                        [r["broker_exec_id"] for r in rows])).all()}
    inserted = 0
    stale_deps = set()
    for e in attribute(session, conn_id, rows):
        if e.broker_exec_id in seen:
            continue
        seen.add(e.broker_exec_id)      # guard duplicates WITHIN one batch
        session.add(e)
        inserted += 1
        if out is not None:
            out.append(e)
        # write-ahead ledger: the fill is the affirmative evidence that
        # closes the journal row (same transaction as the Execution
        # insert). Loud, never fatal -- the ledger row must land.
        try:
            from dqengine.live import journal
            journal.fold_execution(session, conn_id, e)
        except Exception as jex:
            print(f"[journal] fold failed {conn_id}: {jex!r}", flush=True)
        if e.deployment_id:
            stale_deps.add(e.deployment_id)
    if stale_deps:
        _signal_warm_stale(session, conn_id, stale_deps)
    return inserted


def _signal_warm_stale(session, conn_id: str, dep_ids: set) -> None:
    """Broker truth just landed for these deployments: under `enforce`
    their warm engines were stepped on unconfirmed model fills and must
    rebuild from the ledger (spec: 2026-08-25-warm-enforce-design.md §3.3).
    Under observe/off the ledger never touches accounting, so no signal.
    A worst-case false alarm (rows that fail to commit after this) costs
    one harmless rebuild; a lost signal is caught by the nightly audit."""
    from dqengine.live.persistence import BrokerConnection
    conn = session.get(BrokerConnection, conn_id)
    if conn is None or (conn.execution_truth or "off") != "enforce":
        return
    from dqengine.live.driver import deployment as driver
    for dep_id in dep_ids:
        driver.mark_warm_stale(dep_id, "broker executions ingested")


def poll(adapter, creds: dict, conn_id: str) -> tuple:
    """Fetch and store this connection's newest executions. Returns
    `(new_rows, skipped_rows)`. Raises nothing broker-specific — callers
    treat any exception as `unknown`, not as `no fills`.

    I6: `skipped` is how many raw rows the adapter could not parse. Skipping
    them is correct at the adapter layer, but a skipped row's absence on a
    reconciled day would otherwise be read by `ExecutionLedger.take()` as an
    affirmative no-fill — inverting the rule that unparsed is UNKNOWN. The
    caller routes a non-zero count into the same poll-error signal a broker
    outage uses, which widens the unknown set.

    Uses `persistence.SessionLocal` (looked up on the module at call time,
    not imported by name) so tests that re-point the factory at a
    test-database one are honored here too.
    """
    with persistence.SessionLocal() as s:
        last = (s.query(Execution.filled_at)
                .filter(Execution.connection_id == conn_id)
                .order_by(Execution.filled_at.desc()).first())
    since = (last[0] - timedelta(seconds=OVERLAP_S)) if last else None
    fetched = adapter.executions(creds, since=since)
    # Read the skip count off the ADAPTER'S OWN return, before the `or []`
    # below: an ExecutionBatch that parsed nothing is empty and therefore
    # falsy, and substituting a plain list for it would drop the count in
    # exactly the case that most needs to say UNKNOWN. A plain list from an
    # adapter that does not report skips reads as zero.
    skipped = int(getattr(fetched, "skipped", 0) or 0)
    rows = fetched or []
    new_rows: list = []
    with persistence.SessionLocal() as s:
        n = store(s, conn_id, rows, out=new_rows)
        # tuples built BEFORE commit: expire-on-commit sessions detach
        fills = [(e.broker_order_id, e.client_order_id, e.symbol,
                  float(e.signed_qty)) for e in new_rows]
        s.commit()
    if fills:
        # affirmative evidence into the live book: moves positions between
        # audits and clears idless-ack pending entries (2026-08-31 fix).
        # Bookkeeping only -- a failure here must never fail the poll.
        try:
            from dqengine.live.book import book_for
            book_for(conn_id).note_fills(fills)
        except Exception as e:
            print(f"[executions] book fill-note failed {conn_id}: {e!r}",
                  flush=True)
    return n, skipped
