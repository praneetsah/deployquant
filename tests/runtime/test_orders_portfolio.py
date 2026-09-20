from datetime import date

from dqengine.runtime.core.portfolio import Sleeve
from dqengine.runtime.enums import OrderStatus, OrderType, UpdateOrderFields
from dqengine.runtime.orders import OrderBook, Transactions
from dqengine.runtime.portfolio_view import PortfolioManager
from dqengine.runtime.symbol import Symbol


def mk():
    sleeve = Sleeve(1000.0, margin_max=1.33)
    events = []
    book = OrderBook(sleeve, clock=lambda: (date(2026, 8, 27), 10 * 3600_000),
                     events_out=events.append)
    prices = {"TQQQ": 100.0}
    return sleeve, book, events, PortfolioManager(sleeve, prices), prices


def test_market_fill_updates_sleeve_and_emits():
    sleeve, book, events, pf, prices = mk()
    t = book.market(Symbol("TQQQ"), 10, price=100.0, tag="entry")
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 100.0
    assert sleeve.qty["TQQQ"] == 10 and abs(sleeve.cash - 0.0) < 1e-9
    assert events[-1].status == OrderStatus.FILLED and events[-1].fill_quantity == 10
    assert pf["TQQQ"].invested and pf["TQQQ"].quantity == 10
    assert abs(pf.total_portfolio_value - 1000.0) < 1e-9


def test_market_reject_beyond_buying_power():
    sleeve, book, events, pf, prices = mk()
    t = book.market(Symbol("TQQQ"), 100, price=100.0, tag="too big")  # needs 10k > 1.33k
    assert t.status == OrderStatus.INVALID
    assert sleeve.qty.get("TQQQ", 0) == 0
    assert book.orders[-1].status == "rejected"


def test_resting_limit_sell_fill_rule():
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    t = book.limit(Symbol("TQQQ"), -10, limit_price=105.0, tag="tp")
    assert t.status == OrderStatus.SUBMITTED
    book.check_resting("TQQQ", o=106.0, h=107.0, l=104.0, c=106.5)
    # sell limit with high>=limit fills at max(open, limit) = 106.0
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 106.0
    assert sleeve.qty["TQQQ"] == 0


def test_ticket_update_and_cancel():
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    t = book.limit(Symbol("TQQQ"), -10, limit_price=105.0)
    t.update(UpdateOrderFields(limit_price=103.0, quantity=-5, tag="moved"))
    assert t.limit_price == 103.0 and t.quantity == -5 and t.tag == "moved"
    tx = Transactions(book)
    assert [x.order_id for x in tx.get_open_order_tickets("TQQQ")] == [t.order_id]
    tx.cancel_open_orders("TQQQ")
    assert t.status == OrderStatus.CANCELED
    assert tx.get_open_order_tickets() == []
    assert tx.get_order_by_id(t.order_id).tag == "moved"


def test_buy_limit_fill_at_min_open_limit():
    sleeve, book, events, pf, prices = mk()
    t = book.limit(Symbol("TQQQ"), 5, limit_price=99.0)
    book.check_resting("TQQQ", o=98.0, h=99.5, l=97.5, c=99.2)   # low<=99 → fill min(98,99)=98
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 98.0


def test_stop_sell_fill_at_min_open_stop():
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    t = book.stop_market(Symbol("TQQQ"), -10, stop_price=95.0)
    book.check_resting("TQQQ", o=93.0, h=96.0, l=92.0, c=94.0)   # low<=95 → fill min(93,95)=93
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 93.0


def test_stop_limit_triggers_then_limit_live_next_bar():
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    t = book.stop_limit(Symbol("TQQQ"), -10, stop_price=95.0, limit_price=94.0)
    # bar 1: stop breaches (low<95) — converts, but the limit leg is NOT live yet
    book.check_resting("TQQQ", o=93.5, h=96.0, l=92.0, c=94.0)
    assert t.status == OrderStatus.SUBMITTED
    # bar 2: limit rule (high>limit) fills at max(open, limit)
    book.check_resting("TQQQ", o=93.0, h=95.0, l=92.5, c=94.5)
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 94.0


