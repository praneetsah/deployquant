"""The two bus consumers: dedupe, acks, isolation, and which handler runs.

No database and no Redis here. The consumers only read, dedupe and ack, so
a list-backed bus is the whole world they need, and the handler they call
is the thing under test.
"""
import pytest

from dqengine.live import consumer


class MiniBus:
    """Everything the consumers ask of a bus: read a batch, ack it, and
    (for the thread starters) declare the group."""

    def __init__(self, events=()):
        self.events = list(events)
        self.acked = []
        self.groups = []
        self.reads = []

    def read(self, stream, group, name, block_ms=0):
        self.reads.append((stream, group, name, block_ms))
        e, self.events = self.events, []
        return e

    def ack(self, stream, group, *ids):
        self.acked.append((stream, group, list(ids)))

    def ensure_group(self, stream, group):
        self.groups.append((stream, group))


# ---------------------------------------------------------------- sync lane

def test_sync_consumer_dedupes_within_the_batch_and_acks_everything():
    bus = MiniBus([("1-1", {"conn": "c1"}), ("1-2", {"conn": "c1"}),
                   ("1-3", {"conn": "c2"})])
    calls = []
    n = consumer.consume_sync_once(bus, sync=calls.append, block_ms=10)
    assert n == 2 and calls == ["c1", "c2"], "one sync per connection"
    assert bus.acked == [("sync", "api", ["1-1", "1-2", "1-3"])], \
        "every entry acked, duplicates included"
    assert bus.reads == [("sync", "api", "main", 10)]


def test_sync_consumer_skips_events_with_no_connection():
    bus = MiniBus([("1-1", {}), ("1-2", {"conn": ""}),
                   ("1-3", {"conn": "c9"})])
    calls = []
    assert consumer.consume_sync_once(bus, sync=calls.append) == 1
    assert calls == ["c9"]
    assert bus.acked[0][2] == ["1-1", "1-2", "1-3"], "acked anyway"


def test_sync_consumer_one_failure_does_not_stop_the_others(capsys):
    bus = MiniBus([("1-1", {"conn": "bad"}), ("1-2", {"conn": "good"})])
    calls = []

    def sync(cid):
        if cid == "bad":
            raise RuntimeError("boom")
        calls.append(cid)

    n = consumer.consume_sync_once(bus, sync=sync)
    assert n == 1 and calls == ["good"]
    assert "sync bad failed" in capsys.readouterr().out
    assert bus.acked, "a failed sync still acks: redelivery would re-sweep"


def test_an_empty_read_acks_nothing_and_returns_zero():
    bus = MiniBus([])
    assert consumer.consume_sync_once(bus) == 0
    assert consumer.consume_intents_once(bus) == 0
    assert bus.acked == []


# -------------------------------------------------------------- intent lane

def _fresh_books(monkeypatch, fresh=True):
    """book_for(conn).intent_is_new without a database behind it."""
    asked = []

    class Book:
        def __init__(self):
            self.seen = set()

        def intent_is_new(self, dep, seq):
            asked.append((dep, seq))
            if not fresh:
                return False
            key = (dep, seq)
            if key in self.seen:
                return False
            self.seen.add(key)
            return True

    books = {}
    from dqengine.live import book as book_mod
    monkeypatch.setattr(book_mod, "book_for",
                        lambda conn: books.setdefault(conn, Book()))
    return asked


def test_intent_consumer_dedupes_by_connection_and_drops_redeliveries(
        monkeypatch):
    _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"dep": "dA", "conn": "cX", "seq": "100"}),
                   ("1-2", {"dep": "dB", "conn": "cX", "seq": "100"}),
                   ("1-3", {"dep": "dA", "conn": "cX", "seq": "100"}),
                   ("1-4", {"dep": "dC", "conn": "cY", "seq": "100"})])
    handled = []
    n = consumer.consume_intents_once(bus, handle=handled.append)
    assert handled == ["cX", "cY"] and n == 2
    assert bus.acked == [("intents", "oms-fast",
                          ["1-1", "1-2", "1-3", "1-4"])]

    bus.events = [("2-1", {"dep": "dA", "conn": "cX", "seq": "100"})]
    handled.clear()
    assert consumer.consume_intents_once(bus, handle=handled.append) == 0
    assert handled == [], "the same seq is not a second order"


def test_intent_consumer_treats_an_unparsable_seq_as_zero(monkeypatch):
    asked = _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"dep": "d", "conn": "c", "seq": "not-a-number"}),
                   ("1-2", {"dep": "e", "conn": "c2", "seq": None}),
                   ("1-3", {"dep": "f", "conn": "c3", "seq": "12"})])
    handled = []
    assert consumer.consume_intents_once(bus, handle=handled.append) == 3
    assert handled == ["c", "c2", "c3"], "a bad seq must not lose the order"
    assert asked == [("d", 0), ("e", 0), ("f", 12)], \
        "an unreadable seq asks the book about 0, never a made-up number"


