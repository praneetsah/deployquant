"""The keystone gate (second-resolution spec §9.1): the historical batch
aggregator and the live streaming consolidator must produce IDENTICAL bars
from an identical trade tape — property-tested over randomized tapes with
excluded conditions, bursts, empty seconds, and session edges."""
import random

from dqengine.live.secbars import (SESSION_OPEN_MS, SecondBucketConsolidator,
                                   aggregate_trades_to_seconds,
                                   parse_alpaca_trade, trade_eligible)

CLOSE = 57600000


def _tape(seed, n=4000):
    rng = random.Random(seed)
    out, ms = [], SESSION_OPEN_MS - 2000        # start before the session
    px = 100.0
    for _ in range(n):
        ms += rng.choice([0, 1, 3, 40, 200, 900, 1000, 2500, 30000])
        px = max(1.0, px * (1.0 + rng.uniform(-0.0008, 0.0008)))
        conds = rng.choice([(), (), (), ("@",), ("I",), ("T",), ("@", "F"),
                            ("W", "@")])
        out.append((ms, round(px, 4), float(rng.randint(1, 500)), conds))
    return out


def test_batch_equals_streaming_over_random_tapes():
    for seed in range(12):
        tape = _tape(seed)
        batch = aggregate_trades_to_seconds(tape, close_ms=CLOSE)
        cons = SecondBucketConsolidator(close_ms=CLOSE)
        live = {}
        last_flush = 0
        for ms, px, sz, conds in tape:
            # flush at arbitrary cadence, like a wall-clock loop would
            if ms - last_flush > 3000:
                for sym, bar in cons.flush_before(ms):
                    live[bar[0]] = bar
                last_flush = ms
            cons.add_trade("X", ms, px, sz, conds)
        for sym, bar in cons.flush_before(10**12):
            live[bar[0]] = bar
        assert live == batch, f"seed {seed}: batch and streaming diverged"


def test_excluded_conditions_and_session_bounds():
    tape = [
        (SESSION_OPEN_MS - 1, 99.0, 10, ()),           # pre-open: out
        (SESSION_OPEN_MS, 100.0, 10, ()),              # opening second
        (SESSION_OPEN_MS + 100, 200.0, 10, ("I",)),    # odd lot: ignored
        (SESSION_OPEN_MS + 300, 101.0, 5, ("@",)),
        (CLOSE - 1, 102.0, 1, ()),                     # last eligible ms
        (CLOSE, 103.0, 1, ()),                         # at close: out
    ]
    bars = aggregate_trades_to_seconds(tape, close_ms=CLOSE)
    assert set(bars) == {SESSION_OPEN_MS, ((CLOSE - 1) // 1000) * 1000}
    o = bars[SESSION_OPEN_MS]
    assert o[1:] == [100.0, 101.0, 100.0, 101.0, 15.0], \
        "the odd lot must not touch o/h/l/c or volume"


def test_gap_seconds_produce_no_bars():
    tape = [(SESSION_OPEN_MS, 100.0, 1, ()),
            (SESSION_OPEN_MS + 30000, 101.0, 1, ())]
    bars = aggregate_trades_to_seconds(tape, close_ms=CLOSE)
    assert len(bars) == 2


def test_parse_alpaca_trade_nanoseconds_and_conditions():
    row = {"t": "2024-06-17T13:30:00.123456789Z", "p": 101.5, "s": 7,
           "c": ["@", "F"]}
    ms, px, sz, conds = parse_alpaca_trade(row)
    assert ms == SESSION_OPEN_MS + 123 and px == 101.5 and sz == 7.0
    assert trade_eligible(conds)
    assert parse_alpaca_trade({"t": "garbage", "p": 1}) is None
    assert parse_alpaca_trade({"p": 1}) is None


def test_trade_succession_completes_prior_second():
    cons = SecondBucketConsolidator(close_ms=CLOSE)
    cons.add_trade("X", SESSION_OPEN_MS + 100, 100.0, 1)
    cons.add_trade("X", SESSION_OPEN_MS + 1100, 101.0, 1)   # next second
    done = cons.flush_before(SESSION_OPEN_MS + 1500)
    assert [b[0] for _, b in done] == [SESSION_OPEN_MS], \
        "the succession trade completes the prior bar even without a flush"
