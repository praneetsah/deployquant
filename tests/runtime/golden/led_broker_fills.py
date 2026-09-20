from AlgorithmImports import *
from dqengine.runtime.blocks import BlockContext, NotReady

# platform-generated: permits the reserved `ir:` order-tag prefix,
# which carries each order's IR rule id as its live identity
__STRATEGY_LAB_GENERATED__ = True

PARAMS = {'TIER_HIGH': 1.08177, 'TIER_MID': 1.07, 'TIER_LOW': 1.025, 'TIER_PROFIT_HIGH': 0.003, 'STOP_PCT': 0.2, 'SIZE_BASE': 0.98, 'SIZE_VOLBOOST': 1.25, 'AVGDOWN_TRIGGER': -0.05, 'AVGDOWN_SIZE': 0.3, 'VOL_WINDOW': 14, 'VOL_LOOKBACK': 252, 'VOL_DECILE': 0.9}
SYMS = ['TQQQ']
UNIVERSE = ['TQQQ']
PRIMARY = 'TQQQ'
RULES_BY_SYMBOL = {'TQQQ': ['avg-down', 'tiered-target', 'weekly-entry', 'close-stop', 'loser-exit-thu', 'loser-exit-eow']}


class TqqqWeekly1(QCAlgorithm):
    """TQQQ Weekly 1% — ejected from the block editor. Runs the SAME
    indicator engine the block strategy runs, so the two agree by
    construction rather than by two implementations matching."""

    TIER_HIGH = 1.08177
    TIER_MID = 1.07
    TIER_LOW = 1.025
    TIER_PROFIT_HIGH = 0.003
    STOP_PCT = 0.2
    SIZE_BASE = 0.98
    SIZE_VOLBOOST = 1.25
    AVGDOWN_TRIGGER = -0.05
    AVGDOWN_SIZE = 0.3
    VOL_WINDOW = 14
    VOL_LOOKBACK = 252
    VOL_DECILE = 0.9

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
                                warm=False, track_ohlc=False)
        self._current_day = None
        self._prev_session_day = None
        self._guard_week = {}
        self._guard_flag = {}
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 0),
                         self._session_start)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.after_market_open(self.sym, 1),
                         self._at_open)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.before_market_close(self.sym, 1),
                         self._before_close_1)
        self.schedule.on(self.date_rules.every_day(self.sym),
                         self.time_rules.at(14, 0),
                         self._at_1400)

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
        self.transactions.cancel_open_orders(sym)
        qty = int(self.portfolio[sym].quantity)
        if qty > 0:
            self.market_order(sym, -qty, tag=tag)
            if not self.portfolio[sym].invested:
                self._on_flat(sym)

    def _on_flat(self, sym):
        # IR `_on_flat`: a position ending releases the once_per=position
        # guards of the rules bound to THAT symbol. Called from exactly three
        # places, as the IR engine calls it: a liquidate, a managed-target
        # fill, and a rebalance that took a symbol to zero. A plain market
        # sell that flattens does NOT release them.
        for rid in RULES_BY_SYMBOL.get(sym, ()):
            self._guard_flag.pop("position:" + rid, None)

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

    def _rule_avg_down(self):
        # rule: avg-down  (market_order on TQQQ)
        if not (not self._guard_flag.get("position:avg-down")):
            return
        if not (((self.portfolio['TQQQ'].invested) and (self._cmp(lambda: (self._bc.pnl_pct('TQQQ', 'prior_close', use_open=False)) <= (self.AVGDOWN_TRIGGER))))):
            return
        ref_price = self._bc.indicator({'ind': 'prior_close'}, 'TQQQ')
        qty = int(float(self.portfolio.total_portfolio_value) * (self.AVGDOWN_SIZE) / ref_price)
        if qty > 0:
            self.market_order('TQQQ', 1 * qty, tag="ir:avg-down")
            self._guard_flag["position:avg-down"] = True

    def _rule_tiered_target(self):
        # rule: tiered-target  (managed_target on TQQQ)
        if not (self.portfolio['TQQQ'].invested):
            return
        # qty='all': the IR engine never reads managed_target['qty'] and always
        # exits the whole position, so this target is for all of it.
        qty = int(self.portfolio['TQQQ'].quantity)
        if qty <= 0:
            return
        limit_px = ((self._bc.entry_price('TQQQ')) * (((self.TIER_HIGH) if self._cmp(lambda: (self._cmp(lambda: (self._bc.pnl_pct('TQQQ', 'prior_close', use_open=False)) > (self.TIER_PROFIT_HIGH)))) else ((self.TIER_MID) if self._cmp(lambda: (self._cmp(lambda: (self._bc.pnl_pct('TQQQ', 'prior_close', use_open=False)) > (0)))) else (self.TIER_LOW)))))
        self._place_or_update_target('TQQQ', qty, limit_px, tag="ir:tiered-target")

    def _rule_weekly_entry(self):
        # rule: weekly-entry  (market_order on TQQQ)
        if not self._is_first_of_week():
            return
        if not (self._guard_week.get('weekly-entry') != self._week_key(self._current_day)):
            return
        if not ((not (self.portfolio['TQQQ'].invested))):
            return
        ref_price = self._bc.session_open('TQQQ')
        qty = int(float(self.portfolio.total_portfolio_value) * (((self.SIZE_VOLBOOST) if self._cmp(lambda: (self._cmp(lambda: (self._bc.indicator({'ind': 'pct_rank', 'of': {'ind': 'realized_vol', 'window': {'param': 'VOL_WINDOW'}}, 'lookback': {'param': 'VOL_LOOKBACK'}, 'min_obs': 60}, 'TQQQ')) >= (self.VOL_DECILE)))) else (self.SIZE_BASE))) / ref_price)
        if qty > 0:
            self.market_order('TQQQ', 1 * qty, tag="ir:weekly-entry")
            self._guard_week['weekly-entry'] = self._week_key(self._current_day)

    def _rule_close_stop(self):
        # rule: close-stop  (liquidate on TQQQ)
        if not (((self.portfolio['TQQQ'].invested) and (self._cmp(lambda: (self._bc.days_held('TQQQ')) >= (1))) and (self._cmp(lambda: (self._bc.price('TQQQ')) <= (((self._bc.entry_price('TQQQ')) * (((1) - (self.STOP_PCT))))))))):
            return
        self._liquidate_all('TQQQ', "ir:close-stop")

    def _rule_loser_exit_thu(self):
        # rule: loser-exit-thu  (liquidate on TQQQ)
        if not self._tomorrow_is_last_trading_day():
            return
        if not (((self.portfolio['TQQQ'].invested) and (self._cmp(lambda: (self._bc.price('TQQQ')) <= (self._bc.entry_price('TQQQ')))))):
            return
        self._liquidate_all('TQQQ', "ir:loser-exit-thu")

    def _rule_loser_exit_eow(self):
        # rule: loser-exit-eow  (liquidate on TQQQ)
        if not self._is_last_trading_day():
            return
        if not (((self.portfolio['TQQQ'].invested) and (self._cmp(lambda: (self._bc.pnl_pct('TQQQ', 'prior_close', use_open=True)) <= (0))))):
            return
        self._liquidate_all('TQQQ', "ir:loser-exit-eow")

    def _at_open(self):
        if self._current_day is None:
            return
        self._bc.mark_prices()
        self._bc.take_snapshot(freeze_open=True)
        try:
            self._rule_avg_down()
        except NotReady:
            pass
        try:
            self._rule_tiered_target()
        except NotReady:
            pass
        try:
            self._rule_weekly_entry()
        except NotReady:
            pass

    def _before_close_1(self):
        if self._current_day is None:
            return
        self._bc.mark_prices()
        self._bc.take_snapshot(freeze_open=False)
        try:
            self._rule_close_stop()
        except NotReady:
            pass
        try:
            self._rule_loser_exit_eow()
        except NotReady:
            pass

    def _at_1400(self):
        if self._current_day is None:
            return
        self._bc.mark_prices()
        self._bc.take_snapshot(freeze_open=False)
        try:
            self._rule_loser_exit_thu()
        except NotReady:
            pass
