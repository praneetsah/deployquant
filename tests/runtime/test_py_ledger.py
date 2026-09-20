"""Broker truth outranks the model, in the python order book.

Three-valued, exactly as ir_engine/engine.py:_fill:

    rows  -> apply the BROKER's fills
    []    -> the broker affirmatively did not fill: apply nothing
    None  -> UNKNOWN: keep the model fill, marked unconfirmed

Conflating [] with None is how an API outage becomes a duplicate position.
Conflating either with "no ledger" is how a real fill gets dropped.

The buying-power ordering is the subtle one and it is tested first, because
it is the defect that porting this naively would have introduced: ir_engine
has no BP check inside _fill at all (it checks at the ORDER site), while the
python book checked BP before it had consulted anyone. A broker fill that
the model thinks it cannot afford is still a broker fill — the venue already
accepted the risk, the shares are already in the account, and rejecting it
leaves the sleeve flat while the account is long. The next sweep then sells
real shares to "correct" a position the model invented its way out of.
"""
from datetime import date

import pytest

from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill
from dqengine.runtime.enums import OrderStatus
from dqengine.runtime.identity import intent_id

from conftest_helpers import make_book


DAY = date(2026, 9, 4)


@pytest.fixture
def book_with_sleeve():
    return make_book(DAY)


def _ledger(rows, reconciled_from=DAY, unknown=()):
    return ExecutionLedger(rows, unknown=unknown, reconciled_from=reconciled_from)


def _fill_row(qty, px, order_id=None, sym="TQQQ", fees=0.0):
    return LedgerFill(day=DAY, time_ms=10 * 3600_000, symbol=sym, qty=qty,
                      price=px, fees=fees,
                      rule_tag=intent_id(sym, order_id) if order_id else None)


def test_a_broker_fill_is_applied_even_when_the_model_cannot_afford_it(book_with_sleeve):
    """The venue already accepted the risk and the shares are in the account.
    Rejecting the fill leaves the sleeve flat while the broker is long."""
    book, sleeve = book_with_sleeve
    book.ledger = _ledger([_fill_row(100, 50.12, order_id=1)])
    sleeve.cash = 10.0                                    # nowhere near enough
    t = book.market("TQQQ", 100, 50.0, tag="entry")

    assert sleeve.qty.get("TQQQ") == 100
    assert not any(o.status == "rejected" for o in sleeve.orders)
    assert t.status == OrderStatus.FILLED


def test_the_broker_price_wins_over_the_model_price(book_with_sleeve):
    book, sleeve = book_with_sleeve
    book.ledger = _ledger([_fill_row(100, 50.12, order_id=1)])
    book.market("TQQQ", 100, 50.0, tag="entry")
    assert sleeve.fills[-1].price == pytest.approx(50.12)


def test_a_broker_fee_is_debited_from_sleeve_cash(book_with_sleeve):
    """The commission on a broker fill reaches the sleeve, not just the Fill.

    `Sleeve.apply_fill` is `cash -= qty * price + fees`
    (dqengine/runtime/core/portfolio.py), and the order book passes `fees=lf.fees`
    when it adopts a ledger row. Both halves have to hold: a book that
    dropped the fee on the way through would leave this assertion short by
    exactly the commission, and the default fee model returns 0.0, so the
    `+ fees` term is reachable only via this ledger path -- which is the
    live ENFORCE path, carrying real broker commissions into reported cash
    and equity -- or via a user's own fee model.

    Added in Phase 3 Task 9: the deleted test_ledger_engine.py asserted this
    against the IR engine and nothing asserted it against this one. (The
    arithmetic inside apply_fill is covered directly by
    api/tests/test_portfolio.py::test_fees_are_debited_from_cash; what was
    missing, and is pinned here, is that the book PROPAGATES the fee.)
    """
    book, sleeve = book_with_sleeve
    start = sleeve.cash
    book.ledger = _ledger([_fill_row(100, 50.12, order_id=1, fees=0.51)])
    book.market("TQQQ", 100, 50.0, tag="entry")

    assert sleeve.fills[-1].fees == pytest.approx(0.51)
    assert round(start - sleeve.cash, 2) == round(100 * 50.12 + 0.51, 2)