def test_order_prices_rounded_to_cents():
    # LEAN rounds order prices to the minimum price variation; a limit at
    # 21.12525 is really a limit at 21.13 (parity-critical)
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    t = book.limit(Symbol("TQQQ"), -10, limit_price=21.12525)
    assert t.limit_price == 21.13
    t.update(UpdateOrderFields(limit_price=23.4744))
    assert t.limit_price == 23.47


def test_carried_session_defers_market_fill_to_next_real_bar():
    sleeve, book, events, pf, prices = mk()
    book.carried = True                     # data-less session
    t = book.market(Symbol("TQQQ"), 5, price=100.0, tag="deferred")
    assert t.status == OrderStatus.SUBMITTED and sleeve.qty.get("TQQQ", 0) == 0
    book.carried = False                    # next real session
    book.check_resting("TQQQ", o=101.0, h=102.0, l=100.5, c=101.5)
    assert t.status == OrderStatus.FILLED and t.average_fill_price == 101.5


def test_touch_does_not_fill():
    # strict breaches, mirroring LEAN: high == limit / low == stop -> no fill
    sleeve, book, events, pf, prices = mk()
    book.market(Symbol("TQQQ"), 10, price=100.0)
    sell = book.limit(Symbol("TQQQ"), -10, limit_price=105.0)
    book.check_resting("TQQQ", o=104.0, h=105.0, l=103.0, c=104.5)
    assert sell.status == OrderStatus.SUBMITTED
    stop = book.stop_market(Symbol("TQQQ"), -10, stop_price=95.0)
    book.check_resting("TQQQ", o=96.0, h=97.0, l=95.0, c=96.0)
    assert stop.status == OrderStatus.SUBMITTED


# ===================================================================
# The ManagedOrder dataclass itself.
# From tests/test_exits.py, whose other 37 tests drove the IR Backtester
# and went with it in Phase 3 Task 9. These four never did: they construct
# the dataclass directly out of dqengine/runtime/core/portfolio.py.
#
# Standing finding from that task: with the IR engine gone, nothing in
# shipped code CONSTRUCTS a ManagedOrder any more. The type survives as the
# declared value type of `Sleeve.targets`, which is likewise now unwritten.
# Deciding whether managed orders get re-adopted by the python engine or
# removed is a separate, deliberate change to money-path code, so the shape
# stays pinned here in the meantime rather than drifting untested.
# ===================================================================

def test_managed_order_defaults_to_limit():
    from dqengine.runtime.core.portfolio import ManagedOrder
    m = ManagedOrder("r1", "SPY", limit_price=100.0)
    assert m.kind == "limit"
    assert m.stop_price is None and m.trail_pct is None
    assert m.high_water is None and m.sibling_id is None


def test_managed_order_carries_stop_and_trail_fields():
    from dqengine.runtime.core.portfolio import ManagedOrder
    m = ManagedOrder("r1", "SPY", kind="trailing_stop", trail_pct=0.05,
                     high_water=110.0, placed_day=date(2026, 1, 2),
                     sibling_id="r1:tp")
    assert m.kind == "trailing_stop" and m.trail_pct == 0.05
    assert m.high_water == 110.0 and m.sibling_id == "r1:tp"


def test_managed_target_is_a_backcompat_alias():
    from dqengine.runtime.core.portfolio import ManagedOrder, ManagedTarget
    assert ManagedTarget is ManagedOrder


def test_positional_call_site_regression():
    """Locks the first five fields in order.

    Written against `ir_engine/engine.py`, which built these positionally:
        ManagedTarget(rule["id"], sym, round(float(px), 2), rule["id"],
                      placed_day=...)
    That call site is gone. The order is still worth pinning because a
    dataclass enforces no types at runtime, so any future re-adopter
    reordering these fields would silently bind a float into `kind`
    instead of `limit_price` — the exact failure this test was written to
    make loud.
    """
    from dqengine.runtime.core.portfolio import ManagedOrder
    d = date(2024, 1, 2)
    m = ManagedOrder("r1", "SPY", 410.0, "r1", placed_day=d)
    assert m.rule_id == "r1"
    assert m.symbol == "SPY"
    assert m.limit_price == 410.0
    assert m.tag == "r1"
    assert m.placed_day == d
    assert m.kind == "limit"  # default
