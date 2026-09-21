"""Order book for the LEAN-compatible runtime.

Single mutation point for orders and fills. The fill-price rules themselves
live in `dqengine/runtime/core/fills.py` and are CALLED, not copied — that module
is the one implementation both engines share, so a Python strategy and a
block strategy cannot fill differently. See it for the rules and why
strictness matters. Market orders fill at the current bar close (the price the caller
passes); everything else is delegated.

Order prices are rounded to the minimum price variation ($0.01), as LEAN
does. Buys beyond Sleeve.buying_power are rejected whole.
"""
from dataclasses import dataclass, field
from datetime import datetime

from dqengine.runtime.core import fills
from dqengine.runtime.core.data import close_time_ms
from dqengine.runtime.core.portfolio import OrderRecord

from .errors import UnsupportedApiError
from .identity import GENERATED_TAG_PREFIX, intent_id

from .aliases import PascalMixin, alias_methods
from .enums import OrderDirection, OrderStatus, OrderType, UpdateOrderFields


@dataclass
class OrderEvent(PascalMixin):
    order_id: int
    symbol: str
    status: OrderStatus
    direction: OrderDirection
    fill_price: float = 0.0
    fill_quantity: int = 0
    quantity: int = 0
    message: str = ""
    utc_time: datetime | None = None


@alias_methods
class OrderTicket(PascalMixin):
    def __init__(self, book, order_id, symbol, quantity, order_type, tag,
                 limit_price=None, stop_price=None):
        self._book = book
        self.order_id = order_id
        self.symbol = symbol
        self.quantity = quantity
        self.order_type = order_type
        self.tag = tag
        self.limit_price = limit_price
        self.stop_price = stop_price
        self.status = OrderStatus.NEW
        self.average_fill_price = 0.0
        self.quantity_filled = 0
        self.time = None
        self._triggered = False   # stop_limit / LIT: trigger leg touched
        self.trailing_amount = None
        self.trailing_as_percentage = True

    @property
    def direction(self) -> OrderDirection:
        return OrderDirection.BUY if self.quantity > 0 else OrderDirection.SELL

    def update(self, fields: UpdateOrderFields):
        return self._book.update(self, fields)

    def cancel(self):
        return self._book.cancel(self)

    def is_open(self) -> bool:
        return self.status in (OrderStatus.SUBMITTED, OrderStatus.UPDATE_SUBMITTED)


