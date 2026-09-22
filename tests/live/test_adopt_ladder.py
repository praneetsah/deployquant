"""The mode ladder: a broker connection moves from `off` to `observe` to
`enforce`, one rung at a time. The executions ledger itself is pinned in
test_executions.py, and the command that walks this ladder in
test_adopt.py."""
import pytest

from dqengine.live import adopt as sx
from dqengine.live import persistence


def test_set_mode_rejects_an_unknown_mode(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c9", user_id=owner_id, broker="alpaca"))
        s.commit()
    with pytest.raises(ValueError):
        sx.set_mode("c9", "on")


def test_set_mode_cannot_skip_observe(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c8", user_id=owner_id, broker="alpaca"))
        s.commit()
    with pytest.raises(ValueError):
        sx.set_mode("c8", "enforce")          # off -> enforce is not allowed


def test_set_mode_advances_and_reverts(pg, owner_id):
    with pg() as s:
        s.add(persistence.BrokerConnection(id="c7", user_id=owner_id, broker="alpaca"))
        s.commit()
    assert sx.set_mode("c7", "observe") == "observe"
    assert sx.set_mode("c7", "enforce") == "enforce"
    assert sx.set_mode("c7", "observe") == "observe"   # reverting is allowed
