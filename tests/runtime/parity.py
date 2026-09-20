"""One IR, one engine, and a frozen golden fixture.

Phase 2 proved every case here against the IR engine, fill for fill and
equity to the cent, and froze the RESULT: `golden/<name>.py` is the source
the generator emitted and `golden/<name>.json` is the (day, ms, symbol, qty,
price) fill digest plus the daily equity curve. Note which side of that
comparison the digest came from -- `assert_parity` always froze the PYTHON
engine's output, never the oracle's -- so the fixtures were self-sufficient
from the day they were written and Phase 3's change here is subtractive: the
oracle half of the comparison goes, the case itself still runs end to end on
real bars and still has to land on the same fills.

What this can and cannot catch, stated plainly so nobody over-trusts it:
it catches any change that makes an existing strategy trade differently,
which is the failure that costs money. It cannot adjudicate a NEW shape --
there is no second implementation left to ask. A new codegen feature needs
a LEAN comparison, a hand-computed expectation, or a golden a human read.

    UPDATE_GOLDEN=1 pytest tests/runtime/test_codegen_sells.py

rewrites the fixtures. Without it, drift is a failure and a MISSING fixture
is a failure -- see _freeze.
"""
import hashlib
import json
import os

from dqengine import codegen
from dqengine.runtime import run_python_backtest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
from dqengine.config import data_root                                  # noqa: E402
DATA = data_root()
GOLDEN = os.path.join(HERE, "golden")

# Every golden name assert_golden was actually called with this session.
#
# test_golden_inventory pins the fixture LIST in both directions, which
# catches a golden with no pinned name -- but not a golden whose test case
# was deleted while the fixture and the name stayed. That is the one hole
# that leaves 48 files, 48 names and a green suite with nothing replaying
# them. test_every_pinned_golden_was_actually_exercised reads this set at
# the end of a full run and compares it to GOLDEN_NAMES.
CALLED: set[str] = set()


def _update() -> bool:
    return os.environ.get("UPDATE_GOLDEN") == "1"


def generate(ir, margin=1.0):
    return codegen.generate_python(ir, margin_max=margin)


def run_py(code, start, end, cash, data, store, bar_ms, cash_events,
           ledger=None):
    ov = {"start": start, "end": end, "cash": cash}
    if store is not None:
        ov["store"] = store
    if bar_ms:
        ov["bar_ms"] = bar_ms
    if cash_events:
        ov["cash_events"] = list(cash_events)
    if ledger is not None:
        ov["ledger"] = ledger
    return run_python_backtest(code, data_root=data, overrides=ov)


def _digest(fills, equity_days, equity):
    return {
        "fills": [list(f) for f in fills],
        "equity_days": list(equity_days),
        "equity": [round(v, 2) for v in equity],
    }


def _canonical(fills):
    """Sort a rebalance's same-instant fills by symbol.

    For the ONE thing a golden cannot pin: when two symbols take the same
    integer delta, `Allocator.rebalance` submits them in `set` iteration
    order -- Python's per-process string hash order (Hazard 5, pinned by
    test_the_rebalance_delta_tie_break_is_still_hash_ordered). The
    SEQUENCE of those orders therefore differs between two processes, so a
    frozen fill list that kept it would fail on a different PYTHONHASHSEED
    and prove nothing about the strategy.

    Measured on 2026-09-15 across seeds 1 and 777: the `inverse_vol` shape
    reorders, and the set of (day, ms, symbol, qty, price) does NOT -- so
    canonicalising the sequence loses the sequence and nothing else. Every
    fill, every field and the whole equity curve are still compared.

    Used only where equal deltas actually occur (the wt_* weight-shape
    cases). The other 52 cases keep submission order in their digest,
    which is why none of their fixtures move."""
    return sorted(fills, key=lambda f: (f[0], f[1], f[2]))


def _freeze(name, code, digest):
    """Compare against the committed fixture, or write it under
    UPDATE_GOLDEN=1. NEVER write it just because it is absent.

    That auto-create branch was harmless while the IR engine ran first and
    had to agree: a regenerated fixture had still passed a second engine.
    Now that this function is the only check, a fixture that is missing --
    deleted, renamed, never committed, dropped by a bad merge -- would be
    rewritten from whatever the generator happens to emit today and the test
    would pass having proved nothing. Fail instead.
    """
    src_path = os.path.join(GOLDEN, f"{name}.py")
    dig_path = os.path.join(GOLDEN, f"{name}.json")
    if _update():
        os.makedirs(GOLDEN, exist_ok=True)
        with open(src_path, "w") as fh:
            fh.write(code)
        with open(dig_path, "w") as fh:
            json.dump(digest, fh, indent=1, sort_keys=True)
        return
    for path in (src_path, dig_path):
        assert os.path.exists(path), (
            f"golden fixture {os.path.basename(path)!r} is missing. This is "
            f"NOT a first run: every case in this suite has a committed "
            f"fixture (tests/runtime/test_golden_inventory.py pins the "
            f"list). Restore it from git. Only if the case is genuinely new "
            f"and you intend to create it: UPDATE_GOLDEN=1 pytest <file>, "
            f"then READ the generated source before committing it.")
    with open(src_path) as fh:
        want_src = fh.read()
    assert code == want_src, (
        f"generated source for {name!r} drifted from its golden fixture.\n"
        f"old sha={hashlib.sha256(want_src.encode()).hexdigest()[:12]} "
        f"new sha={hashlib.sha256(code.encode()).hexdigest()[:12]}\n"
        f"If the change is intended: UPDATE_GOLDEN=1 pytest <this file> "
        f"and READ the diff before committing it.")
    with open(dig_path) as fh:
        want = json.load(fh)
    assert digest == want, f"fills/equity for {name!r} drifted from golden"


def assert_golden(ir, *, start, end, name, cash=10000.0, margin=1.0,
                  data=DATA, store=None, bar_ms=None, cash_events=None,
                  ledger=None, canonical_fill_order=False):
    """Generate `ir`, run it over [start, end] on dqengine.runtime, and assert
    every fill and every daily equity value against `golden/<name>.json`
    and the source against `golden/<name>.py`.

    The digest is compared BEFORE it is written in the UPDATE_GOLDEN case
    only by virtue of _freeze's branch order; in the normal case nothing is
    written at all. Returns the run result so callers can assert more.

    `ledger` is the adoption shape: an ExecutionLedger / LiveCappedLedger of
    BROKER fills that the replay must take instead of its own. It rides the
    same `overrides` dict as `store` and `cash_events` so the four `led_*`
    cases freeze through exactly this path and not a hand-rolled one.

    `canonical_fill_order` sorts same-instant fills by symbol before
    freezing -- see _canonical, and read it before turning it on.
    """
    CALLED.add(name)
    code = generate(ir, margin)
    got = run_py(code, start, end, cash, data, store, bar_ms, cash_events,
                 ledger)
    assert "error" not in got, got.get("error", {}).get(
        "traceback", got.get("error"))
    fills = [(f["day"], f["ms"], f["sym"], int(f["qty"]), f["px"])
             for f in got["fills"]]
    if canonical_fill_order:
        fills = _canonical(fills)
    _freeze(name, code, _digest(fills, got["equity_days"], got["equity"]))
    return got
