"""The socket frame reader is fed by a process that runs user code. Every
byte is attacker-controlled: the cap, the per-syscall deadline and the
JSON-object check are what stand between a hostile strategy and a driver
thread that never comes back.
"""
import os
import socket
import struct
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dqengine.runtime import engine_rpc as rpc                  # noqa: E402


def _pair():
    a, b = socket.socketpair()
    return a, b


def test_round_trip():
    a, b = _pair()
    rpc.send_frame(a, {"seq": 1, "op": "ping", "args": {}}, time.monotonic() + 1)
    assert rpc.recv_frame(b, time.monotonic() + 1) == {"seq": 1, "op": "ping", "args": {}}


def test_closed_after_two_header_bytes_is_eof_not_a_document():
    a, b = _pair()
    a.send(b"\x00\x00"); a.close()
    with pytest.raises(EOFError):
        rpc.recv_frame(b, time.monotonic() + 1)


def test_closed_mid_body_is_eof():
    a, b = _pair()
    a.send(struct.pack(">I", 10) + b"{\"a\":"); a.close()
    with pytest.raises(EOFError):
        rpc.recv_frame(b, time.monotonic() + 1)


def test_a_dribbling_peer_is_cut_at_the_deadline_not_per_byte():
    """One byte per 0.2 s against a 0.5 s deadline: the reader must give up
    at ~0.5 s. A per-recv timeout would let it run bytes x 0.5 s."""
    a, b = _pair()
    body = b'{"seq": 1, "ok": true, "result": {}}'
    frame = struct.pack(">I", len(body)) + body

    def dribble():
        for ch in frame:
            try:
                a.send(bytes([ch]))
            except OSError:
                return
            time.sleep(0.2)
    threading.Thread(target=dribble, daemon=True).start()
    t = time.monotonic()
    with pytest.raises(TimeoutError):
        rpc.recv_frame(b, t + 0.5)
    assert time.monotonic() - t < 1.0


def test_a_length_over_the_cap_is_refused_without_reading_the_body():
    a, b = _pair()
    a.send(struct.pack(">I", rpc.FRAME_MAX + 1))
    with pytest.raises(ValueError, match="too large"):
        rpc.recv_frame(b, time.monotonic() + 1)


def test_a_body_that_is_not_an_object_is_refused():
    a, b = _pair()
    a.send(struct.pack(">I", 2) + b"[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        rpc.recv_frame(b, time.monotonic() + 1)


def test_send_respects_the_deadline_when_the_peer_never_reads():
    a, b = _pair()
    big = {"seq": 1, "op": "x", "args": {"blob": "x" * (4 << 20)}}
    t = time.monotonic()
    with pytest.raises((TimeoutError, OSError)):
        rpc.send_frame(a, big, t + 0.3)
    assert time.monotonic() - t < 1.0


def test_envelope_error_shape_is_the_one_the_driver_matches():
    e = rpc.envelope_error({"ok": False, "error": {"type": "ValueError", "message": "nope"},
                            "dead": "gone"})
    assert str(e) == "ValueError: nope (engine dead: gone)"
    e = rpc.envelope_error({"ok": False, "error": {"type": "X", "message": "y"}})
    assert str(e) == "X: y"
