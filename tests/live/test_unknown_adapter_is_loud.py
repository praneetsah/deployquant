"""A connection whose broker has no adapter installed.

The sweep cannot send anything for it, which is correct. What was wrong is
that it said nothing: `except (KeyError, LookupError): return "skipped"`
looks the same from outside as a paced skip, so a deployment created with a
broker id nothing claims ticked, published intents, and had its whole order
path quietly do nothing while `dqengine status` stayed green.

Now it costs one line in the log and one error on the deployment row every
sweep, for as long as it is true. A connection with no deployments on it is
still silent: an empty slot has nothing to report.
"""
from datetime import date

import pytest

from dqengine import brokers
from dqengine.live import executor, persistence, status, vault
from dqengine.live.book import book_for

from fakes import FakeBus

CONN = "c-noadapter"
DEP = "d-noadapter"


def _seed(pg, owner_id, *, broker="etrade", with_deployment=True):
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id=CONN, user_id=owner_id, broker=broker, status="ok",
            mode="paper",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        if with_deployment:
            s.add(persistence.Deployment(
                id=DEP, user_id=owner_id, name="D",
                start_date=date(2026, 8, 1),
                ir={"universe": {"static": ["SPY"]}}, status="running",
                broker_connection_id=CONN, cash_initial=1000.0,
                position={"holdings": []}))
        s.commit()


@pytest.fixture(autouse=True)
def _clean_gates():
    executor._SYNC_GATE.pop(CONN, None)
    executor._GATHER_CACHE.pop(CONN, None)
    yield
    executor._SYNC_GATE.pop(CONN, None)
    executor._GATHER_CACHE.pop(CONN, None)


def _sweep():
    executor._SYNC_GATE.pop(CONN, None)
    return executor.sync_broker_account(CONN)


def test_a_running_deployment_on_an_unknown_broker_is_loud(pg, owner_id,
                                                           capsys):
    _seed(pg, owner_id)
    with pytest.raises(brokers.UnknownBroker):
        from dqengine.adapters import catalog as registry
        registry.get_adapter("etrade")          # the state under test
    assert _sweep() == "skipped"
    line = capsys.readouterr().out
    assert "no usable adapter" in line and "etrade" in line
    assert CONN in line

    with pg() as s:
        dep = s.get(persistence.Deployment, DEP)
        exe = (dep.position or {}).get("execution") or {}
    assert exe["errors"], "the sweep recorded no error on the deployment"
    assert "no usable adapter" in exe["errors"][0]
    assert exe["actions"] == []
    assert exe["executions"]["error"], \
        "an unread account must not read as a clean poll"


def test_status_refuses_to_call_that_deployment_healthy(pg, owner_id):
    _seed(pg, owner_id)
    _sweep()
    bad = status.problems(status.report(bus=FakeBus(), check_lock=False))
    assert any("no usable adapter" in line for line in bad), bad


def test_it_says_so_on_every_sweep_not_only_the_first(pg, owner_id, capsys):
    _seed(pg, owner_id)
    _sweep()
    capsys.readouterr()
    assert _sweep() == "skipped"
    assert "no usable adapter" in capsys.readouterr().out, \
        "a once-only line is a line nobody sees"


def test_an_empty_connection_stays_silent(pg, owner_id, capsys):
    _seed(pg, owner_id, with_deployment=False)
    assert _sweep() == "skipped"
    assert capsys.readouterr().out == "", \
        "a connection with no deployments has nothing to report"


def test_a_stopped_deployment_does_not_keep_it_loud(pg, owner_id, capsys):
    _seed(pg, owner_id)
    with pg() as s:
        s.get(persistence.Deployment, DEP).status = "stopped"
        s.commit()
    assert _sweep() == "skipped"
    assert capsys.readouterr().out == ""


def test_the_cli_paper_broker_reaches_the_venue(pg, owner_id, monkeypatch):
    """The defect this file exists for: `alpaca-paper` used to land in the
    branch above, so every sweep of a CLI paper deployment sent nothing."""
    from dqengine.adapters.alpaca_paper import AlpacaPaperAdapter
    calls = []
    monkeypatch.setattr(AlpacaPaperAdapter, "ensure_session",
                        lambda self, creds: None)
    monkeypatch.setattr(AlpacaPaperAdapter, "positions",
                        lambda self, creds: calls.append("positions") or {})
    monkeypatch.setattr(AlpacaPaperAdapter, "open_orders",
                        lambda self, creds: [])
    monkeypatch.setattr(executor, "_poll_executions", lambda *a, **k: (0, 0))
    _seed(pg, owner_id, broker="alpaca-paper")
    book_for(CONN).apply_audit({}, [])
    assert _sweep() == "audited"
    assert calls == ["positions"], "the sweep never read the account"
