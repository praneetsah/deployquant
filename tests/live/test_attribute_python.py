"""A python deployment owns its symbols too.

`_deployment_universes` answers "which deployment could have TRADED this
fill". It read that set out of `d.ir`, and a python deployment has no IR at
all -- so its symbol set was empty, `attribute` never matched it, and the
fill landed `source="manual"` with a NULL deployment_id.

That is not a cosmetic gap. `build_ledger` filters on
`Execution.deployment_id == dep.id`, so a manual-bucketed fill is
INDISTINGUISHABLE from no fill; once `_capped_live_from` rolls at close+10min
the ledger returns `[]` -- "the broker confirmed nothing filled" -- and the
model drops a REAL fill. The next sweep sees `have` without `desired` and
sells the position. Same flattening the IR weight-tree gap caused, re-armed
for every python deployment.

The `universe` column is the traded set for python (main.py derives it from a
manifest pass), which is exactly the question this function asks.
"""
from datetime import date, datetime, timezone
from dqengine.live import persistence


def _row(exec_id, sym="TQQQ", side="buy", qty=100.0, px=50.0):
    return {"broker_order_id": "", "broker_exec_id": exec_id,
            "client_order_id": "", "symbol": sym, "side": side,
            "qty": qty, "price": px, "fees": 0.0,
            "filled_at": datetime(2026, 1, 5, 15, 0, 0, tzinfo=timezone.utc),
            "order_level_avg": False}


def _seed_python(pg, owner_id, universe=("TQQQ",)):
    """A python deployment: kind='python', ir NULL, code set, universe column
    populated -- exactly what main.py's python deploy path writes."""
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c1", user_id=owner_id, broker="webull"))
        s.add(persistence.Deployment(
            id="dpy", user_id=owner_id, name="PY", kind="python",
            ir=None, code="pass", universe=list(universe),
            status="running", broker_connection_id="c1",
            cash_initial=1000.0, start_date=date(2026, 9, 1)))
        s.commit()


def test_a_python_deployment_has_a_universe_for_attribution(pg, owner_id):
    from dqengine.live import executions as ex
    _seed_python(pg, owner_id)
    with pg() as s:
        assert ex._deployment_universes(s, "c1")["dpy"] == {"TQQQ"}


def test_a_python_fill_is_attributed_to_its_deployment(pg, owner_id):
    """The end-to-end consequence: without this the row is `manual`, and a
    manual row is read downstream as a confirmed no-fill."""
    from dqengine.live import executions as ex
    _seed_python(pg, owner_id)
    with pg() as s:
        assert ex.store(s, "c1", [_row("e1")]) == 1
        s.commit()
    with pg() as s:
        e = s.query(persistence.Execution).one()
        assert e.deployment_id == "dpy"
        assert e.source != "manual"


def test_an_ir_deployment_still_uses_its_tradeable_symbols(pg, owner_id):
    """Regression guard on the narrower set: a deployment whose IR only READS
    a symbol inside an expression is not a holder of it. The python fallback
    must not widen this back to collect_ir_symbols."""
    from dqengine.live import executions as ex
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c2", user_id=owner_id, broker="webull"))
        s.add(persistence.Deployment(
            id="dir", user_id=owner_id, name="IR",
            ir={"universe": {"static": ["SPY"]},
                "rules": [{"id": "r", "trigger": {"type": "session_open"},
                           "when": {"gt": [{"ind": "rsi", "window": 14,
                                            "symbol": "TQQQ"}, 50]},
                           "action": {"type": "market_order", "side": "buy",
                                      "size": {"pct_equity": 1.0}}}]},
            status="running", broker_connection_id="c2",
            cash_initial=1000.0, start_date=date(2026, 9, 1)))
        s.commit()
    with pg() as s:
        # TQQQ is only gated on, never traded -- not a holder
        assert ex._deployment_universes(s, "c2")["dir"] == {"SPY"}
