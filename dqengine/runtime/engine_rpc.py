"""RPC between a long-lived sandbox engine and its driver: files, or a
unix socket on the same directory.

The sandbox runs with `--network none` on purpose (pyrunner.py: user code
never sees broker creds, Postgres, Redis, or the API env), and the only
thing the two sides share is the per-session workdir mounted at /work.
Two transports live on it, same envelope, same op table (engine_ops.py):

  * FILES (the original, kept for macOS Docker Desktop, where a unix
    socket on a bind mount cannot be connected from the host):

    driver  writes  req-{seq}.json   {"op": ..., "args": {...}}      (atomic)
    server  writes  res-{seq}.json   {"ok": true, "result": ...}     (atomic)
                                  or {"ok": false, "error": {...}, "dead": ...}
    server  writes  state.json       heartbeat: {"alive", "dead", "seq_done",
                                     "ts", "pid"}                    (atomic)

Every write goes through a temp file + os.replace, so a reader never sees a
half-written document. Requests are processed strictly in sequence order;
a request is deleted once its response is written, so a restarted server
cannot replay one.

  * UNIX SOCKET (prod, Linux): the server listens on <workdir>/engine.sock
    -- a filesystem object, not a network; `--network none` is untouched
    and only a process with a bind of that host directory (the sandbox
    service) can connect. Frames are 4-byte big-endian length + UTF-8 JSON
    object. A request is {"seq", "op", "args"}; a response is
    {"seq", "ok", "result" | "error", "dead"}. `seq` is the driver's
    per-session monotonic counter (the same one that names the files):
    the driver refuses a response whose seq is not the one it sent, and
    the server refuses -- WITHOUT executing -- a seq at or below the last
    one it answered, marking the engine stale, because a repeated seq
    means a caller retried, which the contract forbids. Round trip is
    ~0.1 ms against ~2 ms of 1 ms polls on the file path.

TRUST: user code is exec'd in the SAME process as the server, so every
byte the driver reads -- file or frame -- is attacker-controlled. The
frame reader caps the size, applies a wall-clock deadline to EVERY
syscall (a peer dribbling one byte per timeout cannot hold the driver),
and the driver treats any failure to parse or validate as "close the
socket, the session is dead, raise".

Both ends import this module: the engine side (engine_server.py, inside the
container) and the driver side (dqengine/sandbox/pyrunner.py). Keeping the
wire format in one place is what stops the two from drifting.
"""
from __future__ import annotations

import json
import os
import re
import struct
import time

REQ_RE = re.compile(r"^req-(\d+)\.json$")
STATE_FILE = "state.json"
STOP_OP = "stop"
SOCK_NAME = "engine.sock"

# Largest frame either side will accept. Measured 2026-09-06: a FULL
# snapshot of the 5.4-year tqqq_weekly deployment (520 fills, 1364 equity days)
# is 175 KB, so a 10-year one is ~350 KB; a first-of-day push for a
# 29-symbol universe is ~0.5 MB of rows. 8 MiB is >4x the larger of those
# with room for a 100-symbol universe, and small enough that a crafted
# length prefix cannot make the driver allocate anything that matters.
FRAME_MAX = 8 << 20


# ---------------------------------------------------------------- frames

def _remaining(deadline: float) -> float:
    r = deadline - time.monotonic()
    if r <= 0:
        raise TimeoutError("frame deadline passed")
    return r


def send_frame(sock, doc: dict, deadline: float) -> None:
    """Length-prefixed JSON. The remaining time is re-applied before every
    send: a peer that stops reading cannot hold the sender past `deadline`."""
    body = json.dumps(doc, separators=(",", ":")).encode()
    buf = memoryview(struct.pack(">I", len(body)) + body)
    while buf:
        sock.settimeout(_remaining(deadline))
        n = sock.send(buf)
        if n == 0:
            raise EOFError("socket closed mid-send")
        buf = buf[n:]


def recv_frame(sock, deadline: float) -> dict:
    """One frame, or raises: EOFError on a close (a partial frame is EOF,
    never a half-parsed document), TimeoutError at `deadline`, ValueError
    for a frame over FRAME_MAX or a body that is not a JSON object."""
    head = _recv_exact(sock, 4, deadline)
    n = struct.unpack(">I", head)[0]
    if n > FRAME_MAX:
        raise ValueError(f"frame too large: {n} > {FRAME_MAX}")
    doc = json.loads(_recv_exact(sock, n, deadline))
    if not isinstance(doc, dict):
        raise ValueError("frame is not a JSON object")
    return doc


def _recv_exact(sock, n: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise EOFError("socket closed mid-frame")
        buf += chunk
    return bytes(buf)


def sock_path(workdir: str) -> str:
    return os.path.join(workdir, SOCK_NAME)


# -------------------------------------------------------------- envelope

def envelope_error(res: dict) -> RuntimeError:
    """The ONE shaper of an error envelope into the RuntimeError the driver
    matches on -- file, socket and in-process all raise through here."""
    err = res.get("error") or {}
    return RuntimeError(
        f"{err.get('type', 'Error')}: {err.get('message', '')}"
        + (f" (engine dead: {res['dead']})" if res.get("dead") else ""))


def write_atomic(path: str, doc: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def read_json(path: str):
    """None when the file is absent or mid-write (which os.replace makes
    impossible to observe, but a caller racing an unlink can still miss)."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def req_path(workdir: str, seq: int) -> str:
    return os.path.join(workdir, f"req-{seq}.json")


def res_path(workdir: str, seq: int) -> str:
    return os.path.join(workdir, f"res-{seq}.json")


def pending_requests(workdir: str) -> list[int]:
    """Sequence numbers of requests waiting for a response, ascending."""
    out = []
    try:
        names = os.listdir(workdir)
    except OSError:
        return out
    for n in names:
        m = REQ_RE.match(n)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def write_state(workdir: str, **fields) -> None:
    doc = {"ts": time.time(), "pid": os.getpid()}
    doc.update(fields)
    write_atomic(os.path.join(workdir, STATE_FILE), doc)


def read_state(workdir: str):
    return read_json(os.path.join(workdir, STATE_FILE))
