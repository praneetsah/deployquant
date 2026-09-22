"""The rows one deployment ticks on, and the broker-execution ledger the
engine is handed.

This is the `DeploymentStore` port (`dqengine.live.driver.ports`): one
transaction object per tick over one row, and one method that answers "what
did the broker actually fill for this deployment". Everything it reads is
in `dqengine.live.persistence`, so a host with a Postgres and nothing else
can install it as it stands.

Two invariants are the whole reason the port has a named implementation
rather than four callables. A transaction that ends without a decision
leaves the row UNTOUCHED -- that is how a runner outage ends, with no error
recorded and the last good payload kept. And `ledger()` RAISES on failure:
`None` is reserved for "this connection is not enforcing", which makes the
engine keep its own model fills, and a failure that borrowed that meaning
would silently demote a real account to modelled fills.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone

from dqengine.runtime.core import collect_ir_symbols
from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill

from dqengine.live import persistence
from dqengine.live.persistence import Deployment
from dqengine.live.driver import deployment
# the timezone constant only; the driver FUNCTION is reached through its
# module, so a test that patches the real home patches what runs here
from dqengine.live.driver.deployment import ET


class _DeploymentTx:
    """One tick's scope over one row. Both commit methods are the two endings
    tick_deployment has always had; leaving without either leaves the row
    untouched, which is what a sandbox outage must do (hazard H2)."""

    def __init__(self, session, dep_id: str):
        self._s = session
        self.dep = session.get(persistence.Deployment, dep_id)
        self._events = None

    @property
    def events(self) -> list:
        # read on first use: a stopped or missing row is answered before the
        # driver looks at the events, and paid for no query then either
        if self._events is None:
            self._events = (
                (self._s.query(persistence.SleeveEvent)
                 .filter(persistence.SleeveEvent.deployment_id == self.dep.id)
                 .order_by(persistence.SleeveEvent.effective_date).all())
                if self.dep is not None else [])
        return self._events

    def commit_payload(self, out: dict) -> None:
        dep = self.dep
        dep.stats = out["stats"]
        dep.equity = out["equity"]
        # already merged by the driver (I7): the keys the replay never emits,
        # position["execution"] above all, survive a tick
        dep.position = out["position"]
        dep.fills = out["fills"]
        dep.journal = out["journal"]
        dep.tick_error = None
        dep.last_tick = datetime.now(timezone.utc)
        self._s.commit()

    def commit_error(self, text: str) -> None:
        self.dep.tick_error = text
        self.dep.last_tick = datetime.now(timezone.utc)
        self._s.commit()


class SqlDeploymentStore:
    """Rows from Postgres."""

    @contextlib.contextmanager
    def open(self, dep_id: str):
        with persistence.SessionLocal() as s:
            yield _DeploymentTx(s, dep_id)

    def ledger(self, dep):
        """UNCAPPED broker truth, or None when the connection is not
        `enforce`. Nothing is caught: a build that fails must raise into the
        tick handler (hazard H6)."""
        with persistence.SessionLocal() as s:
            return build_ledger(s, dep)

    def connection(self, conn_id: str):
        """One brokerage connection row, or None when it is gone. Read in
        its own short session: the driver asks on a daily deployment's tick
        and at deploy, and both want the row as it is now, not as the tick's
        transaction found it. `expire_on_commit=False` keeps the two columns
        the driver reads (`broker`, `execution_truth`) loaded after the
        session closes."""
        from dqengine.live.persistence import BrokerConnection
        with persistence.SessionLocal() as s:
            return s.get(BrokerConnection, conn_id)

def build_ledger(session, dep: Deployment):
    """The deployment's broker-execution ledger, or None when this
    connection is not in `enforce`.

    This is the gate that keeps the whole execution-truth feature dormant
    until an operator turns it on: in `observe` the ledger is polled and
    displayed (see the executor's write-back) but must never reach
    `Backtester(ledger=)` and touch accounting."""
    from dqengine.live.persistence import BrokerConnection, Execution
    if not dep.broker_connection_id:
        return None
    conn = session.get(BrokerConnection, dep.broker_connection_id)
    if conn is None or (conn.execution_truth or "off") != "enforce":
        return None
    rows = (session.query(Execution)
            .filter(Execution.deployment_id == dep.id)
            .order_by(Execution.filled_at).all())
    fills = []
    for r in rows:
        et = r.filled_at.astimezone(ET)
        fills.append(LedgerFill(
            day=et.date(),
            time_ms=(et.hour * 3600 + et.minute * 60 + et.second) * 1000,
            symbol=r.symbol, qty=int(round(r.signed_qty)), price=float(r.price),
            fees=float(r.fees or 0.0), rule_tag=r.rule_tag,
            # carried so take() can group every execution of one order into
            # one fill (a partial fill arrives as several rows, spec §4)
            broker_order_id=r.broker_order_id))
    # unknown_from=today: unknown-ness only ever describes what we cannot
    # SEE YET, so it demotes absence on recent days only. Settled history
    # (yesterday's confirmed fill, its cost basis, the order levels derived
    # from it) is immune to a poll hiccup or a resting order's pendency --
    # the flap that churns orders when it is not.
    return ExecutionLedger(fills, unknown=_unknown_symbols(session, dep),
                           reconciled_from=dep.reconciled_from,
                           unknown_from=datetime.now(ET).date())


def _unknown_symbols(session, dep: Deployment) -> set:
    """Symbols whose fill state we cannot currently vouch for: the last
    sweep's execution poll failed, or an order submitted today has no
    confirming execution yet. Unknown != no fill (spec §6a) -- this is
    where "a broker outage must not read as nothing filled" is enforced.
    Conflating the two is exactly how an outage becomes a duplicate live
    position: the sleeve would conclude it never traded, the replay still
    wants the position, and the executor buys it again."""
    from sqlalchemy import and_, false, func, or_
    from dqengine.live.persistence import BrokerOrder, Execution
    pos = dep.position or {}
    if ((pos.get("execution") or {}).get("executions") or {}).get("error"):
        # every symbol the strategy can actually trade, not just
        # universe.static -- weight-tree `asset` nodes select symbols
        # outside it (the same reason the replay unions the universe with
        # currently-held symbols, and the reason collect_ir_symbols exists
        # at all). A rotation strategy holding a weight-tree-selected asset
        # must not slip through here uncovered.
        # Question answered here: "which symbols' fill state can we not
        # vouch for" -- over-breadth is correct, so this stays
        # collect_ir_symbols (not collect_tradeable_symbols).
        # a python deployment has no IR: its universe column IS the set
        if not isinstance(dep.ir, dict):
            return {str(u).upper() for u in deployment._dep_universe(dep)}
        return {s.upper() for s in collect_ir_symbols(dep.ir)}
    today_et = datetime.now(ET).date()
    start = datetime.combine(today_et, datetime.min.time(), tzinfo=ET)
    # Market deltas are recorded with deployment_id=None (the executor's
    # market-delta submit), so keying only on `deployment_id == dep.id`
    # left the exact order type this feature exists for with no unknown
    # coverage at all. A market order submitted at sweep N is not yet in the
    # ledger when tick N+1 runs (the fleet poll ticks deployments BEFORE it
    # syncs): take() would find no row on a reconciled day, return [] --
    # confirmed no fill -- the sleeve would stay flat, and sweep N+1,
    # computing want=0 against a real long position, would SELL it at
    # market. Union in this connection's unattributed orders, restricted to
    # the symbols this strategy can actually trade so another deployment's
    # (or the account holder's own) order cannot freeze this one.
    # Same question as above ("can't vouch for" set): collect_ir_symbols,
    # not collect_tradeable_symbols -- keep the two call sites unified.
    try:
        if dep.kind == "python":
            # a python strategy declares its universe nowhere but its code;
            # the column IS the expansion, so there is nothing to collect
            raise ValueError("python deployment")
        ir_symbols = {s.upper() for s in collect_ir_symbols(dep.ir)}
    except Exception:
        # Same guard as the executions module's -- an IR that no longer
        # expands (an old doc, a bad edit) must not raise out of
        # build_ledger and abort the tick. Degrade to universe.static, the
        # behaviour that shipped, when it is still readable.
        ir_symbols = {str(u).upper() for u in deployment._dep_universe(dep)}
    orders = (session.query(BrokerOrder)
            .filter(or_(BrokerOrder.deployment_id == dep.id,
                        and_(BrokerOrder.deployment_id.is_(None),
                             BrokerOrder.connection_id ==
                             dep.broker_connection_id,
                             func.upper(BrokerOrder.symbol).in_(ir_symbols)
                             if ir_symbols else false())),
                    BrokerOrder.action == "submit",
                    BrokerOrder.created_at >= start,
                    BrokerOrder.status.notin_(
                        ["filled", "canceled", "rejected", "expired"]))
            .all())
    if not orders:
        return set()
    # BrokerOrder rows are insert-only: `status` is the synchronous
    # submit-time acknowledgment from the adapter (accepted/new/pending_new)
    # and nothing ever advances it afterwards (the executor's
    # broker_order_row and its sweep never issue an UPDATE). Treating that
    # stale status as the only resolution signal would keep a symbol traded
    # today "unknown" for the rest of the trading day even after a
    # confirming Execution lands -- defeating the feature on exactly the day
    # it matters (a fill shown at the model's price on the day it executed
    # is the bug this exists to fix). Resolve against the ledger instead: an
    # order stops being "unresolved" once a matching Execution row exists,
    # matched on broker_order_id and falling back to client_order_id.
    order_ids = {o.broker_order_id for o in orders if o.broker_order_id}
    cids = {o.client_order_id for o in orders if o.client_order_id}
    resolved_order_ids, resolved_cids = set(), set()
    # A cancel row resolves its order too: a canceled ticket will never get
    # an execution, and treating it as forever-in-flight keeps its symbol
    # unknowable for the rest of the day -- one canceled resting sell then
    # holds the displayed basis at the model price until midnight.
    # A cancel REQUEST is not proof of cancellation, but the sweep only
    # records `cancel` after the adapter accepted it, and the executions
    # poll continues regardless -- if the order filled in the race window,
    # its execution lands and presence wins anyway.
    if order_ids or cids:
        for boid, cid in (session.query(BrokerOrder.broker_order_id,
                                        BrokerOrder.client_order_id)
                          .filter(BrokerOrder.connection_id ==
                                 dep.broker_connection_id,
                                  BrokerOrder.action == "cancel",
                                  or_(BrokerOrder.broker_order_id.in_(
                                          order_ids) if order_ids
                                      else false(),
                                      BrokerOrder.client_order_id.in_(
                                          cids) if cids else false()))
                          .all()):
            if boid:
                resolved_order_ids.add(boid)
            if cid:
                resolved_cids.add(cid)
    if order_ids or cids:
        conds = []
        if order_ids:
            conds.append(Execution.broker_order_id.in_(order_ids))
        if cids:
            conds.append(Execution.client_order_id.in_(cids))
        for boid, cid in (session.query(Execution.broker_order_id,
                                        Execution.client_order_id)
                          .filter(Execution.connection_id ==
                                 dep.broker_connection_id,
                                  or_(*conds)).all()):
            if boid:
                resolved_order_ids.add(boid)
            if cid:
                resolved_cids.add(cid)
    # uppercased like the poll-error branch above: ExecutionLedger normalizes
    # what it is handed, but an unnormalized member here would still be a
    # symbol nobody ever asks about -- i.e. silently no coverage.
    return {(o.symbol or "").upper() for o in orders
            if not ((o.broker_order_id and
                    o.broker_order_id in resolved_order_ids) or
                   (o.client_order_id and
                    o.client_order_id in resolved_cids))}
