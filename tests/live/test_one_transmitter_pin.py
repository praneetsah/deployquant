"""One sync cycle at a time per broker connection.

`_conn_lock` hands out one lock per connection and `sync_broker_account` does
its whole cycle inside it. Two cycles on one account at once would both read
"no order yet" and both send it. The auditor and fast-path tests exercise the
logic around this, but nothing tested the lock itself.
"""
import threading
import time

from dqengine.live import executor

from rig import blind_rig


def test_one_lock_per_connection_and_a_different_one_for_another():
    a1, a2, b = executor._conn_lock("conn-A"), executor._conn_lock("conn-A"), executor._conn_lock("conn-B")
    assert a1 is a2
    assert a1 is not b


def test_two_cycles_on_one_connection_never_overlap(pg, owner_id, monkeypatch):
    blind_rig(pg, owner_id, monkeypatch, "c36", "d36")
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: True)   # never skip for pacing
    real = executor.reconcile
    inside, overlaps, spans = [0], [0], []

    def slow(*a, **k):
        inside[0] += 1
        if inside[0] > 1:
            overlaps[0] += 1
        t0 = time.time()
        try:
            time.sleep(0.15)
            return real(*a, **k)
        finally:
            spans.append((t0, time.time()))
            inside[0] -= 1
    monkeypatch.setattr(executor, "reconcile", slow)

    threads = [threading.Thread(target=executor.sync_broker_account, args=("c36",), kwargs={"fast": False})
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(spans) >= 3                      # every cycle ran (a full cycle calls reconcile at least once)
    assert overlaps[0] == 0
    ordered = sorted(spans)
    assert all(ordered[i][1] <= ordered[i + 1][0] + 1e-6 for i in range(len(ordered) - 1))


def test_the_lock_is_released_when_a_cycle_fails(pg, owner_id, monkeypatch):
    blind_rig(pg, owner_id, monkeypatch, "c36b", "d36b")

    def boom(*a, **k):
        raise RuntimeError("mid-cycle failure")
    monkeypatch.setattr(executor, "reconcile", boom)
    assert executor.sync_broker_account("c36b", fast=True) == "error"
    lock = executor._conn_lock("c36b")
    assert lock.acquire(blocking=False)         # not left held
    lock.release()
