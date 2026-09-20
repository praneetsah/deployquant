"""Redis Streams wrapper for the worker/OMS bus.

The bus carries NOTIFICATIONS, not data. A bar event says "symbol X closed a
bar" -- the bar itself lives in the Postgres bar store, and the consumer
pulls truth from there (the pull model Plan 1 established: the store's view
IS the event order, so cross-symbol races cannot exist). Losing the bus
therefore loses no data, only promptness, and the poll loop remains the
safety net -- exactly the degradation contract the streamer already has.

Consumer groups give the replay property workers rely on: a worker that
crashes mid-batch rejoins its group under the same consumer name and is
redelivered everything it never acked. At-least-once + idempotent ticks
(tick_deployment re-derives from the store) = safe.

No silent failure: callers surface a dead bus in /api/health; this module
just raises.
"""
from __future__ import annotations

import os
from typing import Optional

MAXLEN = 10000        # ~3 hours of 58-symbol minutes; the store is the
                      # durable record, the stream is a wake-up buffer


class Bus:
    def __init__(self, url: str):
        import redis
        self.url = url
        self._r = redis.Redis.from_url(url, decode_responses=True)

    def ping(self) -> bool:
        return bool(self._r.ping())

    def publish(self, stream: str, fields: dict) -> str:
        return self._r.xadd(stream, {k: str(v) for k, v in fields.items()},
                            maxlen=MAXLEN, approximate=True)

    def ensure_group(self, stream: str, group: str) -> None:
        import redis
        try:
            self._r.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def read(self, stream: str, group: str, consumer: str,
             block_ms: int = 1000, count: int = 100) -> list:
        """Backlog first (entries delivered to this consumer but never
        acked -- the crash-replay path), then new entries. Returns
        [(id, fields), ...]."""
        out = []
        backlog = self._r.xreadgroup(group, consumer, {stream: "0"},
                                     count=count)
        for _, entries in backlog or []:
            out.extend(entries)
        if len(out) < count:
            fresh = self._r.xreadgroup(group, consumer, {stream: ">"},
                                       count=count - len(out),
                                       block=block_ms)
            for _, entries in fresh or []:
                out.extend(entries)
        return out

    def ack(self, stream: str, group: str, *ids: str) -> None:
        if ids:
            self._r.xack(stream, group, *ids)

    # ---- plain key helpers (worker heartbeats) ----

    def set_ex(self, key: str, value: str, ex_s: int) -> None:
        self._r.set(key, value, ex=ex_s)

    def get(self, key: str) -> Optional[str]:
        return self._r.get(key)

    def delete(self, key: str) -> None:
        self._r.delete(key)


# Process-wide singleton, set once at startup by main's init (or left None
# when REDIS_URL is absent). The streamer and supervisor read it; a None bus
# means "workers disabled, visibly" -- /api/health carries the reason.
BUS: Optional[Bus] = None


def init_bus() -> Optional[Bus]:
    global BUS
    BUS = bus_from_env()
    return BUS


def bus_from_env() -> Optional[Bus]:
    """REDIS_URL unset -> None: workers disabled, visibly (health says why),
    and the platform runs exactly as it did before this module existed."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    return Bus(url)
