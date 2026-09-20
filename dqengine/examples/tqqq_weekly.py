from AlgorithmImports import *
from collections import deque
import statistics


class TqqqWeekly(QCAlgorithm):
    """
    Sample strategy: a weekly swing trade on TQQQ (3x Nasdaq-100), minute bars.

    The idea is modest -- aim for a small gain most weeks rather than a big one
    some weeks:

      * Enter at the Monday open (first session of the week).
      * Each morning, rest a take-profit limit a few percent above cost. The
        target is tiered: highest while the position is fresh, lower once it
        has been held a while, and just above break-even once it is underwater.
      * A close 20% under cost arms a protective exit.
      * Near the end of the week, positions that are LOSING are closed (Thursday
        2pm, with a Friday near-the-close backstop). Winners are left to run
        until their target fills.
      * Two sizing layers: size up slightly in the most volatile weeks
        (VOL_BOOST), and add 30% once to a position that closes 5% or more
        underwater (AVG_DOWN). Leverage is capped at 1.33x, so an add that would
        exceed it is refused -- on purpose.

    The brokerage model has no market-on-close order, so the Friday backstop is
    a market order about a minute before the close.

    This file is an EXAMPLE of what the engine can run -- resting limits and
    stops, scheduled events, margin, indicator-driven sizing -- and it is the
    algorithm behind this project's parity and benchmark numbers. It is not a
    recommendation. Past results do not predict future results.

    START / END / SLIPPAGE_BPS affect backtests only.
    """

    # ===== strategy toggles =====
    CUT_LOSERS = False             # cut losers at the open instead of waiting for the recovery target
    STREAK_SIZE = False            # half-size the week after a loss
    VOL_GATE = False               # skip the most volatile weeks
    LATE_ENTRY = False             # enter at 10:00 instead of the open
    VOL_BOOST = True               # size up in the most volatile weeks (see _size_fraction)
    RATCHET = False                # trailing exit for winners (off: the target cap makes it unreachable)
    AVG_DOWN = True                # add 30% once when a position closes >=5% underwater
    HOLD_LOSERS = False            # never time-exit losers
    SLIPPAGE_BPS = 0               # backtest only; set 5 for realistic fills. Live uses real prices.

    START = (2021, 1, 4)           # backtest only -- ignored in live trading
    END = (2026, 6, 9)             # backtest only -- ignored in live trading

    # ===== parameters =====
    TIER_HIGH = 1.08177
    TIER_MID = 1.07
    TIER_LOW = 1.025
    TIER_PROFIT_HIGH = 0.003
    STOP_TRIGGER_PCT = 0.20
    THURSDAY_EXIT_HOUR = 14

    def initialize(self):
        self.set_start_date(*self.START)
        self.set_end_date(*self.END)
        self.set_cash(1000)
        self.set_brokerage_model(BrokerageName.WEBULL, AccountType.MARGIN)

        equity = self.add_equity("TQQQ", Resolution.MINUTE)
        equity.set_data_normalization_mode(DataNormalizationMode.RAW)  # local rig: pre-adjusted data + no-op factors
        self._symbol = equity.symbol
        self._security = equity
        equity.set_slippage_model(ConstantSlippageModel(self.SLIPPAGE_BPS / 10000.0))
        equity.set_leverage(1.33)   # a 3x ETF at 75% margin = 1.33x max. Caps avg-down stacks.
        # Benchmark = buy-and-hold TQQQ (the traded instrument), not the SPY default -- a risk-matched
        # yardstick. Stats/reporting only; does not affect trading, fills, or returns.
        self.set_benchmark(self._symbol)

        self.schedule.on(self.date_rules.every_day(self._symbol),
                         self.time_rules.before_market_close(self._symbol, 1),
                         self._close_stop_check)
        # MOC emulation for Webull (no native MOC): fire market order ~1 min before close
        self.schedule.on(self.date_rules.every_day(self._symbol),
                         self.time_rules.before_market_close(self._symbol, 1),
                         self._eow_backstop_emulated)
        self.schedule.on(self.date_rules.every_day(self._symbol),
                         self.time_rules.at(self.THURSDAY_EXIT_HOUR, 0),
                         self._thursday_exit)
        self.schedule.on(self.date_rules.every_day(self._symbol),
                         self.time_rules.at(10, 0),
                         self._late_entry)

        self._week_open = 0.0
        self._traded_this_week = False
        self._current_week_key = None
        self._entry_price = 0.0
        self._entry_day = None
        self._current_day = None
        self._prev_close = 0.0
        self._last_close = 0.0
        self._last_trade_won = True          # streak sizing state
        self._peak_close = 0.0               # ratchet state
        self._added_this_position = False    # avg-down state
        self._pending_late_entry = False
        self._eow_backstop_pending = False   # MOC-emulation flag
        self._pending_exit_tag = None        # deferred-liquidation flag (cancel limit -> then market-exit)
        self._seen_first_session = False     # start guard: defer a first entry that would land on a Friday
        # realized-vol gate state
        self._daily_rets = deque(maxlen=15)
        self._vol_history = deque(maxlen=252)
        self._gate_closed = False

    # ---------------- core ----------------

    def on_data(self, slice):
        if self._symbol not in slice.bars:
            return
        bar = slice.bars[self._symbol]
        day = bar.end_time.date()
        if day != self._current_day:
            if self._last_close > 0 and self._prev_close > 0:
                self._daily_rets.append(self._last_close / self._prev_close - 1.0)
                if len(self._daily_rets) >= 14:
                    v = statistics.pstdev(list(self._daily_rets)[-14:])
                    self._vol_history.append(v)
                    if len(self._vol_history) >= 60:
                        hist = sorted(self._vol_history)
                        self._gate_closed = v >= hist[int(0.9 * (len(hist) - 1))]
            self._prev_close = self._last_close
            self._current_day = day
            self._on_session_open(bar.open, bar.end_time)
        self._last_close = bar.close

    def _size_fraction(self):
        if self.STREAK_SIZE and not self._last_trade_won:
            return 0.49
        if getattr(self, 'VOL_BOOST', False) and self._gate_closed:
            # >>> 1.25 = liquidation-buffer build (76.2% CAGR). Change to 1.30 for 77.2%. <<<
            return 1.25
        return 0.98

    def _on_session_open(self, session_open, dt):
        self._eow_backstop_pending = False   # reset MOC-emulation flag each session
        week_key = dt.isocalendar()[0] * 100 + dt.isocalendar()[1]
        first_day = week_key != self._current_week_key
        self._current_week_key = week_key
        last_day = self._is_last_trading_day(dt)

        # Start guard: defer the FIRST entry only if the start lands on the last trading
        # day of the week (Fri) -- that position would carry the weekend UNMANAGED (no
        # take-profit/stop until Mon). Mon-Thu starts enter normally (managed next day).
        defer_first_entry = False
        if not self._seen_first_session:
            self._seen_first_session = True
            defer_first_entry = last_day

        if first_day:
            self._week_open = session_open
            self._traded_this_week = False
            self._pending_late_entry = False

        holding = self.portfolio[self._symbol]

        # Restart resync: a fresh start resets our cost-basis state to 0, but the broker still
        # holds the shares -- so holding.invested is True while _entry_price is 0, which blows up the
        # profit calc below with a /0. Seed our state from the broker's reported average price the
        # first time we inherit a position. _prev_close can also be cold on the first session, so fall
        # back to the cost basis (reads as breakeven -> the +2.5% recovery target, e.g. 85.13 @ 83.05).
        if holding.invested and self._entry_price <= 0:
            self._entry_price = float(holding.average_price) or float(self._security.price)
            self._entry_day = self._current_day      # don't fire the close-stop on the resync day
            self._added_this_position = False
        if holding.invested and self._prev_close <= 0:
            self._prev_close = float(self._entry_price)

        if holding.invested:
            if self._entry_price <= 0:
                return                               # no cost basis yet -- wait for data, don't /0
            qty = int(holding.quantity)
            profit = (self._prev_close - self._entry_price) / self._entry_price
            self._peak_close = max(self._peak_close, self._prev_close)

            # Ratchet: a winner that closes >7% below its peak close is exited
            if (getattr(self, 'RATCHET', False) and profit > 0
                    and self._prev_close < self._peak_close * 0.93):
                self._liquidate("Ratchet")
                return

            if profit <= 0 and self.CUT_LOSERS:
                self._liquidate("CutLoser")
                return

            # Average down once per position when deeply underwater.
            # On a base 0.98x week this reaches ~1.28x (allowed); on a vol-boost week
            # it would reach ~1.55x and is REJECTED by the 1.33x leverage cap (intended).
            if (getattr(self, 'AVG_DOWN', False) and not self._added_this_position
                    and profit <= -0.05):
                add_qty = int(float(self.portfolio.total_portfolio_value) * 0.30 / self._prev_close)
                if add_qty > 0:
                    self.market_order(self._symbol, add_qty, tag="AvgDown")
                    self._added_this_position = True
                    qty = int(self.portfolio[self._symbol].quantity)

            if profit > self.TIER_PROFIT_HIGH:
                target, tag = self.TIER_HIGH, "Type-A+"
            elif profit > 0:
                target, tag = self.TIER_MID, "Type-A"
            else:
                target, tag = self.TIER_LOW, "Type-C"
            self._place_or_update_target(qty, float(self._entry_price) * target, tag)

            # week-end exits apply only to losers (winners are held over the weekend).
            # MOC emulation: flag now; _eow_backstop_emulated fires a market order ~1 min before close.
            if last_day and profit <= 0 and not getattr(self, 'HOLD_LOSERS', False):
                self._eow_backstop_pending = True
        else:
            self.transactions.cancel_open_orders(self._symbol)
            if first_day and not self._traded_this_week and not defer_first_entry:
                if self.VOL_GATE and self._gate_closed:
                    return                      # sit out high-vol weeks
                if self.LATE_ENTRY:
                    self._pending_late_entry = True
                else:
                    self._enter(session_open)

    def _place_or_update_target(self, qty, limit_px, tag):
        # Reconcile the resting target limit IN PLACE rather than cancel-then-resubmit. Submitting a
        # fresh sell while the prior sell is still (pending-)cancel makes Webull see 2x the sell qty
        # against the long position and reject it ("This order ... will reverse an existing position").
        # Orders are GTC, so yesterday's limit is still resting each morning -- we modify it (price/qty)
        # instead of cancel+new, so a second order never exists and the reversal reject can't happen.
        # First morning after entry there's no resting limit yet, so this places a fresh one.
        tickets = [t for t in self.transactions.get_open_order_tickets(self._symbol)
                   if t.order_type == OrderType.LIMIT and t.quantity < 0]
        if tickets:
            fields = UpdateOrderFields()
            fields.limit_price = limit_px
            fields.quantity = -qty
            fields.tag = tag
            tickets[0].update(fields)
            for extra in tickets[1:]:        # collapse any duplicates onto the primary
                extra.cancel()
        else:
            self.limit_order(self._symbol, -qty, limit_px, tag=tag)

    def _enter(self, ref_price):
        qty = int(float(self.portfolio.total_portfolio_value) * self._size_fraction() / ref_price)
        if qty > 0:
            self.market_order(self._symbol, qty, tag="Entry")

    def _late_entry(self):
        if not self._pending_late_entry or self.portfolio[self._symbol].invested:
            return
        self._pending_late_entry = False
        price = float(self._security.price)
        if price > 0:
            self._enter(price)

    def _liquidate(self, tag):
        # Exit the FULL position without colliding with the resting recovery limit. On Webull a fresh
        # market sell submitted while that limit still rests = 2x sell vs the long -> "reverse position"
        # reject. So cancel the resting limit first and DEFER the market exit until the cancel confirms
        # (on_order_event). Submitting the market order now would race the cancel and trip the same reject.
        # If nothing is resting, sell immediately.
        tickets = [t for t in self.transactions.get_open_order_tickets(self._symbol)
                   if t.order_type == OrderType.LIMIT and t.quantity < 0]
        if tickets:
            self._pending_exit_tag = tag
            for t in tickets:
                t.cancel()
        else:
            self._market_exit(tag)

    def _market_exit(self, tag):
        qty = int(self.portfolio[self._symbol].quantity)
        if qty > 0:
            self.market_order(self._symbol, -qty, tag=tag)

    def _eow_backstop_emulated(self):
        # Webull MOC emulation: exit a flagged week-end loser at ~1 min before close.
        # (Webull has no native MarketOnClose; a near-close market order fills within
        #  ~a cent of the close on TQQQ. Affects the Friday loser backstop only.)
        if getattr(self, '_eow_backstop_pending', False) and self.portfolio[self._symbol].invested:
            self._liquidate("EOW-backstop-emul")
        self._eow_backstop_pending = False

    def _close_stop_check(self):
        h = self.portfolio[self._symbol]
        if not h.invested or self._entry_day is None or self._current_day <= self._entry_day:
            return
        price = float(self._security.price)
        if 0 < price <= float(self._entry_price) * (1.0 - self.STOP_TRIGGER_PCT):
            self._liquidate("Stop(close)")

    def _thursday_exit(self):
        h = self.portfolio[self._symbol]
        if not h.invested or self._current_day is None:
            return
        if not self._tomorrow_is_last_trading_day(self.time):
            return
        price = float(self._security.price)
        if price > float(self._entry_price):
            return                              # winners keep running
        if getattr(self, 'HOLD_LOSERS', False):
            return                              # HOLD_LOSERS: losers keep running too
        self._liquidate("EOW-Thu2pm")

    def on_order_event(self, order_event):
        # Deferred liquidation: once the resting limit's cancel is CONFIRMED, fire the market exit.
        # Doing it any earlier would race the cancel and trip Webull's position-reversal reject.
        if order_event.status == OrderStatus.CANCELED:
            if self._pending_exit_tag and self.portfolio[self._symbol].invested:
                tag = self._pending_exit_tag
                self._pending_exit_tag = None
                self._market_exit(tag)
            return
        if order_event.status != OrderStatus.FILLED:
            return
        if order_event.direction == OrderDirection.BUY:
            self._entry_price = float(order_event.fill_price)
            self._entry_day = self._current_day
            self._traded_this_week = True
            self._peak_close = max(self._peak_close, float(order_event.fill_price)) if self._added_this_position else float(order_event.fill_price)
            if "AvgDown" not in (self.transactions.get_order_by_id(order_event.order_id).tag or ""):
                self._added_this_position = False
        else:
            self._pending_exit_tag = None        # any sell fill resolves a pending exit
            if not self.portfolio[self._symbol].invested:
                self.transactions.cancel_open_orders(self._symbol)
                self._last_trade_won = float(order_event.fill_price) > float(self._entry_price)

    def _is_last_trading_day(self, dt):
        nxt = self._security.exchange.hours.get_next_trading_day(dt)
        ka = dt.date().isocalendar()[0] * 100 + dt.date().isocalendar()[1]
        b = nxt.date() if hasattr(nxt, "date") else nxt
        kb = b.isocalendar()[0] * 100 + b.isocalendar()[1]
        return ka != kb

    def _tomorrow_is_last_trading_day(self, dt):
        nxt = self._security.exchange.hours.get_next_trading_day(dt)
        return self._is_last_trading_day(nxt)