class OrderBook:
    def __init__(self, sleeve, clock, events_out, prices=None, securities=None,
                 ledger=None):
        self.sleeve = sleeve
        self.clock = clock                # () -> (date, time_ms)
        self.events_out = events_out      # OrderEvent -> None
        self.prices = prices if prices is not None else {}
        self.securities = securities if securities is not None else {}
        self.tickets: list[OrderTicket] = []
        # the OPEN subset, insertion-ordered — check_resting runs per bar,
        # so it must scan only what actually rests (scanning the full
        # historical list was 56% of a 5-year backtest's runtime)
        self._open: list[OrderTicket] = []
        self.next_id = 1
        self.allow_orders = True          # False during warm-up
        self.carried = False              # data-less session: defer market fills
        # Backtests only (the backtester sets it, and never on a live run). A market order placed
        # once the session's last bar has closed cannot trade at that bar's
        # price: the exchange is shut. LEAN turns it into market-on-open, and
        # so does this. Live books leave it False -- what a deployment sends a
        # broker at 16:00 is the executor's decision, not the model's.
        self.after_close_to_moo = False
        self.refused_log: list[str] = []
        self.rejections: dict[str, int] = {}   # reason -> count
        # Broker-execution ledger (live only). None = backtest: the model's
        # own fills stand. When present it OUTRANKS the model — see _fill.
        self.ledger = ledger
        # Intended fills the broker confirmed it did not make. Journalled
        # rather than dropped: guards consume on submission, so a one-shot
        # rule will not retry, and a trade that vanishes between the model
        # and the account must not do so silently.
        self.unfilled_log: list[str] = []
        # set by the runner for codegen OUTPUT, which legitimately uses the
        # reserved `ir:` tag prefix to keep a compiled block strategy's
        # order identities equal to its IR deployment's
        self.generated = False

    # ---------- helpers ----------

    def _emit(self, ticket, status, fill_price=0.0, fill_qty=0, message=""):
        self.events_out(OrderEvent(ticket.order_id, ticket.symbol, status,
                                   ticket.direction, fill_price, fill_qty,
                                   ticket.quantity, message))

    def _new_ticket(self, symbol, qty, order_type, tag, limit_price=None,
                    stop_price=None) -> OrderTicket:
        # `ir:` is RESERVED. Generated code uses it to carry the IR rule id
        # as an order's live identity (dqengine.runtime.identity.intent_id), so a
        # user tag starting with it would become that order's identity too —
        # and two orders a user tagged the same way would share one cid
        # prefix, making the executor cancel and resubmit them against each
        # other every sweep. That is exactly the churn the identity work
        # removed. Only codegen output may use the prefix.
        if tag and str(tag).startswith(GENERATED_TAG_PREFIX) \
                and not getattr(self, "generated", False):
            raise UnsupportedApiError(
                f"order tags starting with {GENERATED_TAG_PREFIX!r} are "
                f"reserved by the platform — use a different tag")
        t = OrderTicket(self, self.next_id, str(symbol).upper(), int(qty),
                        order_type, tag, limit_price, stop_price)
        # creation time gates same-bar fills: LEAN evaluates a resting order
        # against the just-completed bar only if the order predates the bar's
        # end — an update keeps the original time, a fresh order does not.
        t.created = self.clock()
        self.next_id += 1
        self.tickets.append(t)
        return t

    def _slippage(self, symbol: str, ticket) -> float:
        sec = self.securities.get(symbol)
        if sec is None:
            return 0.0
        return sec.slippage_model.get_slippage_approximation(sec, ticket)

    def _fee(self, symbol: str) -> float:
        sec = self.securities.get(symbol)
        if sec is None:
            return 0.0
        return sec.fee_model.get_order_fee(sec)

    def _unrest(self, ticket: OrderTicket):
        try:
            self._open.remove(ticket)
        except ValueError:
            pass

    def _fill(self, ticket: OrderTicket, price: float, slip: float = 0.0):
        px = price + slip if ticket.quantity > 0 else price - slip
        qty = ticket.quantity
        # margin is owed on the exposure an order ADDS, whichever side it is
        # on: opening a short consumes buying power exactly like opening a
        # long. Reducing, closing or flipping always clears for the part it
        # closes. (Long-only, this is the old cost-vs-buying-power check.)
        day, ms = self.clock()

        # ---- broker truth, when a ledger is present -------------------
        # Taken BEFORE any bookkeeping and before any event is emitted: a
        # user's on_order_event may place another order, whose own _fill
        # would call take() re-entrantly and could consume rows meant for
        # this one. Decide first, emit last.
        real = None
        if self.ledger is not None:
            # never "" — take() matches rule tags with ==, so a falsy
            # sentinel silently matches no tagged row and falls through to
            # pool order (the deleted IR engine stated the same rule; see
            # tests/runtime/test_py_ledger.py, which pins it here).
            # Same identity the live layer publishes for this order (it is
            # what lands on Execution.rule_tag), so take() prefers the rows
            # that actually belong to it. For a GENERATED strategy that is
            # the original IR rule id — see dqengine.runtime.identity.
            # side: a row only ever goes to an order on its own side (see
            # ExecutionLedger.take)
            real = self.ledger.take(day, ticket.symbol,
                                    intent_id(ticket.symbol, ticket.order_id,
                                              ticket.tag or ""),
                                    side=1 if qty > 0 else -1)
            if real == []:
                # Written as `== []`, not `not real`: the broker AFFIRMATIVELY
                # did not fill, which is a different fact from "we do not
                # know" (None, below) — conflating them is how an outage
                # becomes a duplicate position. Apply nothing, and leave the
                # ticket RESTING: it is still live at the broker, and
                # tearing it down here would drop a real protective stop off
                # a real position.
                self.unfilled_log.append(
                    f"{day} {ticket.symbol} {qty:+d} {ticket.tag or ''} "
                    f"— broker confirmed no fill")
                if not ticket.is_open():
                    # A MARKET order never rested, so leaving it NEW would
                    # hang any user code waiting on ticket.status or on an
                    # on_order_event callback. It is not live anywhere —
                    # close it loudly. (A resting order takes the branch
                    # above and stays open: it IS still live at the broker.)
                    ticket.status = OrderStatus.CANCELED
                    self._emit(ticket, OrderStatus.CANCELED,
                               message="broker confirmed no fill")
                return False

        if real:
            # The venue already accepted the risk and the shares are already
            # in the account, so buying power is NOT consulted here. The
            # model rejecting a real broker fill would leave the sleeve flat
            # while the account is long, and the next sweep would sell real
            # shares to "correct" it.
            filled = 0
            last_px = px
            for lf in real:
                self.sleeve.apply_fill(lf.day, lf.time_ms, ticket.symbol,
                                       lf.qty, lf.price, ticket.tag,
                                       fees=lf.fees, model_price=px)
                self.sleeve.orders.append(
                    OrderRecord(lf.day, lf.time_ms, ticket.symbol, lf.qty,
                                ticket.order_type.value, ticket.tag,
                                "filled", lf.price))
                filled += lf.qty
                last_px = lf.price
            ticket.status = OrderStatus.FILLED
            ticket.average_fill_price = last_px
            ticket.quantity_filled = filled
            self._unrest(ticket)
            self._emit(ticket, OrderStatus.FILLED, fill_price=last_px,
                       fill_qty=filled)
            return True

        # ---- the model's own fill (no ledger, or UNKNOWN) --------------
        # `real is None` means UNKNOWN: keep the model's fill so the replay
        # does not conclude it is flat and re-buy a position it already
        # holds, but mark it provisional. Buying power binds on this branch
        # because it IS the model's fill.
        prev = self.sleeve.qty.get(ticket.symbol, 0)
        added = (abs(prev + qty) - abs(prev)) * px
        if added > self.sleeve.buying_power(self.prices) + 1e-9:
            self._reject(ticket, "insufficient buying power")
            return False
        self.sleeve.apply_fill(day, ms, ticket.symbol, qty, px, ticket.tag,
                               fees=self._fee(ticket.symbol),
                               **({} if self.ledger is None
                                  else {"confirmed": False}))
        self.sleeve.orders.append(OrderRecord(day, ms, ticket.symbol, qty,
                                              ticket.order_type.value,
                                              ticket.tag, "filled", px))
        ticket.status = OrderStatus.FILLED
        ticket.average_fill_price = px
        ticket.quantity_filled = qty
        self._unrest(ticket)
        self._emit(ticket, OrderStatus.FILLED, fill_price=px, fill_qty=qty)
        return True

    def _pump(self, ticket: OrderTicket) -> bool:
        """Adopt the broker's fill of a resting ticket, if one is visible by
        the current bar. Returns True when a fill was applied. Never
        concludes "the broker did not fill" -- a resting order may fill any
        second -- so absence leaves the bar-cross simulation to run."""
        day, ms = self.clock()
        take = getattr(self.ledger, "take_rule_fill", None)
        if take is None:
            return False
        real = take(day, ticket.symbol,
                    intent_id(ticket.symbol, ticket.order_id, ticket.tag or ""),
                    upto_ms=ms)
        if not real:
            return False
        # exactly the broker-fill branch of _fill: no buying-power gate (the
        # money already moved at the broker), rows applied at THEIR time and
        # price, ticket torn down as a simulated fill would be
        filled, last_px = 0, 0.0
        for lf in real:
            self.sleeve.apply_fill(lf.day, lf.time_ms, ticket.symbol, lf.qty,
                                   lf.price, ticket.tag, fees=lf.fees)
            self.sleeve.orders.append(OrderRecord(
                lf.day, lf.time_ms, ticket.symbol, lf.qty,
                ticket.order_type.value, ticket.tag, "filled", lf.price))
            filled += lf.qty
            last_px = lf.price
        ticket.status = OrderStatus.FILLED
        ticket.average_fill_price = last_px
        ticket.quantity_filled = filled
        self._unrest(ticket)
        self._emit(ticket, OrderStatus.FILLED, fill_price=last_px, fill_qty=filled)
        return True

    def _reject(self, ticket: OrderTicket, why: str):
        day, ms = self.clock()
        self.rejections[why] = self.rejections.get(why, 0) + 1
        self.sleeve.orders.append(OrderRecord(day, ms, ticket.symbol,
                                              ticket.quantity,
                                              ticket.order_type.value,
                                              ticket.tag, "rejected"))
        ticket.status = OrderStatus.INVALID
        self._unrest(ticket)
        self._emit(ticket, OrderStatus.INVALID, message=why)

    def _refuse_warmup(self, symbol, qty, order_type, tag) -> OrderTicket:
        t = self._new_ticket(symbol, qty, order_type, tag)
        t.status = OrderStatus.INVALID
        self.refused_log.append(f"order during warm-up refused: {order_type.value} "
                                f"{qty} {t.symbol}")
        self._emit(t, OrderStatus.INVALID, message="orders are not allowed during warm-up")
        return t

    # ---------- order entry ----------

    def market(self, symbol, qty, price, tag="") -> OrderTicket:
        if not self.allow_orders:
            return self._refuse_warmup(symbol, qty, OrderType.MARKET, tag)
        if self.after_close_to_moo and not self.carried:
            day, ms = self.clock()
            if ms >= close_time_ms(day):
                return self._rest(symbol, qty, OrderType.MARKET_ON_OPEN, tag)
        t = self._new_ticket(symbol, qty, OrderType.MARKET, tag)
        if self.carried:
            # no real market to execute against — rest until the next real
            # bar; check_resting fills it at that bar's close
            t.status = OrderStatus.SUBMITTED
            self._open.append(t)
            self._emit(t, OrderStatus.SUBMITTED)
            return t
        self._fill(t, price, slip=self._slippage(t.symbol, t))
        return t

    def limit(self, symbol, qty, limit_price, tag="") -> OrderTicket:
        if not self.allow_orders:
            return self._refuse_warmup(symbol, qty, OrderType.LIMIT, tag)
        # LEAN rounds order prices to the minimum price variation ($0.01) —
        # the IR engine does the same (engine.py), and parity depends on it.
        t = self._new_ticket(symbol, qty, OrderType.LIMIT, tag,
                             limit_price=round(float(limit_price), 2))
        t.status = OrderStatus.SUBMITTED
        self._open.append(t)
        self._emit(t, OrderStatus.SUBMITTED)
        return t

    def stop_market(self, symbol, qty, stop_price, tag="") -> OrderTicket:
        if not self.allow_orders:
            return self._refuse_warmup(symbol, qty, OrderType.STOP_MARKET, tag)
        t = self._new_ticket(symbol, qty, OrderType.STOP_MARKET, tag,
                             stop_price=round(float(stop_price), 2))
        t.status = OrderStatus.SUBMITTED
        self._open.append(t)
        self._emit(t, OrderStatus.SUBMITTED)
        return t

    def stop_limit(self, symbol, qty, stop_price, limit_price, tag="") -> OrderTicket:
        if not self.allow_orders:
            return self._refuse_warmup(symbol, qty, OrderType.STOP_LIMIT, tag)
        t = self._new_ticket(symbol, qty, OrderType.STOP_LIMIT, tag,
                             limit_price=round(float(limit_price), 2),
                             stop_price=round(float(stop_price), 2))
        t.status = OrderStatus.SUBMITTED
        self._open.append(t)
        self._emit(t, OrderStatus.SUBMITTED)
        return t

    # ---------- resting-order engine ----------

    def market_on_open(self, symbol, qty, tag="") -> OrderTicket:
        return self._rest(symbol, qty, OrderType.MARKET_ON_OPEN, tag)

    def market_on_close(self, symbol, qty, tag="") -> OrderTicket:
        return self._rest(symbol, qty, OrderType.MARKET_ON_CLOSE, tag)

    def trailing_stop(self, symbol, qty, trailing_amount,
                      as_percentage=True, tag="") -> OrderTicket:
        t = self._rest(symbol, qty, OrderType.TRAILING_STOP, tag)
        if t.is_open():
            t.trailing_amount = float(trailing_amount)
            t.trailing_as_percentage = bool(as_percentage)
            t.stop_price = None          # anchored on the first bar seen
        return t

    def limit_if_touched(self, symbol, qty, trigger_price, limit_price,
                         tag="") -> OrderTicket:
        t = self._rest(symbol, qty, OrderType.LIMIT_IF_TOUCHED, tag,
                       limit_price=round(float(limit_price), 2))
        if t.is_open():
            t.stop_price = round(float(trigger_price), 2)
        return t

    def _rest(self, symbol, qty, order_type, tag, limit_price=None):
        if not self.allow_orders:
            return self._refuse_warmup(symbol, qty, order_type, tag)
        t = self._new_ticket(symbol, qty, order_type, tag,
                             limit_price=limit_price)
        t.status = OrderStatus.SUBMITTED
        self._open.append(t)
        self._emit(t, OrderStatus.SUBMITTED)
        return t

    def fill_at_session_edge(self, kind: OrderType, prices: dict):
        """Fill every resting MOO/MOC ticket at the session's open/close.

        Called by the backtester at the two moments those orders mean
        something; they are the only order kinds whose trigger is a clock
        rather than a price, so they cannot live in check_resting."""
        for t in list(self._open):
            if not t.is_open() or t.order_type != kind:
                continue
            px = prices.get(t.symbol)
            if px:
                self._fill(t, px, slip=self._slippage(t.symbol, t))

    def check_resting(self, symbol, o, h, l, c, exclude_created=None):
        if not self._open:
            return
        sym = str(symbol).upper()
        # snapshot: fills fired here run user on_order_event handlers that
        # may cancel or place orders, mutating _open mid-iteration
        for t in list(self._open):
            if not t.is_open() or t.symbol != sym:
                continue
            if exclude_created is not None and t.created == exclude_created:
                continue
            # THE FILL PUMP (lean-fills spec §3.3), ported from the IR
            # engine's _check_targets/_check_entries. This ticket rests
            # NATIVELY at the broker, so the broker -- not our bars -- decides
            # when and at what price it filled. A tagged ledger row visible
            # by this bar IS that decision: adopt it and skip the simulation.
            # Without this, a take-profit the broker fills on a wick our bar
            # never printed leaves the model still long: want > have, and
            # the executor BUYS BACK what the broker just sold -- the
            # 2026-08-25 churn class the pump was adopted to close.
            if self.ledger is not None and self._pump(t):
                continue
            buy = t.quantity > 0
            if t.order_type == OrderType.MARKET:
                # a market order deferred from a carried session fills on the
                # first real bar, at its close — the standard market price
                self._fill(t, c, slip=self._slippage(sym, t))
            elif t.order_type == OrderType.LIMIT:
                px = fills.limit_fill(buy, o, h, l, t.limit_price)
                if px is not None:
                    self._fill(t, px)
            elif t.order_type == OrderType.STOP_MARKET:
                px = fills.stop_fill(buy, o, h, l, t.stop_price)
                if px is not None:
                    self._fill(t, px, slip=self._slippage(sym, t))
            elif t.order_type == OrderType.TRAILING_STOP:
                # ratchet first, then test: the stop only ever moves in the
                # direction that protects the position, and a bar that both
                # extends the trend and breaches cannot do both at once
                amt = t.trailing_amount
                ref = l if buy else h
                new_stop = (ref * (1 + amt) if buy else ref * (1 - amt)) \
                    if t.trailing_as_percentage else \
                    (ref + amt if buy else ref - amt)
                if t.stop_price is None:
                    t.stop_price = new_stop
                elif buy:
                    t.stop_price = min(t.stop_price, new_stop)
                else:
                    t.stop_price = max(t.stop_price, new_stop)
                px = fills.stop_fill(buy, o, h, l, t.stop_price)
                if px is not None:
                    self._fill(t, px, slip=self._slippage(sym, t))
            elif t.order_type == OrderType.LIMIT_IF_TOUCHED:
                # the mirror of stop_limit: the trigger is approached from
                # the FAVOURABLE side (a buy triggers on a fall to it)
                if not t._triggered:
                    if fills.touch_triggered(buy, h, l, t.stop_price):
                        t._triggered = True
                else:
                    px = fills.limit_fill(buy, o, h, l, t.limit_price)
                    if px is not None:
                        self._fill(t, px)
            elif t.order_type == OrderType.STOP_LIMIT:
                already_triggered = t._triggered
                if not already_triggered:
                    if fills.stop_triggered(buy, h, l, t.stop_price):
                        t._triggered = True
                if already_triggered:
                    px = fills.limit_fill(buy, o, h, l, t.limit_price)
                    if px is not None:
                        self._fill(t, px)

    # ---------- mutation ----------

    def update(self, ticket: OrderTicket, fields: UpdateOrderFields):
        if not ticket.is_open():
            self.refused_log.append(
                f"update ignored on {ticket.status.value} order {ticket.order_id}")
            return
        # same reservation _new_ticket enforces: a tag is an identity once
        # it carries the generated prefix, and update() was a way around it
        if fields.tag is not None and str(fields.tag).startswith(
                GENERATED_TAG_PREFIX) and not getattr(self, "generated", False):
            raise UnsupportedApiError(
                f"order tags starting with {GENERATED_TAG_PREFIX!r} are "
                f"reserved by the platform — use a different tag")
        if fields.quantity is not None:
            ticket.quantity = int(fields.quantity)
        if fields.limit_price is not None:
            ticket.limit_price = round(float(fields.limit_price), 2)
        if fields.stop_price is not None:
            ticket.stop_price = round(float(fields.stop_price), 2)
        if fields.tag is not None:
            ticket.tag = fields.tag

    def cancel(self, ticket: OrderTicket):
        if not ticket.is_open():
            return
        day, ms = self.clock()
        self.sleeve.orders.append(OrderRecord(day, ms, ticket.symbol,
                                              ticket.quantity,
                                              ticket.order_type.value,
                                              ticket.tag, "canceled"))
        ticket.status = OrderStatus.CANCELED
        self._unrest(ticket)
        self._emit(ticket, OrderStatus.CANCELED)

    # ---------- queries ----------

    @property
    def orders(self):
        return self.sleeve.orders

    def open_tickets(self, symbol=None) -> list[OrderTicket]:
        sym = str(symbol).upper() if symbol is not None else None
        return [t for t in self._open
                if t.is_open() and (sym is None or t.symbol == sym)]

    def get(self, order_id) -> OrderTicket | None:
        for t in self.tickets:
            if t.order_id == order_id:
                return t
        return None


@alias_methods
class Transactions(PascalMixin):
    def __init__(self, book: OrderBook):
        self._book = book

    def get_open_order_tickets(self, symbol=None) -> list[OrderTicket]:
        return self._book.open_tickets(symbol)

    def get_open_orders(self, symbol=None) -> list[OrderTicket]:
        return self._book.open_tickets(symbol)

    def cancel_open_orders(self, symbol=None):
        for t in self._book.open_tickets(symbol):
            self._book.cancel(t)

    def get_order_by_id(self, order_id) -> OrderTicket | None:
        return self._book.get(order_id)

    def get_order_ticket(self, order_id) -> OrderTicket | None:
        return self._book.get(order_id)
