"""Bus semantics against a real local Redis (db 9, flushed per test).

The `bus` fixture skips when no Redis listens on :6380 -- mirroring
conftest's postgres skip. Two tests stand apart from it:
test_bus_from_env_none_without_url needs neither the redis package nor a
server, and test_bus_from_env_builds_a_bus_when_url_is_set needs only the
package (importorskip; from_url builds a pool, it does not connect).
Start a server with: docker run -d --name platform-redis -p 6380:6379 redis:7-alpine
"""
import pytest

from dqengine.live.bus import Bus, bus_from_env

TEST_URL = "redis://localhost:6380/9"


@pytest.fixture()
def bus():
    try:
        b = Bus(TEST_URL)
        b.ping()
    except Exception:
        pytest.skip("no redis on :6380")
    b._r.flushdb()
    yield b
    b._r.flushdb()


def test_publish_read_ack_roundtrip(bus):
    bus.ensure_group("bars", "g")
    bus.publish("bars", {"sym": "TQQQ", "start_ms": 34200000})
    got = bus.read("bars", "g", "c1", block_ms=10)
    assert len(got) == 1
    _id, fields = got[0]
    assert fields == {"sym": "TQQQ", "start_ms": "34200000"}
    bus.ack("bars", "g", _id)
    assert bus.read("bars", "g", "c1", block_ms=10) == []


def test_unacked_entries_are_redelivered_after_restart(bus):
    """The crash-replay property workers rely on: same group + same consumer
    name, nothing acked -> everything comes back."""
    bus.ensure_group("bars", "g")
    bus.publish("bars", {"sym": "A"})
    bus.publish("bars", {"sym": "B"})
    first = bus.read("bars", "g", "w", block_ms=10)
    assert len(first) == 2                 # delivered, deliberately not acked
    reborn = Bus(TEST_URL)                 # "restarted" worker, same identity
    again = reborn.read("bars", "g", "w", block_ms=10)
    assert [f["sym"] for _, f in again] == ["A", "B"]
    reborn.ack("bars", "g", *[i for i, _ in again])
    assert reborn.read("bars", "g", "w", block_ms=10) == []


def test_two_groups_each_see_every_entry(bus):
    """Workers for different deployments each need every bar notification:
    groups are independent cursors, not competing consumers."""
    bus.ensure_group("bars", "dep:a")
    bus.ensure_group("bars", "dep:b")
    bus.publish("bars", {"sym": "SPY"})
    a = bus.read("bars", "dep:a", "w", block_ms=10)
    b = bus.read("bars", "dep:b", "w", block_ms=10)
    assert len(a) == 1 and len(b) == 1


def test_ensure_group_is_idempotent(bus):
    bus.ensure_group("s", "g")
    bus.ensure_group("s", "g")             # BUSYGROUP swallowed


def test_heartbeat_keys(bus):
    bus.set_ex("worker:hb:d1", "123", ex_s=60)
    assert bus.get("worker:hb:d1") == "123"
    assert bus.get("worker:hb:absent") is None


def test_bus_from_env_none_without_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert bus_from_env() is None


def test_bus_from_env_builds_a_bus_when_url_is_set(monkeypatch):
    # the engine does not declare redis; a self-hoster without it never
    # constructs a Bus (spec 2026-09-18 §5 R2)
    pytest.importorskip("redis")
    # redis.Redis.from_url only builds a connection pool -- no server needed
    monkeypatch.setenv("REDIS_URL", TEST_URL)
    b = bus_from_env()
    assert b is not None and b.url == TEST_URL


def test_delete_removes_a_key(bus):
    bus.set_ex("warm:stale:dep-1", "executions ingested", 60)
    assert bus.get("warm:stale:dep-1") == "executions ingested"
    bus.delete("warm:stale:dep-1")
    assert bus.get("warm:stale:dep-1") is None
    bus.delete("warm:stale:dep-1")          # deleting a missing key is fine
