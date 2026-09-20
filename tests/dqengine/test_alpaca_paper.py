"""No code path from the open driver to real money: every call the paper
adapter makes reaches the paper host, whatever the creds say."""
import pytest

from dqengine.adapters import alpaca_paper
from dqengine.adapters.alpaca_paper import AlpacaPaperAdapter


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def fake_req(creds, method, path, body=None, params=None):
        calls.append((dict(creds), method, path, body))
        return [] if method == "GET" else {"id": "o1", "symbol": "SPY",
                                           "qty": "1", "side": "buy",
                                           "type": "market", "status": "new"}
    import dqengine.adapters.alpaca as mod
    monkeypatch.setattr(mod, "_req", fake_req)
    return calls


def test_live_flag_in_creds_is_overridden_to_paper_on_every_call(wire):
    a = AlpacaPaperAdapter()
    live_creds = {"key_id": "k", "secret_key": "s", "paper": False}
    a.positions(live_creds)
    a.open_orders(live_creds)
    a.submit(live_creds, "SPY", 1, "buy")
    assert wire and all(c[0]["paper"] is True for c in wire)
    assert live_creds["paper"] is False, "the caller's dict is not mutated"
    from dqengine.adapters.alpaca import PAPER_BASE, _base
    assert all(_base(c[0]) == PAPER_BASE for c in wire)


def test_creds_from_env_reads_either_spelling_and_names_the_missing_var():
    c = AlpacaPaperAdapter.creds_from_env({"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"})
    assert c == {"key_id": "k", "secret_key": "s", "paper": True}
    with pytest.raises(KeyError, match="ALPACA_KEY_ID and ALPACA_SECRET_KEY"):
        AlpacaPaperAdapter.creds_from_env({"ALPACA_KEY_ID": "k"})


def test_identity_and_caps_come_from_the_platform_adapter():
    assert AlpacaPaperAdapter.id == "alpaca-paper"
    assert AlpacaPaperAdapter.caps.paper is True
    assert all(hasattr(AlpacaPaperAdapter, m) for m in alpaca_paper._PINNED)