def test_a_pumped_broker_fee_is_debited_too(book_with_sleeve):
    """The same for the fill pump, the other site that adopts a ledger row.

    A take-profit that filled at the broker on a wick our bars never printed
    still cost a commission; the pump passes `fees=lf.fees` at the third
    apply_fill call site and this is what says so.
    """
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    start = sleeve.cash
    t = book.limit("TQQQ", -100, 60.0, tag="tp")
    tag = intent_id("TQQQ", t.order_id, "tp")
    book.ledger = _ledger([LedgerFill(day=DAY, time_ms=9 * 3600_000 + 59_000,
                                      symbol="TQQQ", qty=-100, price=60.02,
                                      fees=0.37, rule_tag=tag,
                                      broker_order_id="b1")])
    book.check_resting("TQQQ", o=57.0, h=58.0, l=56.0, c=57.5)

    assert sleeve.qty["TQQQ"] == 0
    assert sleeve.fills[-1].fees == pytest.approx(0.37)
    # a SELL adds proceeds and still pays the commission
    assert round(sleeve.cash - start, 2) == round(100 * 60.02 - 0.37, 2)


def test_a_confirmed_no_fill_applies_nothing(book_with_sleeve):
    """`[]` means the broker says it did not fill. Written as == [] in the
    implementation for the same reason it is asserted separately here."""
    book, sleeve = book_with_sleeve
    book.ledger = _ledger([])
    book.market("TQQQ", 100, 50.0, tag="entry")
    assert sleeve.qty.get("TQQQ", 0) == 0
    assert sleeve.fills == []


def test_unknown_keeps_the_model_fill_marked_unconfirmed(book_with_sleeve):
    """No ledger data for this day is NOT a no-fill. Keep the model's fill so
    the replay does not conclude it is flat and buy the position again."""
    book, sleeve = book_with_sleeve
    book.ledger = _ledger([], reconciled_from=None)       # before the window
    book.market("TQQQ", 100, 50.0, tag="entry")
    assert sleeve.qty.get("TQQQ") == 100
    assert sleeve.fills[-1].confirmed is False


def test_without_a_ledger_the_model_fills_and_buying_power_still_binds(book_with_sleeve):
    """No ledger = backtest. The BP check must still reject there, or every
    backtest silently gains infinite leverage."""
    book, sleeve = book_with_sleeve
    book.ledger = None
    sleeve.cash = 10.0
    book.market("TQQQ", 100, 50.0, tag="entry")
    assert sleeve.qty.get("TQQQ", 0) == 0
    assert any(o.status == "rejected" for o in sleeve.orders)


def test_buying_power_still_binds_on_the_unknown_branch(book_with_sleeve):
    """UNKNOWN keeps the MODEL's fill, so the model's own limits apply."""
    book, sleeve = book_with_sleeve
    book.ledger = _ledger([], reconciled_from=None)
    sleeve.cash = 10.0
    book.market("TQQQ", 100, 50.0, tag="entry")
    assert sleeve.qty.get("TQQQ", 0) == 0


def test_a_resting_order_survives_a_confirmed_no_fill(book_with_sleeve):
    """The most dangerous bookkeeping in the engine: a stop the broker says
    did not fill is still LIVE at the broker. Tearing the ticket down here
    would drop a real protective order off a real position."""
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    t = book.stop_market("TQQQ", -100, 90.0, tag="stop")
    book.ledger = _ledger([])
    book.check_resting("TQQQ", o=95.0, h=96.0, l=89.0, c=91.0)   # breached
    assert t.is_open(), "a confirmed no-fill must leave the order resting"
    assert sleeve.qty["TQQQ"] == 100


def test_an_empty_tag_is_not_passed_to_the_ledger_as_a_rule(book_with_sleeve):
    """take() matches rule tags with ==, so "" would match no tagged row and
    fall through to pool order. ir_engine states the same rule: never ""."""
    book, sleeve = book_with_sleeve
    seen = []

    class Spy:
        reconciled_from = DAY

        def take(self, day, symbol, rule_id=None):
            seen.append(rule_id)
            return None

    book.ledger = Spy()
    book.market("TQQQ", 10, 50.0, tag="")
    assert seen and seen[0] is not None and seen[0] != ""


def test_a_market_order_the_broker_did_not_fill_is_closed_not_left_hanging(
        book_with_sleeve):
    """A market order never rests, so a confirmed no-fill would leave it NEW
    forever — and user code waiting on ticket.status or on_order_event would
    wait forever with it. Close it loudly instead.

    A RESTING order is the opposite case and must stay open (tested above):
    it is still live at the broker."""
    book, sleeve = book_with_sleeve
    events = []
    book.events_out = events.append
    book.ledger = _ledger([])

    t = book.market("TQQQ", 100, 50.0, tag="entry")

    assert t.status == OrderStatus.CANCELED
    assert not t.is_open()
    assert any(e.status == OrderStatus.CANCELED for e in events)
    assert any("no fill" in (e.message or "") for e in events)


