from datetime import date

from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill


def _f(d, sym, qty, px, ms=34301000, tag=None, fees=0.0, boid=None):
    return LedgerFill(day=d, time_ms=ms, symbol=sym, qty=qty, price=px,
                      fees=fees, rule_tag=tag, broker_order_id=boid)


D = date(2026, 8, 24)


def test_take_returns_the_days_fill_for_that_symbol():
    led = ExecutionLedger([_f(D, "TQQQ", 77, 69.35)], reconciled_from=D)
    got = led.take(D, "TQQQ", "weekly-entry")
    assert [(g.qty, g.price) for g in got] == [(77, 69.35)]


def test_take_consumes_so_a_second_call_gets_the_next_fill():
    """A second call gets the next fill only when the two rows are two
    separate ORDERS -- two rule fires on one day. Two rows of a single
    order are one partial fill and belong to one take (see
    test_take_consumes_every_execution_of_one_order below); locking the
    one-row-per-call behaviour in generally is what stranded the remainder
    of a partially filled order forever."""
    led = ExecutionLedger([_f(D, "TQQQ", 40, 69.35, boid="o1"),
                           _f(D, "TQQQ", 37, 69.36, boid="o2")],
                          reconciled_from=D)
    assert [g.qty for g in led.take(D, "TQQQ", "r1")] == [40]
    assert [g.qty for g in led.take(D, "TQQQ", "r1")] == [37]


def test_rule_tagged_rows_are_preferred_over_untagged():
    led = ExecutionLedger([_f(D, "TQQQ", 10, 1.0),
                           _f(D, "TQQQ", 20, 2.0, tag="exit-stop")],
                          reconciled_from=D)
    assert [g.qty for g in led.take(D, "TQQQ", "exit-stop")] == [20]


def test_no_row_on_a_reconciled_day_means_broker_confirmed_no_fill():
    led = ExecutionLedger([], reconciled_from=D)
    assert led.take(D, "TQQQ", "weekly-entry") == []


def test_unknown_symbol_returns_none_not_empty():
    led = ExecutionLedger([], unknown={"TQQQ"}, reconciled_from=D)
    assert led.take(D, "TQQQ", "weekly-entry") is None


def test_days_before_reconciled_from_are_unknown():
    led = ExecutionLedger([], reconciled_from=date(2026, 8, 20))
    assert led.take(date(2026, 8, 19), "TQQQ", "r") is None
    assert led.take(date(2026, 8, 20), "TQQQ", "r") == []


def test_leftovers_reports_unclaimed_fills():
    led = ExecutionLedger([_f(D, "TQQQ", 77, 69.35), _f(D, "SPY", 5, 500.0)],
                          reconciled_from=D)
    led.take(D, "TQQQ", "r1")
    assert [g.symbol for g in led.leftovers()] == ["SPY"]


# --- C2: one take consumes every execution of ONE order -------------------

def test_take_consumes_every_execution_of_one_order():
    """Spec §4 makes rows per-execution precisely so a partial fill survives
    as two rows; §6 says the engine consumes matching rows (PLURAL) in
    filled_at order. An order Alpaca reports as two executions must apply in
    full -- taking only the first permanently understates a real holding,
    and the executor then sells the remainder it thinks it does not own."""
    led = ExecutionLedger([_f(D, "TQQQ", 40, 69.35, boid="o1"),
                           _f(D, "TQQQ", 37, 69.36, ms=34302000, boid="o1")],
                          reconciled_from=D)
    got = led.take(D, "TQQQ", "weekly-entry")
    assert [(g.qty, g.price) for g in got] == [(40, 69.35), (37, 69.36)]
    assert led.leftovers() == []


def test_partial_fill_falls_back_to_rule_tag_when_no_order_id():
    """Rows without a broker order id group on the chosen row's rule tag --
    and only on it, so a different rule's fills that day stay put."""
    led = ExecutionLedger([_f(D, "TQQQ", 40, 1.0, tag="entry"),
                           _f(D, "TQQQ", 37, 2.0, ms=34302000, tag="entry"),
                           _f(D, "TQQQ", 5, 3.0, ms=34303000, tag="exit")],
                          reconciled_from=D)
    assert [g.qty for g in led.take(D, "TQQQ", "entry")] == [40, 37]
    assert [g.qty for g in led.take(D, "TQQQ", "exit")] == [5]


def test_untagged_rows_with_no_order_id_are_taken_one_at_a_time():
    """Nothing to group on: take one, so a second rule's fill that day is
    not swallowed. Over-consuming is as wrong as under-consuming."""
    led = ExecutionLedger([_f(D, "TQQQ", 40, 1.0),
                           _f(D, "TQQQ", 37, 2.0, ms=34302000)],
                          reconciled_from=D)
    assert [g.qty for g in led.take(D, "TQQQ", "r1")] == [40]
    assert [g.qty for g in led.take(D, "TQQQ", "r1")] == [37]


# --- minor: symbol case is normalized once, here --------------------------

def test_symbol_matching_is_case_insensitive():
    """normalize_execution uppercases and _unknown_symbols uppercases, but a
    lowercase-universe IR reaches take() raw. Matching nothing would read as
    a confirmed no-fill -- the exact inversion this module exists to
    prevent."""
    led = ExecutionLedger([_f(D, "TQQQ", 77, 69.35)], reconciled_from=D)
    assert [g.qty for g in led.take(D, "tqqq", "r")] == [77]
    led2 = ExecutionLedger([_f(D, "tqqq", 77, 69.35)], reconciled_from=D)
    assert [g.qty for g in led2.take(D, "TQQQ", "r")] == [77]
    assert led2.leftovers() == []
    led3 = ExecutionLedger([], unknown={"tqqq"}, reconciled_from=D)
    assert led3.take(D, "TQQQ", "r") is None


