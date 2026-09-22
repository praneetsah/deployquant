"""The bus consumers between the driver and the executor (spec 2026-09-19 §3.3).

A tick that acts publishes two things and then goes back to waiting for the
next bar: `intents {dep, conn, seq}` the moment it has an order to place,
and `sync {conn}` once its payload is written. Something has to read those
and call the executor. That is this module, and it is the last piece of the
order path that lived on the hosted side.

Both consumers take their handler as an argument. The defaults below are
the open ones -- the executor's fast lane with the plain sweep behind it --
which is all a single-deployment account needs. A host that folds several
deployments onto one broker account passes its own handler and keeps its
combiner to itself; nothing else about the lane changes.

Stream names, group names and the consumer name are stored state on the
bus: they are what an already-running group is keyed by, so they are
literals here and not configuration.
"""
from __future__ import annotations

import threading
import time

from dqengine.live.bus import Bus


def default_sync(conn_id: str) -> None:
    """Reconcile one connection: the plain sweep, nothing around it.

    One deployment on one connection needs no wrapper to reach the venue.
    A host that folds several deployments onto one account passes its own
    handler to the consumer instead."""
    from dqengine.live.executor import sync_broker_account
    sync_broker_account(conn_id)


def default_intent(conn_id: str) -> str:
    """Handle one fast-lane intent: the executor's fast pass, with the
    plain sweep as its fallback."""
    from dqengine.live.executor import handle_intent, sync_broker_account
    return handle_intent(conn_id, sweep=sync_broker_account)


def consume_sync_once(bus: Bus, sync=None, block_ms: int = 1000) -> int:
    """One pass of the sync consumer: a tick that produced a fresh payload
    publishes {"conn": id}, and the process that owns the connection -- the
    single broker writer for it -- reconciles the account. Dedupes within
    the batch (two deployments on one connection often fire together; the
    sweep reconciles the whole account, so once is enough). Returns the
    number of syncs performed."""
    if sync is None:
        sync = default_sync
    events = bus.read("sync", "api", "main", block_ms=block_ms)
    if not events:
        return 0
    conns, done = [], set()
    for _, f in events:
        cid = f.get("conn")
        if cid and cid not in done:
            done.add(cid)
            conns.append(cid)
    n = 0
    for cid in conns:
        try:
            sync(cid)
            n += 1
        except Exception as e:
            print(f"[workers] sync {cid} failed: {e!r}", flush=True)
    bus.ack("sync", "api", *[i for i, _ in events])
    return n


def consume_intents_once(bus: Bus, handle=None,
                         block_ms: int = 1000) -> int:
    """One pass of the fast-lane consumer: a tick publishes {dep, conn, seq}
    the moment it acts, and the handler submits through the book. Seq
    idempotency drops redeliveries; batch dedupe runs each connection once
    (the fast reconcile reads every deployment's fresh payload anyway).
    Returns handled connections."""
    from dqengine.live.book import book_for
    if handle is None:
        handle = default_intent
    events = bus.read("intents", "oms-fast", "main", block_ms=block_ms)
    if not events:
        return 0
    todo, seen = [], set()
    for _, f in events:
        conn, dep = f.get("conn"), f.get("dep")
        try:
            seq = int(f.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        fresh = bool(conn and dep and book_for(conn).intent_is_new(dep, seq))
        if fresh and conn not in seen:
            seen.add(conn)
            todo.append(conn)
    n = 0
    if len(todo) <= 1:
        for conn in todo:
            try:
                handle(conn)
                n += 1
            except Exception as e:
                print(f"[oms] intent {conn} failed: {e!r}", flush=True)
    else:
        # independent lanes: one user's burst must never queue another
        # user's orders (per-connection locks keep one transmitter each)
        from concurrent.futures import ThreadPoolExecutor

        def _one(conn):
            try:
                handle(conn)
                return 1
            except Exception as e:
                print(f"[oms] intent {conn} failed: {e!r}", flush=True)
                return 0
        with ThreadPoolExecutor(max_workers=min(16, len(todo))) as ex:
            n = sum(ex.map(_one, todo))
    bus.ack("intents", "oms-fast", *[i for i, _ in events])
    return n


def start_intent_consumer(bus: Bus, handle=None, stop=None):
    """Run the fast-lane consumer on its own daemon thread. `handle` is the
    host's intent handler; left out, every intent takes `default_intent`.

    `stop` is a callable asked once per pass whether to return, and the
    thread is returned so a caller that passes one can join it. A host whose
    consumers live as long as the process passes neither."""
    bus.ensure_group("intents", "oms-fast")

    def loop():
        while stop is None or not stop():
            try:
                consume_intents_once(bus, handle=handle)
            except Exception as e:
                print(f"[oms] intent consumer error: {e!r}", flush=True)
                time.sleep(2)
    t = threading.Thread(target=loop, daemon=True, name="intent-consumer")
    t.start()
    return t


def start_sync_consumer(bus: Bus, sync=None, stop=None):
    """Run the sync consumer on its own daemon thread. `sync` is the host's
    connection handler; left out, every event takes `default_sync`. `stop`
    and the returned thread work as they do for the fast lane above."""
    bus.ensure_group("sync", "api")

    def loop():
        while stop is None or not stop():
            try:
                consume_sync_once(bus, sync=sync)
            except Exception as e:
                print(f"[workers] sync consumer error: {e!r}", flush=True)
                time.sleep(2)
    t = threading.Thread(target=loop, daemon=True, name="sync-consumer")
    t.start()
    return t
