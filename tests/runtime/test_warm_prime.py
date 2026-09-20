"""The wall-clock schedule fire (spec 2026-09-18): a scheduled callback the
clock has reached fires NOW off driver-supplied prices, and never again when
the official bar lands. Only the callback runs; indicators, on_data, fills
and the bar anchors are the real bar's business.
"""
import os
import sys
from datetime import date, datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from conftest_helpers import OPEN_MS, SynthStore, synth_day             # noqa: E402
from dqengine.runtime.core.data import SCALE                                  # noqa: E402
from dqengine.runtime.warm import WarmPyEngine                                # noqa: E402

D = date(2026, 8, 24)
P = date(2026, 8, 21)
OV = {"start": "2026-08-21", "end": "2026-08-24", "cash": 1000.0}
FIRE_MS = OPEN_MS + 5 * 60_000            # the at(9, 35) rule
CLOSE_MS = 16 * 3600 * 1000

# A rule at 09:35 that records what it saw and rebalances. Six bars a day
# (09:30..09:35 starts, ends 09:31..09:36) so the bar ENDING 09:35 is the
# fifth.
CODE = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 21); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
        self.seen = []
        self.on_data_n = 0
        self.schedule.on(self.date_rules.every_day("TQQQ"),
                         self.time_rules.at(9, 35), self._go)
    def _go(self):
        self.seen.append((self.time, self.securities["TQQQ"].price,
                          self.securities["TQQQ"].close))
        self.set_holdings("TQQQ", 0.5)
    def on_data(self, data):
        self.on_data_n += 1