# --------------------------------------------------------------- live cap

def test_capped_ledger_demotes_no_fill_to_unknown_on_live_days():
    """On a live day the sweep has not run yet when the engine steps, so an
    absent row means "in flight", never "the broker declined"."""
    from dqengine.runtime.core.ledger import LiveCappedLedger
    led = LiveCappedLedger(ExecutionLedger([], reconciled_from=D),
                           live_from=D)
    assert led.take(D, "TQQQ", "weekly-entry") is None


def test_capped_ledger_keeps_batch_semantics_on_past_days():
    from dqengine.runtime.core.ledger import LiveCappedLedger
    past = date(2026, 8, 21)
    led = LiveCappedLedger(
        ExecutionLedger([_f(past, "REW", 154, 11.93)], reconciled_from=past),
        live_from=D)
    assert [(g.qty, g.price) for g in led.take(past, "REW", "r1")] \
        == [(154, 11.93)]
    # consumed -> a second take on the past day is a REAL confirmed no-fill
    assert led.take(past, "REW", "r2") == []


def test_capped_ledger_applies_existing_rows_on_live_days():
    """A row that IS present on the live day (ingested before a rebuild's
    catch-up steps) is broker truth and applies normally."""
    from dqengine.runtime.core.ledger import LiveCappedLedger
    led = LiveCappedLedger(
        ExecutionLedger([_f(D, "TQQQ", 77, 69.35)], reconciled_from=D),
        live_from=D)
    assert [(g.qty, g.price) for g in led.take(D, "TQQQ", "weekly-entry")] \
        == [(77, 69.35)]
    # ...and once consumed, absence on the live day is back to UNKNOWN
    assert led.take(D, "TQQQ", "other-rule") is None


def test_capped_ledger_delegates_attributes_and_leftovers():
    from dqengine.runtime.core.ledger import LiveCappedLedger
    inner = ExecutionLedger([_f(D, "TQQQ", 77, 69.35)], unknown={"spy"},
                            reconciled_from=D)
    led = LiveCappedLedger(inner, live_from=D)
    assert led.unknown == {"SPY"}
    assert led.reconciled_from == D
    assert [f.qty for f in led.leftovers()] == [77]
    # unknown symbols stay None regardless of the cap
    assert led.take(D, "SPY", None) is None


# ------------------------------------------------- lean-fills: presence/day

def test_presence_beats_unknown():
    """The 2026-08-25 incident's D1: a poll hiccup put TQQQ in the unknown
    set and HID yesterday's confirmed row, flapping the live basis. Rows
    are truth; unknown may only demote absence."""
    led = ExecutionLedger([_f(D, "TQQQ", 77, 69.35)], unknown={"TQQQ"},
                          reconciled_from=D)
    assert [(g.qty, g.price) for g in led.take(D, "TQQQ", "weekly-entry")] \
        == [(77, 69.35)]


def test_unknown_from_scopes_absence_to_recent_days():
    later = date(2026, 8, 25)
    led = ExecutionLedger([], unknown={"TQQQ"}, reconciled_from=D,
                          unknown_from=later)
    assert led.take(D, "TQQQ", "r") == [], \
        "a settled day's absence is a REAL no-fill even mid-outage"
    assert led.take(later, "TQQQ", "r") is None, \
        "today's absence is unknowable during the outage"


def test_unknown_from_none_keeps_everywhere_unknown():
    led = ExecutionLedger([], unknown={"TQQQ"}, reconciled_from=D)
    assert led.take(D, "TQQQ", "r") is None


def test_take_rule_fill_strict_tag_and_time_gate():
    rows = [_f(D, "TQQQ", -77, 70.99, ms=34496000, tag="tiered-target",
               boid="o1"),
            _f(D, "TQQQ", 77, 70.975, ms=34503000, tag=None, boid="o2")]
    led = ExecutionLedger(rows, reconciled_from=D)
    from dqengine.runtime.core.ledger import LiveCappedLedger
    led = LiveCappedLedger(led, live_from=D)      # pump reads pass through
    # bar closing 09:34 (34440000): fill at 09:34:56 not visible yet
    assert led.take_rule_fill(D, "TQQQ", "tiered-target", 34440000) is None
    # bar closing 09:35 (34500000): 34496000 < 34500000 -> visible
    got = led.take_rule_fill(D, "TQQQ", "tiered-target", 34500000)
    assert [(g.qty, g.price) for g in got] == [(-77, 70.99)]
    # strict tag: the untagged market-delta row is never pump-consumed
    assert led.take_rule_fill(D, "TQQQ", "tiered-target", 99999999) is None
    assert led.take_rule_fill(D, "TQQQ", None, 99999999) is None
    assert [f.qty for f in led.leftovers()] == [77]


def test_take_rule_fill_shares_consumption_with_take():
    rows = [_f(D, "TQQQ", -77, 70.99, tag="tiered-target", boid="o1")]
    led = ExecutionLedger(rows, reconciled_from=D)
    assert led.take_rule_fill(D, "TQQQ", "tiered-target", 99999999)
    # the rule's own later bar-cross finds the row already consumed --
    # absence, and (settled day, no unknown) a confirmed no-fill
    assert led.take(D, "TQQQ", "tiered-target") == []