def test_the_ir_tag_prefix_is_reserved_from_user_code(book_with_sleeve):
    """`ir:` carries an order's IR rule id as its live identity. A user
    tagging two orders `ir:tp` would give them ONE identity, hence one cid
    prefix — and the executor would cancel and resubmit them against each
    other every sweep, which is the churn the identity work removed."""
    from dqengine.runtime.errors import UnsupportedApiError

    book, _ = book_with_sleeve
    with pytest.raises(UnsupportedApiError, match="reserved"):
        book.market("TQQQ", 1, 50.0, tag="ir:tp")


def test_generated_code_may_use_the_prefix(book_with_sleeve):
    book, _ = book_with_sleeve
    book.generated = True
    t = book.market("TQQQ", 1, 50.0, tag="ir:tiered-target")
    assert t.tag == "ir:tiered-target"


# ------------------------------------------------------------ the fill pump

def test_a_broker_fill_of_a_resting_order_is_adopted_before_the_bar_cross(book_with_sleeve):
    """The take-profit filled at the broker on a wick our bar never printed.
    Without the pump the model stays long, want > have, and the executor
    BUYS BACK what the broker just sold."""
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    t = book.limit("TQQQ", -100, 60.0, tag="tp")            # rests at 60
    tag = intent_id("TQQQ", t.order_id, "tp")
    book.ledger = _ledger([LedgerFill(day=DAY, time_ms=9 * 3600_000 + 59_000,
                                      symbol="TQQQ", qty=-100, price=60.02,
                                      rule_tag=tag, broker_order_id="b1")])
    # the bar's high (58) never reaches 60 -- the simulation would NOT fill
    book.check_resting("TQQQ", o=57.0, h=58.0, l=56.0, c=57.5)
    assert sleeve.qty["TQQQ"] == 0
    assert not t.is_open()
    assert sleeve.fills[-1].price == pytest.approx(60.02)


def test_the_pump_never_steals_an_untagged_row(book_with_sleeve):
    """Tag match is strict: a manual trade or a netted market delta on the
    same symbol is NOT this ticket's fill."""
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    t = book.limit("TQQQ", -100, 60.0, tag="tp")
    book.ledger = _ledger([LedgerFill(day=DAY, time_ms=9 * 3600_000 + 59_000,
                                      symbol="TQQQ", qty=-100, price=60.02,
                                      rule_tag=None)])
    book.check_resting("TQQQ", o=57.0, h=58.0, l=56.0, c=57.5)
    assert t.is_open() and sleeve.qty["TQQQ"] == 100


def test_a_row_after_the_current_bar_is_not_adopted_yet(book_with_sleeve):
    """upto_ms gates the read so a full-day replay applies the fill at the
    same bar a live step did -- warm-vs-replay equivalence depends on it."""
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    t = book.limit("TQQQ", -100, 60.0, tag="tp")
    tag = intent_id("TQQQ", t.order_id, "tp")
    later = 11 * 3600_000                                  # book clock is 10:00
    book.ledger = _ledger([LedgerFill(day=DAY, time_ms=later, symbol="TQQQ",
                                      qty=-100, price=60.02, rule_tag=tag)])
    book.check_resting("TQQQ", o=57.0, h=58.0, l=56.0, c=57.5)
    assert t.is_open()


def test_a_pumped_row_cannot_be_taken_again_by_the_bar_cross(book_with_sleeve):
    """Consumption shares _taken with take(): the same row must not be
    applied twice if the bar later crosses the level too."""
    book, sleeve = book_with_sleeve
    sleeve.qty["TQQQ"] = 100
    t = book.limit("TQQQ", -100, 60.0, tag="tp")
    tag = intent_id("TQQQ", t.order_id, "tp")
    book.ledger = _ledger([LedgerFill(day=DAY, time_ms=9 * 3600_000 + 59_000,
                                      symbol="TQQQ", qty=-100, price=60.02,
                                      rule_tag=tag, broker_order_id="b1")])
    book.check_resting("TQQQ", o=57.0, h=58.0, l=56.0, c=57.5)   # pumped
    book.check_resting("TQQQ", o=61.0, h=62.0, l=60.5, c=61.0)   # crosses now
    assert sleeve.qty["TQQQ"] == 0
    assert len([f for f in sleeve.fills if f.symbol == "TQQQ"]) == 1