def test_intent_consumer_skips_incomplete_events(monkeypatch):
    _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"conn": "c", "seq": "1"}),        # no dep
                   ("1-2", {"dep": "d", "seq": "1"})])        # no conn
    handled = []
    assert consumer.consume_intents_once(bus, handle=handled.append) == 0
    assert handled == []
    assert bus.acked[0][2] == ["1-1", "1-2"]


def test_several_connections_run_in_parallel_lanes(monkeypatch):
    """One account's burst must never queue another account's orders."""
    _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"dep": "d1", "conn": "c1", "seq": "1"}),
                   ("1-2", {"dep": "d2", "conn": "c2", "seq": "1"}),
                   ("1-3", {"dep": "d3", "conn": "c3", "seq": "1"})])
    import threading
    started = threading.Barrier(3, timeout=10)
    done = []

    def handle(conn):
        started.wait()          # only clears if all three run at once
        done.append(conn)

    assert consumer.consume_intents_once(bus, handle=handle) == 3
    assert sorted(done) == ["c1", "c2", "c3"]


def test_one_failing_lane_is_counted_out_but_the_others_transmit(
        monkeypatch, capsys):
    _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"dep": "d1", "conn": "bad", "seq": "1"}),
                   ("1-2", {"dep": "d2", "conn": "c2", "seq": "1"}),
                   ("1-3", {"dep": "d3", "conn": "c3", "seq": "1"})])
    ok = []

    def handle(conn):
        if conn == "bad":
            raise RuntimeError("boom")
        ok.append(conn)

    assert consumer.consume_intents_once(bus, handle=handle) == 2
    assert sorted(ok) == ["c2", "c3"]
    assert "intent bad failed" in capsys.readouterr().out


def test_a_single_failing_connection_is_reported_not_raised(monkeypatch,
                                                            capsys):
    _fresh_books(monkeypatch)
    bus = MiniBus([("1-1", {"dep": "d1", "conn": "only", "seq": "1"})])

    def handle(conn):
        raise RuntimeError("boom")

    assert consumer.consume_intents_once(bus, handle=handle) == 0
    assert "intent only failed" in capsys.readouterr().out
    assert bus.acked, "acked so the event is not redelivered forever"


# ------------------------------------------------------------ open defaults

def test_the_default_sync_is_the_plain_sweep(monkeypatch):
    from dqengine.live import executor
    seen = []
    monkeypatch.setattr(executor, "sync_broker_account",
                        lambda cid, **kw: seen.append((cid, kw)))
    bus = MiniBus([("1-1", {"conn": "cZ"})])
    assert consumer.consume_sync_once(bus) == 1
    assert seen == [("cZ", {})], "no wrapper around the sweep"


def test_the_default_intent_is_the_fast_lane_with_the_sweep_behind_it(
        monkeypatch):
    from dqengine.live import executor
    _fresh_books(monkeypatch)
    seen = {}

    def fake_handle_intent(conn_id, sweep):
        seen["conn"] = conn_id
        seen["sweep"] = sweep
        return "submitted"

    monkeypatch.setattr(executor, "handle_intent", fake_handle_intent)
    bus = MiniBus([("1-1", {"dep": "d", "conn": "cQ", "seq": "7"})])
    assert consumer.consume_intents_once(bus) == 1
    assert seen["conn"] == "cQ"
    assert seen["sweep"] is executor.sync_broker_account, \
        "the fallback is the plain sweep, not something the host installed"


# ---------------------------------------------------------- thread starters

def test_the_starters_declare_the_group_and_carry_the_handler(monkeypatch):
    """Both starters hand their argument to every pass, so a host's handler
    is not quietly dropped after the first loop."""
    import threading
    import time
    passed = []
    # never set: each consumer thread does exactly one pass and then parks,
    # so neither spins for the rest of the session. Both are daemons.
    parked = threading.Event()

    def one_pass(kind):
        def run(bus, **kw):
            passed.append((kind, *kw.values()))
            parked.wait()
        return run

    monkeypatch.setattr(consumer, "consume_sync_once", one_pass("sync"))
    monkeypatch.setattr(consumer, "consume_intents_once", one_pass("intent"))
    bus = MiniBus()
    mine = object()
    consumer.start_sync_consumer(bus, sync=mine)
    consumer.start_intent_consumer(bus, handle=mine)
    for _ in range(500):
        if len(passed) >= 2:
            break
        time.sleep(0.01)
    assert ("sync", mine) in passed and ("intent", mine) in passed
    assert ("sync", "api") in bus.groups
    assert ("intents", "oms-fast") in bus.groups
