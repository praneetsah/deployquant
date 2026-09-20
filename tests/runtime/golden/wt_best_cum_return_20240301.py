from AlgorithmImports import *
from dqengine.runtime.blocks import BlockContext, NotReady
from dqengine.runtime.allocation import Allocator

# platform-generated: permits the reserved `ir:` order-tag prefix,
# which carries each order's IR rule id as its live identity
__STRATEGY_LAB_GENERATED__ = True

PARAMS = {}
SYMS = ['SPY', 'QQQ', 'IWM', 'TLT']
UNIVERSE = ['SPY', 'QQQ', 'IWM', 'TLT']
PRIMARY = 'SPY'
RULES_BY_SYMBOL = {'SPY': ['rot']}
WEIGHTS_ROT = {'best': {'of': [{'asset': 'SPY'}, {'asset': 'QQQ'}, {'asset': 'IWM'}, {'asset': 'TLT'}], 'by': {'metric': 'cum_return', 'window': 60}, 'order': 'top', 'n': 1}}


class Shape(QCAlgorithm):
    """shape — ejected from the block editor. Runs the SAME
    indicator engine the block strategy runs, so the two agree by
    construction rather than by two implementations matching."""

    def initialize(self):
        self.set_start_date(2021, 1, 4)   # the platform run overrides these
        self.set_end_date(2026, 6, 9)
        self.set_cash(10000)
        for t in SYMS:
            self.add_equity(t, Resolution.MINUTE)
        self.sym = self.securities[PRIMARY].symbol
        self._security = self.securities[PRIMARY]
        self.symbols = [self.securities[t].symbol for t in SYMS]
        # ONE indicator engine, shared with every rule and every weight
        # tree below. NOT warmed here: the platform applies the real
        # start/end AFTER initialize returns, so warming now would read
        # history relative to the placeholder dates above.
        self._bc = BlockContext(self, PARAMS, SYMS, universe=UNIVERSE,
                                warm=True, track_ohlc=True)
        self._alloc_rot = Allocator(self, WEIGHTS_ROT, PARAMS, SYMS, ctx=self._bc)
        self._current_day = None
        self._prev_session_day = None
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 0),
                         self._session_start)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 1),
                         self._at_open)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.before_market_close(self.sym, 1),
                         self._before_close_1)

    def _session_start(self):
        day = self.time.date()
        if day != self._current_day:
            self._prev_session_day = self._current_day
            self._current_day = day
        self._bc.start_session(day)

    def on_end_of_day(self, symbol=None):
        day = self.time.date()
        if day != self._current_day:
            # warm-up session: no events fired, but the daily series still
            # has to advance or every rolling window is one day short
            self._prev_session_day = self._current_day
            self._current_day = day
        self._bc.close_session(day)

    def on_data(self, data):
        # per-session OHLC for the daily series (atr reads highs and
        # lows). Overriding on_data disables the quiet-bar fast path,
        # by design.
        self._bc.on_bar(data)

    def _div(self, a, b):
        if b == 0:
            raise NotReady
        return a / b

    def _cmp(self, f):
        # IR semantics: comparisons and branch conditions over a not-ready
        # value are simply False
        try:
            return bool(f())
        except NotReady:
            return False

    def _week_key(self, d):
        iso = d.isocalendar()
        return iso[0] * 100 + iso[1]

    def _prior_session(self):
        # The session BEFORE today on the calendar -- the calendar reaches back
        # through the store's history, exactly what the IR engine consults.
        # Reading "no previous session in this RUN" as "first of week/month"
        # made a deployment started on a Wednesday fire its weekly entry on
        # day one (live 2026-08-20, adoption gate refusal 2026-09-06).
        if self._prev_session_day is not None:
            return self._prev_session_day
        cal = getattr(self, "_calendar", None)
        days = getattr(cal, "days", None)
        if days:
            from bisect import bisect_left
            i = bisect_left(days, self._current_day)
            if i > 0:
                return days[i - 1]
        return None

    def _is_first_of_week(self):
        prev = self._prior_session()
        if prev is None:
            cal = getattr(self, "_calendar", None)
            if cal is not None and self._current_day in getattr(cal, "_index", {}):
                return cal.is_first_of_week(self._current_day)   # rule-based at the edge
            return True
        return self._week_key(prev) != self._week_key(self._current_day)

    def _is_first_of_month(self):
        prev = self._prior_session()
        if prev is None:
            cal = getattr(self, "_calendar", None)
            if cal is not None and self._current_day in getattr(cal, "_index", {}):
                return cal.is_first_of_month(self._current_day)
            return True
        return (prev.year, prev.month) != \
            (self._current_day.year, self._current_day.month)

    def _next_trading_day(self, d):
        nxt = self._security.exchange.hours.get_next_trading_day(d)
        return nxt.date() if hasattr(nxt, "date") else nxt

    def _is_last_trading_day(self):
        nxt = self._next_trading_day(self._current_day)
        return self._week_key(self._current_day) != self._week_key(nxt)

    def _tomorrow_is_last_trading_day(self):
        nxt = self._next_trading_day(self._current_day)
        nxt2 = self._next_trading_day(nxt)
        return self._week_key(nxt) != self._week_key(nxt2)

    def _is_last_of_month(self):
        nxt = self._next_trading_day(self._current_day)
        return (nxt.year, nxt.month) != \
            (self._current_day.year, self._current_day.month)

    def _place_or_update_target(self, sym, qty, limit_px, tag=""):
        # reconcile the resting GTC sell limit in place (price/qty), never
        # cancel-and-resubmit -- one order, updated each morning
        tickets = [t for t in self.transactions.get_open_order_tickets(sym)
                   if t.order_type == OrderType.LIMIT and t.quantity < 0]
        if tickets:
            fields = UpdateOrderFields()
            fields.limit_price = limit_px
            fields.quantity = -qty
            tickets[0].update(fields)
            for extra in tickets[1:]:
                extra.cancel()
        else:
            self.limit_order(sym, -qty, limit_px, tag=tag)

    def _liquidate_all(self, sym, tag):
        # `sym` is the firing rule's symbol; a liquidate closes everything.
        self.transactions.cancel_open_orders()
        for s in self._bc.held_symbols():
            qty = int(self.portfolio[s].quantity)
            if qty > 0:
                self.market_order(s, -qty, tag=tag)
                if not self.portfolio[s].invested:
                    self._on_flat(s)

    def _on_flat(self, sym):
        # no once_per=position guard in this strategy: nothing to release
        pass

    def on_order_event(self, e):
        # ANY fill that leaves this symbol flat tears down its resting orders.
        # Not housekeeping: the IR engine deletes a managed target the moment
        # sleeve.qty <= 0 (_check_targets), while dqengine.runtime's check_resting
        # fills a resting LIMIT sell whenever the price crosses, position or
        # no position. So a market sell (or a sell at_close_order) that
        # flattens under a live target leaves a ticket that SHORTS the sleeve
        # where the oracle simply has nothing left to fill.
        if e.status != OrderStatus.FILLED:
            return
        if self.portfolio[e.symbol].invested:
            return
        self.transactions.cancel_open_orders(e.symbol)
        ticket = self.transactions.get_order_by_id(e.order_id)
        if ticket is not None and ticket.order_type == OrderType.LIMIT:
            # the guard release, on the other hand, is NOT for every exit: a
            # managed-target fill is one of the IR engine's three _on_flat
            # sites, a plain market sell is not.
            self._on_flat(e.symbol)

    def _rule_rot(self):
        # rule: rot  (set_weights on SPY)
        self._alloc_rot.rebalance(
            self.time.date(), tag="ir:rot", on_flat=self._on_flat)

    def _at_open(self):
        if self._current_day is None:
            return
        self._bc.mark_prices()
        self._bc.take_snapshot(freeze_open=True)
        pass

    def _before_close_1(self):
        if self._current_day is None:
            return
        self._bc.mark_prices()
        self._bc.take_snapshot(freeze_open=False)
        try:
            self._rule_rot()
        except NotReady:
            pass