# ------------------------------------------- the rebalance's on_flat gate

class _FakeCtx:
    """The half of BlockContext a rebalance reads: a fixed target vector
    and the marked prices. The fill decision — the thing under test — comes
    from the REAL OrderBook and the REAL ledger below, not from here."""

    def __init__(self, weights, prices):
        self._weights = weights
        self._params = {}
        self.symbols = sorted(prices)
        self.ind = type("I", (), {"last_price": dict(prices)})()
        self.weights = None

    def ensure_warm(self):
        pass

    def target_weights(self, node, day):
        return dict(self._weights)


class _BookAlgo:
    """A QCAlgorithm surface just wide enough for Allocator.rebalance,
    wired to a real book so market_order really consults the ledger."""

    def __init__(self, book, sleeve, prices):
        self._book, self._prices = book, prices
        self.portfolio = type("P", (), {"_sleeve": sleeve})()
        self.transactions = type("T", (), {
            "get_open_order_tickets": staticmethod(lambda *a: [])})()

    def log(self, msg):
        pass

    def market_order(self, sym, qty, tag=""):
        return self._book.market(sym, int(qty), self._prices[sym], tag=tag)


def _allocator(book, sleeve, weights, prices):
    from dqengine.runtime.allocation import Allocator
    algo = _BookAlgo(book, sleeve, prices)
    return Allocator(algo, {}, {}, sorted(prices),
                     ctx=_FakeCtx(weights, prices))


def _rebalance(sleeve_cash, weights, prices, ledger=None, held=None, day=DAY):
    from dqengine.runtime.core.portfolio import Sleeve
    from dqengine.runtime.orders import OrderBook
    sleeve = Sleeve(cash=sleeve_cash)
    for s, q in (held or {}).items():
        sleeve.qty[s] = q
    book = OrderBook(sleeve, clock=lambda: (day, 10 * 3600_000),
                     events_out=lambda e: None, prices=dict(prices))
    book.ledger = ledger
    flat = []
    _allocator(book, sleeve, weights, prices).rebalance(
        day, tag="reb", on_flat=flat.append)
    return sleeve, flat


def test_a_rebalance_buy_the_broker_did_not_fill_does_not_release_the_guard():
    """ir_engine gates the identical test on `if self._fill(...)`
    (_rebalance_to), with a comment naming this case: on a CONFIRMED no-fill
    of a BUY delta from flat the quantity is still 0, so a bare `qty == 0`
    check reads the failed buy as a position that just closed. It would
    release the once_per=position guards of a rebalance that never happened
    and let the rule re-enter with real shares."""
    sleeve, flat = _rebalance(10_000.0, {"TQQQ": 1.0}, {"TQQQ": 50.0},
                              ledger=_ledger([]))
    assert sleeve.qty.get("TQQQ", 0) == 0        # the broker filled nothing
    assert flat == [], "a no-fill was read as a closed position"


def test_a_rebalance_buy_rejected_for_buying_power_does_not_release_it_either():
    """The backtest half of the same shape, and no ledger needed: a sleeve
    already fully invested (cash 0, $10k of SPY at 1.0x, so buying power is
    0) against a target vector that asks for another $5k of TQQQ. The order
    is rejected, TQQQ stays flat, and nothing about it "just closed"."""
    sleeve, flat = _rebalance(0.0, {"SPY": 1.0, "TQQQ": 0.5},
                              {"SPY": 100.0, "TQQQ": 50.0},
                              held={"SPY": 100})
    assert sleeve.qty.get("TQQQ", 0) == 0
    assert any(o.status == "rejected" for o in sleeve.orders)
    assert flat == []


def test_a_rebalance_that_really_sells_a_symbol_out_still_releases_it():
    """The signal the gate must not swallow: a sell that FILLS and takes the
    symbol to zero is the IR engine's _on_flat site, and the once_per
    =position guards bound to it have to be released there."""
    sleeve, flat = _rebalance(0.0, {"SPY": 1.0}, {"TQQQ": 50.0, "SPY": 100.0},
                              held={"TQQQ": 100})
    assert sleeve.qty.get("TQQQ", 0) == 0
    assert flat == ["TQQQ"], flat