"""


def _scaled_rows(b, lo=0, hi=None):
    hi = b.n if hi is None else hi
    return [[int(b.start_ms[i]), int(round(b.open[i] * SCALE)),
             int(round(b.high[i] * SCALE)), int(round(b.low[i] * SCALE)),
             int(round(b.close[i] * SCALE)), float(b.volume[i])]
            for i in range(lo, hi)]


def _push(b, upto, pushed_ms=-1):
    rows = [r for r in _scaled_rows(b, 0, upto) if r[0] > pushed_ms]
    return {"TQQQ": {"first_ms": int(b.start_ms[0]), "n_total": upto, "rows": rows}}


def _fills(res):
    return [(f["day"], f["sym"], f["qty"], f["px"]) for f in res["fills"]]


def _engine(code=CODE, store_days=None):
    store = SynthStore(store_days or {P: synth_day(P, [99, 99, 99])})
    eng = WarmPyEngine(code, store=store, overrides=dict(OV))
    eng.warm(through=date(2026, 8, 23))
    return eng


FULL = synth_day(D, [100, 101, 102, 103, 104, 105])      # 6 bars, ends 09:31..09:36


def _open_through_0934(eng):
    """Step the first four bars (ends 09:31..09:34); the fifth ends at
    FIRE_MS and must not be in yet."""
    eng.advance(OPEN_MS + 4 * 60_000, D, bars=_push(FULL, 4))
    return eng


def test_primed_through_starts_unset_and_resets_per_session():
    eng = _engine()
    bt = eng._bt
    assert bt._primed_through == -1
    _open_through_0934(eng)
    bt._primed_through = FIRE_MS                # as prime would leave it
    eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    eng.end_session(D)
    # a new session must not inherit the cursor
    bt._begin_session(date(2026, 8, 25))
    assert bt._primed_through == -1


def test_step_bar_skips_an_event_the_prime_cursor_has_passed():
    eng = _engine()
    _open_through_0934(eng)
    eng._bt._primed_through = FIRE_MS           # pretend 09:35 already primed
    n0 = eng._bt.algo.on_data_n                 # warm-up days count too; go relative
    eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    assert eng._bt.algo.seen == [], "the bar must not re-fire a primed event"
    assert eng._bt.algo.on_data_n == n0 + 2, "but the two new bars still step"


def test_pass_one_exclusion_is_inert_unless_a_prime_covers_the_bar():
    """The gate is `t <= _primed_through`: -1 in every batch run (never
    true), and true on the live path only for a bar that lands behind a
    prime. Pinned by watching what exclude_created reaches the book."""
    eng = _engine()
    _open_through_0934(eng)
    seen = []
    book = eng._bt._book
    real = book.check_resting

    def spy(symbol, o, h, l, c, exclude_created=None):
        seen.append(exclude_created)
        return real(symbol, o, h, l, c, exclude_created=exclude_created)
    book.check_resting = spy
    # no prime: pass 1 carries None, pass 2 carries the bar stamp
    eng.advance(OPEN_MS + 5 * 60_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
    assert seen[:2] == [None, (D, OPEN_MS + 5 * 60_000)]
    seen.clear()
    # a prime that covers the next bar's end: pass 1 now carries the stamp too
    eng._bt._primed_through = OPEN_MS + 6 * 60_000 + 10
    eng.advance(OPEN_MS + 6 * 60_000 + 10, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    assert seen[:2] == [(D, OPEN_MS + 6 * 60_000), (D, OPEN_MS + 6 * 60_000)]


def _q(px, at_ms=0):
    return {"TQQQ": {"last": px, "at_ms": at_ms}}


def test_prime_fires_a_due_event_off_the_supplied_price():
    eng = _engine()
    _open_through_0934(eng)
    out = eng.prime(FIRE_MS + 10, D, _q(104.75))
    algo = eng._bt.algo
    assert out == {"fired": [{"name": "_go", "fire_ms": FIRE_MS}],
                   "priced": 1, "unpriced": []}
    assert algo.seen == [(datetime(2026, 8, 24, 9, 35), 104.75, 103.0)], \
        "callback saw self.time == fire time, .price == quote, .close == last bar"
    assert algo._prices["TQQQ"] == 104.75, "set_holdings sizes off _prices"
    assert eng._bt._primed_through == FIRE_MS + 10
    assert eng.dead is None


def test_prime_sizes_set_holdings_off_the_quote():
    eng = _engine()
    _open_through_0934(eng)
    eng.prime(FIRE_MS + 10, D, _q(200.0))
    # a market order fills on placement (orders.py OrderBook.market), at
    # the price prime wrote into _prices -- the same instant and the same
    # mechanism the bar-driven walk uses, just priced off the quote
    assert [(f["sym"], f["qty"], f["px"]) for f in eng.snapshot()["fills"][-1:]] \
        == [("TQQQ", 2, 200.0)], "50% of $1000 at the $200 quote = 2 shares"


def test_the_real_bar_then_steps_but_does_not_refire():
    eng = _engine()
    _open_through_0934(eng)
    eng.prime(FIRE_MS + 10, D, _q(104.75))
    n_before = eng._bt.algo.on_data_n
    eng.advance(FIRE_MS + 2_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
    algo = eng._bt.algo
    assert len(algo.seen) == 1, "fired once, by the prime"
    assert algo.on_data_n == n_before + 1, "the official bar still steps"
    assert algo._prices["TQQQ"] == 104.0, "official close overwrites the quote"
    assert algo.securities["TQQQ"].price == 104.0
    assert eng.dead is None


def test_prime_with_nothing_due_is_a_pure_noop():
    eng = _engine()
    _open_through_0934(eng)
    algo = eng._bt.algo
    t0, p0, c0 = algo.time, dict(algo._prices), eng._bt._primed_through
    assert eng.prime(FIRE_MS - 1, D, _q(999.0)) is None
    assert algo.time == t0 and algo._prices == p0 and eng._bt._primed_through == c0
    assert algo.seen == []


def test_prime_never_refires_an_event_a_bar_already_ran():
    eng = _engine()
    eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6))   # bar-driven fire at 09:35
    assert len(eng._bt.algo.seen) == 1
    assert eng.prime(FIRE_MS + 30_000, D, _q(1.0)) is None
    assert len(eng._bt.algo.seen) == 1


def test_a_later_event_is_untouched_by_an_earlier_prime():
    code = CODE.replace(
        "self.time_rules.at(9, 35), self._go)",
        "self.time_rules.at(9, 35), self._go)\n"
        "        self.schedule.on(self.date_rules.every_day('TQQQ'),"
        " self.time_rules.at(9, 36), self._go)")
    eng = _engine(code)
    _open_through_0934(eng)
    eng.prime(FIRE_MS + 10, D, _q(104.75))
    assert len(eng._bt.algo.seen) == 1
    # the 09:36 bar lands: its event fires the normal way
    eng.advance(OPEN_MS + 6 * 60_000 + 2_000, D,
                bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    assert len(eng._bt.algo.seen) == 2
    assert eng._bt.algo.seen[1][0] == datetime(2026, 8, 24, 9, 36)


def test_an_unpriced_symbol_keeps_the_last_stepped_close():
    eng = _engine()
    _open_through_0934(eng)
    out = eng.prime(FIRE_MS + 10, D, {})
    assert out["priced"] == 0 and out["unpriced"] == ["TQQQ"]
    assert eng._bt.algo.seen[0][1] == 103.0, "last stepped close (bar ending 09:34)"


def test_prime_touches_no_bar_state():
    eng = _engine()
    _open_through_0934(eng)
    bt = eng._bt
    before = (bt._day_bars["TQQQ"].n, eng._frontier, dict(eng._last_end),
              eng.bars_stepped, bt.algo.securities["TQQQ"].close,
              bt.algo.securities["TQQQ"].open)
    eng.prime(FIRE_MS + 10, D, _q(104.75))
    after = (bt._day_bars["TQQQ"].n, eng._frontier, dict(eng._last_end),
             eng.bars_stepped, bt.algo.securities["TQQQ"].close,
             bt.algo.securities["TQQQ"].open)
    assert before == after
    # and the pushed-bars anchors still reconcile afterwards
    eng.advance(FIRE_MS + 2_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
    assert eng.dead is None


def test_prime_before_the_session_opens_is_none():
    eng = _engine()
    assert eng.session_open is False
    assert eng.prime(FIRE_MS + 10, D, _q(1.0)) is None
    assert eng.dead is None


def test_a_callback_that_raises_kills_the_engine():
    code = CODE.replace("self.set_holdings(\"TQQQ\", 0.5)", "raise ValueError('boom')")
    eng = _engine(code)
    _open_through_0934(eng)
    with pytest.raises(Exception):
        eng.prime(FIRE_MS + 10, D, _q(104.75))
    assert eng.dead


def test_a_dead_engine_refuses_to_prime():
    eng = _engine()
    _open_through_0934(eng)
    eng.stale("x")
    with pytest.raises(RuntimeError):
        eng.prime(FIRE_MS + 10, D, _q(1.0))


def test_model_fills_match_the_bar_driven_run_when_quote_equals_close():
    """Quote == the official 09:35 close: the primed run and the plain
    bar-driven run must produce identical fills. The order's creation stamp
    is (day, fire_ms) either way, so resting pass 2 defers it identically."""
    ref = _engine()
    for k in (4, 5, 6):
        ref.advance(OPEN_MS + k * 60_000, D, bars=_push(FULL, k, ref.pushed_state().get("TQQQ", -1)))
    ref.end_session(D)

    eng = _engine()
    _open_through_0934(eng)
    eng.prime(FIRE_MS + 10, D, _q(float(FULL.close[4])))       # bar ending 09:35 closes at 104
    eng.advance(FIRE_MS + 2_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
    eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    eng.end_session(D)

    assert _fills(eng.snapshot()) == _fills(ref.snapshot())
    assert eng.snapshot()["stats"]["end_equity"] == ref.snapshot()["stats"]["end_equity"]


CLOSE_CODE = """
from AlgorithmImports import *
class A(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2026, 8, 21); self.set_end_date(2026, 8, 24)
        self.set_cash(1000); self.add_equity("TQQQ", Resolution.MINUTE)
        self.seen = []
        self.schedule.on(self.date_rules.every_day("TQQQ"),
                         self.time_rules.before_market_close("TQQQ", 1), self._go)
    def _go(self):
        self.seen.append((self.time, self.securities["TQQQ"].price))
        self.set_holdings("TQQQ", 1.0)
    def on_data(self, data): pass
"""


def test_before_close_fires_at_1559_off_the_quote_not_the_candle():
    """The 2026-09-16 case: at 15:59:00.010 the bar ending 15:59:00 has not
    arrived. The prime fires the rule now; the candle lands 2 s later and
    changes nothing but indicators/prices."""
    day = synth_day(D, [100.0 + i * 0.01 for i in range(390)])   # full session
    eng = _engine(CLOSE_CODE)
    eng.advance(CLOSE_MS - 120_000, D, bars=_push(day, 388))      # through the 15:58 end
    fire = CLOSE_MS - 60_000
    assert eng.next_fire_ms() == fire
    out = eng.prime(fire + 10, D, {"TQQQ": {"last": 111.11, "at_ms": 1}})
    assert out["fired"][0]["fire_ms"] == fire
    assert eng._bt.algo.seen == [(datetime(2026, 8, 24, 15, 59), 111.11)]
    assert eng.next_fire_ms() is None, "nothing else scheduled today"
    # the candle ending 15:59:00 arrives at 15:59:02
    eng.advance(fire + 2_000, D, bars=_push(day, 389, eng.pushed_state()["TQQQ"]))
    assert len(eng._bt.algo.seen) == 1
    assert eng._bt.algo._prices["TQQQ"] == pytest.approx(float(day.close[388]))
    eng.advance(CLOSE_MS + 60_000, D, bars=_push(day, 390, eng.pushed_state()["TQQQ"]))
    eng.end_session(D)
    assert eng.dead is None


def test_a_zero_or_negative_last_is_unpriced_and_the_bar_driven_fill_still_happens():
    """A finite 0.0 passed every filter and turned the rebalance into a
    silent no-op (set_holdings returns None on price <= 0) that the bar
    never retried. Such a symbol keeps its held close; the fills must equal
    the bar-driven reference exactly."""
    ref = _engine()
    for k in (4, 5, 6):
        ref.advance(OPEN_MS + k * 60_000, D, bars=_push(FULL, k, ref.pushed_state().get("TQQQ", -1)))
    ref.end_session(D)

    for bad in (0.0, -1.0, float("nan"), float("inf"), True, "12.5"):
        eng = _engine()
        _open_through_0934(eng)
        out = eng.prime(FIRE_MS + 10, D, {"TQQQ": {"last": bad, "at_ms": 1}})
        assert out["priced"] == 0 and out["unpriced"] == ["TQQQ"], bad
        assert eng._bt.algo.seen[0][1] == 103.0, "held close, not the bad quote"
        assert eng._bt.algo._prices["TQQQ"] == 103.0
        eng.advance(FIRE_MS + 2_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
        eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
        eng.end_session(D)
        assert eng.dead is None
        # the order was placed at the held close (103) instead of the bar
        # close (104): quantity may differ by rounding, but an order MUST exist
        assert len(_fills(eng.snapshot())) == len(_fills(ref.snapshot())) == 1, bad


LIMIT_CODE = CODE.replace('self.set_holdings("TQQQ", 0.5)',
                          'self.limit_order("TQQQ", 2, 104.5)')


def test_a_limit_order_from_a_primed_callback_fills_on_the_same_bar_as_the_batch_walk():
    """The primed order rests stamped (D, 09:35) and the bar ending 09:35
    lands afterwards; pass 1's gated exclusion must defer it exactly as the
    batch walk's pass 2 does, so both fill on the 09:36 bar."""
    ref = _engine(LIMIT_CODE)
    for k in (4, 5, 6):
        ref.advance(OPEN_MS + k * 60_000, D, bars=_push(FULL, k, ref.pushed_state().get("TQQQ", -1)))
    ref.end_session(D)

    eng = _engine(LIMIT_CODE)
    _open_through_0934(eng)
    eng.prime(FIRE_MS + 10, D, _q(104.0))
    eng.advance(FIRE_MS + 2_000, D, bars=_push(FULL, 5, eng.pushed_state()["TQQQ"]))
    eng.advance(OPEN_MS + 6 * 60_000, D, bars=_push(FULL, 6, eng.pushed_state()["TQQQ"]))
    eng.end_session(D)

    assert _fills(eng.snapshot()) == _fills(ref.snapshot())
    assert _fills(eng.snapshot()), "the limit must have filled in both walks"
