import math
from datetime import date, datetime, timedelta

from .aliases import PascalMixin, alias_methods, camel_to_snake
from .enums import Resolution
from .errors import reject_extra, unsupported, unsupported_arg
from .scheduling import DateRules, Schedule, TimeRules
from .symbol import Exchange, ExchangeHours, Security, Symbol

MAX_LOG_LINES = 10_000

# LEAN surface that exists but is out of the v1 subset — each becomes a method
# that raises UnsupportedApiError naming itself.
_UNSUPPORTED = [
    "add_option", "add_future", "add_crypto", "add_forex", "add_index",
    "add_cfd", "add_data", "add_universe", "add_alpha", "add_chart",
    "set_universe_selection", "set_alpha", "set_portfolio_construction",
    "set_execution", "set_risk_management", "plot", "train",
    "set_object_store", "download", "add_index_option", "add_future_option",
]


def _looks_like_security_type(x) -> bool:
    """A SecurityType argument, as opposed to a ticker. LEAN writes it as
    SecurityType.Equity or the bare word; tickers are short and all-caps."""
    t = str(x)
    return "." in t or t.lower() in (
        "equity", "option", "future", "forex", "crypto", "cfd", "index",
        "base", "commodity", "futureoption", "indexoption", "cryptofuture")


def _reject_selector(call: str, selector):
    """LEAN's `selector` picks WHICH field feeds an indicator (Field.High,
    a lambda). Indicators here are fed the bar CLOSE, always — accepting a
    selector and ignoring it returns a close-based indicator to code that
    asked for a high-based one, and nothing about the result says so."""
    if selector is not None:
        unsupported_arg(call, "selector", selector,
                        "indicators are computed on the bar close")


class _NotificationManager:
    """self.notify.email(...) / .sms(...) / .web(...).

    LEAN's notify is a manager object, not a method. There is no delivery
    channel in a network-less sandbox, so each call logs what it would have
    sent rather than pretending it was delivered — a strategy relying on an
    alert should be able to see, in the log, that none was sent.
    """

    def __init__(self, log):
        self._log = log

    def email(self, address, subject="", message="", data="", *a, **k):
        self._log(f"Notify.email (not delivered — the sandbox has no "
                  f"network) to={address} subject={subject!r}")

    def sms(self, phone_number, message="", *a, **k):
        self._log(f"Notify.sms (not delivered — the sandbox has no network) "
                  f"to={phone_number}")

    def web(self, address, data="", *a, **k):
        self._log(f"Notify.web (not delivered — the sandbox has no network) "
                  f"to={address}")

    def telegram(self, id="", message="", token=None, *a, **k):
        self._log("Notify.telegram (not delivered — the sandbox has no "
                  "network)")


class _Settings:
    """algorithm.settings — accepts any assignment, unknown reads are None.
    These knobs tune LEAN's portfolio construction; none of them apply here,
    and failing on them would break pasted code over a no-op."""

    def __getattr__(self, name):
        return None


def resolve_hook(algo, snake: str, pascal: str):
    """User hook dispatch: a subclass may override either spelling. Prefer a
    subclass's snake_case override; else its PascalCase override; else the
    base no-op."""
    cls = type(algo)
    base = QCAlgorithm
    if getattr(cls, snake) is not getattr(base, snake):
        return getattr(algo, snake)
    if getattr(cls, pascal, None) is not None and \
            getattr(cls, pascal) is not getattr(base, pascal, None):
        return getattr(algo, pascal)
    return getattr(algo, snake)


@alias_methods
class QCAlgorithm(PascalMixin):
    def __init__(self):
        self.time: datetime | None = None
        self.securities: dict[str, Security] = {}
        self._prices: dict[str, float] = {}
        self.portfolio = None            # injected by PyBacktester
        self.transactions = None         # injected by PyBacktester
        self._book = None                # injected by PyBacktester
        self._store = None               # injected by PyBacktester (history)
        self._calendar = None            # injected by PyBacktester
        self.schedule = Schedule()
        self.date_rules = DateRules()
        self.time_rules = TimeRules()
        self.settings = _Settings()
        self.notify = _NotificationManager(self.log)
        self._start_date: date | None = None
        self._end_date: date | None = None
        self._cash: float = 100000.0
        self._benchmark: str | None = None
        self._warmup_days: int = 0
        self._account_leverage: float | None = None   # set_brokerage_model
        self._security_initializer = None
        self._indicators: dict[str, list] = {}   # sym -> [(resolution, indicator)]
        self._consolidators: dict[str, list] = {}   # sym -> [consolidator]
        self._parameters: dict = {}
        self._runtime_statistics: dict = {}
        self._tags: list = []
        self._quit = False
        self._current_slice = None
        self._market_open = False
        self._max_orders = None
        self._logs: list[str] = []
        self.is_warming_up = False
        self.live_mode = False

    # ---------- configuration ----------

    @staticmethod
    def _to_date(call: str, *args) -> date:
        """LEAN's two spellings: a date/datetime, or (year, month, day)."""
        if len(args) == 1 and isinstance(args[0], (date, datetime)):
            a = args[0]
            return a.date() if isinstance(a, datetime) else a
        if len(args) != 3:
            unsupported(f"{call} with {len(args)} argument(s) — pass a date "
                        f"or (year, month, day)")
        y, m, d = args
        return date(int(y), int(m), int(d))

    def set_start_date(self, *args):
        self._start_date = self._to_date("set_start_date", *args)

    def set_end_date(self, *args):
        self._end_date = self._to_date("set_end_date", *args)

    def set_cash(self, cash):
        self._cash = float(cash)

    def set_benchmark(self, symbol):
        self._benchmark = str(symbol).upper()

    def set_brokerage_model(self, brokerage=None, account_type=None,
                            *a, **k):
        """The brokerage itself is cosmetic here, but the ACCOUNT TYPE is not:
        LEAN gives a margin account 2x by default, and code that says
        AccountType.Margin and never calls set_leverage is relying on exactly
        that. Ignoring it used to run such a strategy at 1x, silently.

        Deliberate divergence: LEAN treats a bare set_brokerage_model(broker)
        as a margin account. Here that stays 1x — inferring 2x from a line
        that says nothing about leverage would quietly double the risk of
        every strategy that only meant to name its broker."""
        reject_extra("set_brokerage_model", a, k, known=("model",))
        for v in [brokerage, account_type] + list(k.values()):
            if v is None:
                continue
            name = str(v).rsplit(".", 1)[-1].strip().lower()
            if name == "margin":
                self._account_leverage = 2.0
            elif name == "cash":
                self._account_leverage = 1.0

    def set_security_initializer(self, initializer):
        """Run on every subscription, existing and future. This is the usual
        LEAN idiom for setting leverage across a universe in one line, so a
        no-op here reads as a leverage override."""
        self._security_initializer = initializer
        for sec in self.securities.values():
            self._init_security(sec)

    def _init_security(self, sec: Security):
        init = self._security_initializer
        if init is None:
            return
        # a SecurityInitializer subclass exposes .initialize; a plain callable
        # (lambda s: s.set_leverage(2)) is itself the hook
        fn = getattr(init, "initialize", None) or getattr(init, "Initialize", None)
        (fn or init)(sec)

    def _effective_leverage(self) -> float:
        """The run's margin ceiling, recomputed on demand. A security that
        declares its own leverage uses it; the rest inherit the account
        default. One sleeve means one ceiling, so the highest wins."""
        default = self._account_leverage or 1.0
        levs = [s.leverage or default for s in self.securities.values()]
        return max(1.0, max(levs) if levs else default)

    def set_time_zone(self, tz=None):
        if tz is None:
            return
        name = str(tz)
        if name not in ("America/New_York", "US/Eastern", "New York", "NewYork"):
            unsupported(f"set_time_zone({name!r})")

    def set_warm_up(self, arg, resolution=None):
        if isinstance(arg, timedelta):
            self._warmup_days = arg.days
        elif resolution in (None, Resolution.DAILY):
            self._warmup_days = int(arg)
        elif resolution == Resolution.MINUTE:
            self._warmup_days = max(1, math.ceil(int(arg) / 390))
        elif resolution == Resolution.SECOND:
            self._warmup_days = max(1, math.ceil(int(arg) / (390 * 60)))
        else:
            unsupported(f"set_warm_up(resolution={resolution})")

    set_warmup = set_warm_up

    # ---------- universe ----------

    # LEAN's positional order — market, fill_forward, LEVERAGE,
    # extended_market_hours — is named out here rather than swallowed by *a,
    # so add_equity("TQQQ", Resolution.MINUTE, leverage=2) means 2x.
    def add_equity(self, ticker, resolution=Resolution.MINUTE, market=None,
                   fill_forward=None, leverage=None,
                   extended_market_hours=None, data_normalization_mode=None,
                   *a, **k) -> Security:
        if resolution == Resolution.HOUR:
            unsupported("Resolution.HOUR")
        reject_extra("add_equity", a, k)
        if market is not None and str(market).rsplit(".", 1)[-1].lower() \
                not in ("usa", "us"):
            unsupported_arg("add_equity", "market", market,
                            "US equities only")
        if fill_forward is False:
            unsupported_arg("add_equity", "fill_forward", fill_forward,
                            "bars are always filled forward here")
        if extended_market_hours:
            unsupported_arg("add_equity", "extended_market_hours",
                            extended_market_hours,
                            "regular-session bars only (09:30-16:00 ET)")
        if data_normalization_mode is not None and \
                str(data_normalization_mode).rsplit(".", 1)[-1].lower() \
                not in ("adjusted", "none"):
            unsupported_arg("add_equity", "data_normalization_mode",
                            data_normalization_mode,
                            "bars are split/dividend ADJUSTED here")
        sym = Symbol(ticker)
        sec = self.securities.get(sym)
        if sec is None:
            sec = Security(sym, resolution, Exchange(ExchangeHours(self._calendar)))
            self.securities[sym] = sec
            self._init_security(sec)
        if leverage is not None:            # explicit arg beats the initializer
            sec.set_leverage(leverage)
        return sec

    # ---------- orders (delegate to the injected book) ----------

    def _require_book(self):
        if self._book is None:
            raise RuntimeError("orders are only available while a backtest is running")
        return self._book

    def _px(self, symbol) -> float:
        return self._prices.get(str(symbol).upper(), 0.0)

    def market_order(self, symbol, quantity, tag="", asynchronous=False):
        book = self._require_book()
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return book.market(symbol, qty, price=self._px(symbol), tag=tag)

    def _invalid_ticket(self, symbol, qty, tag):
        from .enums import OrderStatus, OrderType
        book = self._require_book()
        t = book._new_ticket(symbol, qty, OrderType.MARKET, tag)
        t.status = OrderStatus.INVALID
        return t

    def limit_order(self, symbol, quantity, limit_price, tag=""):
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return self._require_book().limit(symbol, qty, limit_price, tag=tag)

    def stop_market_order(self, symbol, quantity, stop_price, tag=""):
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return self._require_book().stop_market(symbol, qty, stop_price, tag=tag)

    def stop_limit_order(self, symbol, quantity, stop_price, limit_price, tag=""):
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return self._require_book().stop_limit(symbol, qty, stop_price,
                                               limit_price, tag=tag)

    def liquidate(self, symbol=None, tag="Liquidated", *a,
                  symbols=None, symbol_to_liquidate=None, **k):
        """LEAN spells the target three ways and also accepts a list. Taking
        only a scalar meant str(["A","B"]) became one nonsense ticker that
        matched nothing and silently liquidated NOTHING."""
        reject_extra("liquidate", a, k, known=("asynchronous",))
        target = next((x for x in (symbol, symbols, symbol_to_liquidate)
                       if x is not None), None)
        book = self._require_book()
        if target is None:
            syms = [s for s, q in book.sleeve.qty.items() if q != 0]
        elif isinstance(target, (list, tuple, set)):
            syms = [str(x).upper() for x in target]
        else:
            syms = [str(target).upper()]
        for s in syms:
            for t in book.open_tickets(s):
                book.cancel(t)
            q = book.sleeve.qty.get(s, 0)
            if q != 0:
                book.market(s, -q, price=self._px(s), tag=tag)

    def set_holdings(self, symbol, percentage=None, liquidate_existing=False,
                     tag="", **kwargs):
        liquidate_existing = liquidate_existing or \
            kwargs.pop("liquidate_existing_holdings", False)
        reject_extra("set_holdings", (), kwargs, known=("asynchronous",))
        book = self._require_book()
        if isinstance(symbol, (list, tuple)):
            # list of PortfolioTarget(symbol, weight): weights of total
            # portfolio value. Sells go first so the freed cash funds the buys.
            tpv = self.portfolio.total_portfolio_value
            wanted = {}
            for t in symbol:
                s = str(getattr(t, "symbol", t)).upper()
                wanted[s] = float(getattr(t, "quantity", 0.0))
            if liquidate_existing:
                for s, q in list(book.sleeve.qty.items()):
                    if s not in wanted and q != 0:
                        self.liquidate(s, tag=tag or "Liquidated")
            deltas = []
            for s, w in wanted.items():
                price = self._px(s)
                if price <= 0:
                    continue
                current = book.sleeve.qty.get(s, 0)
                delta = int((w * tpv - current * price) / price)
                if delta != 0:
                    deltas.append((s, delta, price))
            for s, delta, price in sorted(deltas, key=lambda d: d[1]):
                book.market(s, delta, price=price, tag=tag)
            return None
        sym = str(symbol).upper()
        if liquidate_existing:
            for s, q in list(book.sleeve.qty.items()):
                if s != sym and q != 0:
                    self.liquidate(s, tag=tag or "Liquidated")
        price = self._px(sym)
        if price <= 0:
            return None
        target_value = float(percentage) * self.portfolio.total_portfolio_value
        current = book.sleeve.qty.get(sym, 0)
        delta = int((target_value - current * price) / price)
        if delta != 0:
            return book.market(sym, delta, price=price, tag=tag)
        return None

    # ---------- data ----------

    def history(self, symbols, periods, resolution=None):
        from .history import history as _history
        return _history(self, symbols, periods, resolution)

    # ---------- indicators ----------

    # ---------- indicator warm-up ----------

    def warm_up_indicator(self, symbol, indicator, resolution=None,
                          selector=None, *a, **k):
        """Feed an indicator enough history to be ready before the first bar.

        Without this an indicator trades COLD for its whole warm-up window
        and nothing about the result says so — the same silent-wrongness
        this runtime exists to avoid, just one level up.
        """
        reject_extra("warm_up_indicator", a, k)
        _reject_selector("warm_up_indicator", selector)
        period = getattr(indicator, "warm_up_period", None) \
            or getattr(indicator, "period", None)
        if not period:
            unsupported("warm_up_indicator on an indicator with no period")
        frame = self.history(symbol, int(period), self._ind_res(resolution))
        self._replay_history(indicator, frame)
        return indicator

    def indicator_history(self, indicator, symbol, period, resolution=None,
                          selector=None, *a, **k):
        """Run `indicator` over `period` bars of history and return the
        series of values it produced, without disturbing a live one."""
        reject_extra("indicator_history", a, k)
        _reject_selector("indicator_history", selector)
        frame = self.history(symbol, int(period), self._ind_res(resolution))
        return self._replay_history(indicator, frame, collect=True)

    @staticmethod
    def _replay_history(indicator, frame, collect=False):
        """Push a history frame through an indicator in time order."""
        out = []
        if frame is None or len(frame) == 0:
            return out if collect else indicator
        bar_based = hasattr(indicator, "update_bar")
        rows = frame.reset_index().to_dict("records") if hasattr(
            frame, "reset_index") else []
        for r in rows:
            t = r.get("time")
            if bar_based:
                indicator.update_bar(t, float(r["high"]), float(r["low"]),
                                     float(r["close"]),
                                     float(r.get("volume") or 0.0),
                                     float(r.get("open") or r["close"]))
            else:
                indicator.update(t, float(r["close"]))
            if collect:
                out.append(indicator.value)
        return out if collect else indicator

    def set_finished_warming_up(self):
        self.is_warming_up = False
        if self._book is not None:
            self._book.allow_orders = True

    # ---------- order conveniences ----------

    def buy(self, symbol, quantity, *a, **k):
        """LEAN's shorthand: always a positive-quantity market order."""
        reject_extra("buy", a, k, known=("tag", "asynchronous"))
        return self.market_order(symbol, abs(int(quantity)),
                                 tag=k.get("tag", ""))

    def sell(self, symbol, quantity, *a, **k):
        reject_extra("sell", a, k, known=("tag", "asynchronous"))
        return self.market_order(symbol, -abs(int(quantity)),
                                 tag=k.get("tag", ""))

    def order(self, symbol, quantity, *a, **k):
        """LEAN's generic order — a signed market order."""
        reject_extra("order", a, k, known=("tag", "asynchronous"))
        return self.market_order(symbol, int(quantity), tag=k.get("tag", ""))

    # ---------- prices ----------

    def get_last_known_price(self, symbol, *a, **k):
        """The last price this runtime has seen for `symbol`, or None."""
        reject_extra("get_last_known_price", a, k)
        px = self._px(symbol)
        return px if px > 0 else None

    def get_last_known_prices(self, symbol=None, *a, **k):
        reject_extra("get_last_known_prices", a, k)
        if symbol is None:
            return {s: p for s, p in self._prices.items() if p > 0}
        px = self._px(symbol)
        return {str(symbol).upper(): px} if px > 0 else {}

    # ---------- securities ----------

    def add_security(self, *a, **k) -> Security:
        """LEAN's generic add_security. Equities only here, so a leading
        SecurityType is accepted when it says equity and refused BY NAME
        otherwise — never silently subscribed as a stock."""
        args = list(a)
        st = k.pop("security_type", None)
        if args and _looks_like_security_type(args[0]):
            st = args.pop(0)
        if st is not None and str(st).rsplit(".", 1)[-1].lower() not in (
                "equity", "base"):
            unsupported_arg("add_security", "security_type", st,
                            "US equities only")
        return self.add_equity(*args, **k)

    def remove_security(self, symbol, *a, **k):
        """Drop a subscription. Any resting orders on it are cancelled — a
        subscription that stops streaming cannot honour them."""
        reject_extra("remove_security", a, k)
        sym = Symbol(symbol)
        if sym not in self.securities:
            return False
        if self._book is not None:
            for t in self._book.open_tickets(str(sym)):
                self._book.cancel(t)
        self.securities.pop(sym, None)
        self._indicators.pop(str(sym), None)
        self._consolidators.pop(str(sym), None)
        return True

    # ---------- message logs ----------

    @property
    def log_messages(self):
        return list(self._logs)

    @property
    def debug_messages(self):
        return list(self._logs)

    @property
    def error_messages(self):
        return [m for m in self._logs if m.startswith("ERROR:")]

    # ---------- runtime & account (design 2026-09-05 §7) ----------

    @property
    def start_date(self):
        return self._start_date

    @property
    def end_date(self):
        return self._end_date

    @property
    def benchmark(self):
        return self._benchmark

    @property
    def account_currency(self) -> str:
        return "USD"                 # US equities only

    @property
    def brokerage_model(self):
        """Not LEAN's model object — the account leverage is the only part
        of it this runtime acts on, so that is what it reports."""
        return {"account_leverage": self._account_leverage or 1.0}

    @property
    def active_securities(self):
        return self.securities

    @property
    def current_slice(self):
        return self._current_slice

    @property
    def is_market_open(self):
        return self._market_open

    def set_parameters(self, params: dict):
        self._parameters.update({str(k): v for k, v in (params or {}).items()})

    def get_parameter(self, name, default=None):
        """LEAN returns parameters as STRINGS; get_parameter(name, 5) coerces
        to the default's type, which is the behaviour pasted code expects."""
        if name not in self._parameters:
            return default
        raw = self._parameters[name]
        if default is None or isinstance(raw, type(default)):
            return raw
        try:
            return type(default)(raw)
        except (TypeError, ValueError):
            return default

    def get_parameters(self):
        return dict(self._parameters)

    def quit(self, message=""):
        """Stop the algorithm. The backtester checks this after each bar and
        ends the run cleanly, keeping everything filled so far."""
        self._quit = True
        if message:
            self.log(f"Quit: {message}")

    def set_quit(self, quit_flag=True):
        self._quit = bool(quit_flag)

    @property
    def status(self):
        return "Stopped" if self._quit else "Running"

    def set_runtime_statistic(self, name, value):
        self._runtime_statistics[str(name)] = value

    @property
    def runtime_statistics(self):
        return dict(self._runtime_statistics)

    def add_tag(self, tag):
        self._tags.append(str(tag))

    def set_tags(self, tags):
        self._tags = [str(t) for t in (tags or [])]

    @property
    def tags(self):
        return list(self._tags)

    # ---------- consolidators (design 2026-09-05 §6) ----------

    def consolidate(self, symbol, period, handler=None, *a, **k):
        """Aggregate `symbol`'s bars into `period` buckets and call
        `handler(bar)` on each completed one. This is how an hourly (or
        4-hourly, or weekly) strategy runs on a minute feed."""
        from .consolidators import TradeBarConsolidator
        reject_extra("consolidate", a, k, known=("tick_type",))
        c = TradeBarConsolidator(period)
        if handler is not None:
            c.add_handler(handler)
        self._consolidators.setdefault(str(symbol).upper(), []).append(c)
        return c

    def create_consolidator(self, period, *a, **k):
        from .consolidators import TradeBarConsolidator
        reject_extra("create_consolidator", a, k)
        return TradeBarConsolidator(period)

    def add_consolidator(self, symbol, consolidator, *a, **k):
        reject_extra("add_consolidator", a, k)
        self._consolidators.setdefault(str(symbol).upper(), []).append(
            consolidator)
        return consolidator

    def subscription_manager_add_consolidator(self, symbol, consolidator):
        return self.add_consolidator(symbol, consolidator)

    def register_indicator(self, symbol, indicator, consolidator=None,
                           selector=None, *a, **k):
        """Feed `indicator` from `consolidator` (or a period/Resolution),
        rather than from the raw bar stream."""
        reject_extra("register_indicator", a, k)
        _reject_selector("register_indicator", selector)
        # self.sma(...) already put it on the automatic feed. Registering it
        # against a consolidator as well would feed it TWICE per bar — once
        # aggregated, once raw — which silently corrupts the series rather
        # than failing. The explicit registration wins.
        self.deregister_indicator(indicator)
        if consolidator is None:
            return self._register_indicator(symbol, Resolution.DAILY,
                                            indicator)
        if not hasattr(consolidator, "add_handler"):
            consolidator = self.consolidate(symbol, consolidator)
        elif consolidator not in self._consolidators.get(
                str(symbol).upper(), []):
            self.add_consolidator(symbol, consolidator)

        bar_based = hasattr(indicator, "update_bar")

        def _feed(bar, ind=indicator, bar_based=bar_based):
            if bar_based:
                ind.update_bar(bar.end_time, bar.high, bar.low, bar.close,
                               bar.volume, bar.open)
            else:
                ind.update(bar.end_time, bar.close)

        consolidator.add_handler(_feed)
        return indicator

    def deregister_indicator(self, indicator, *a, **k):
        reject_extra("deregister_indicator", a, k)
        for regs in self._indicators.values():
            for entry in list(regs):
                if entry[1] is indicator:
                    regs.remove(entry)

    unregister_indicator = deregister_indicator

    def _register_indicator(self, symbol, resolution, ind):
        self._indicators.setdefault(str(symbol).upper(), []).append((resolution, ind))
        return ind

    def _ind_res(self, resolution):
        if resolution is None:
            return Resolution.DAILY
        if isinstance(resolution, timedelta):
            unsupported("indicator with timedelta resolution")
        return resolution

    def sma(self, symbol, period, resolution=None, selector=None,
            *a, **k):
        from .indicators import SimpleMovingAverage
        reject_extra("sma", a, k)
        _reject_selector("sma", selector)
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        SimpleMovingAverage(period))

    def ema(self, symbol, period, smoothing_factor=None, resolution=None,
            selector=None, *a, **k):
        from .indicators import ExponentialMovingAverage
        reject_extra("ema", a, k)
        _reject_selector("ema", selector)
        if smoothing_factor is not None:
            unsupported_arg("ema", "smoothing_factor", smoothing_factor,
                            "the smoothing factor is derived from `period`")
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        ExponentialMovingAverage(period))

    def rsi(self, symbol, period, moving_average_type=None, resolution=None,
            selector=None, *a, **k):
        from .indicators import RelativeStrengthIndex
        reject_extra("rsi", a, k)
        _reject_selector("rsi", selector)
        if moving_average_type is not None and \
                str(moving_average_type).rsplit(".", 1)[-1].lower() != "wilders":
            unsupported_arg("rsi", "moving_average_type",
                            moving_average_type,
                            "only Wilder smoothing is implemented")
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        RelativeStrengthIndex(period))

    def std(self, symbol, period, resolution=None, selector=None,
            *a, **k):
        from .indicators import StandardDeviation
        reject_extra("std", a, k)
        _reject_selector("std", selector)
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        StandardDeviation(period))

    def max(self, symbol, period, resolution=None, selector=None,
            *a, **k):
        from .indicators import Maximum
        reject_extra("max", a, k)
        _reject_selector("max", selector)
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        Maximum(period))

    def min(self, symbol, period, resolution=None, selector=None,
            *a, **k):
        from .indicators import Minimum
        reject_extra("min", a, k)
        _reject_selector("min", selector)
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        Minimum(period))

    def atr(self, symbol, period, moving_average_type=None, resolution=None,
            selector=None, *a, **k):
        from .indicators import AverageTrueRange
        reject_extra("atr", a, k)
        _reject_selector("atr", selector)
        if moving_average_type is not None and \
                str(moving_average_type).rsplit(".", 1)[-1].lower() != "wilders":
            unsupported_arg("atr", "moving_average_type",
                            moving_average_type,
                            "only Wilder smoothing is implemented")
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        AverageTrueRange(period))


    def market_on_open_order(self, symbol, quantity, tag="", *a, **k):
        reject_extra("market_on_open_order", a, k, known=("asynchronous",))
        book = self._require_book()
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return book.market_on_open(symbol, qty, tag=tag)

    def market_on_close_order(self, symbol, quantity, tag="", *a, **k):
        reject_extra("market_on_close_order", a, k, known=("asynchronous",))
        book = self._require_book()
        qty = int(quantity)
        if qty == 0:
            return self._invalid_ticket(symbol, 0, tag)
        return book.market_on_close(symbol, qty, tag=tag)

    def trailing_stop_order(self, symbol, quantity, trailing_amount=None,
                            trailing_as_percentage=True, tag="", *a, **k):
        reject_extra("trailing_stop_order", a, k, known=("asynchronous",))
        book = self._require_book()
        qty = int(quantity)
        if qty == 0 or trailing_amount is None:
            return self._invalid_ticket(symbol, qty, tag)
        return book.trailing_stop(symbol, qty, trailing_amount,
                                  as_percentage=trailing_as_percentage,
                                  tag=tag)

    def limit_if_touched_order(self, symbol, quantity, trigger_price=None,
                               limit_price=None, tag="", *a, **k):
        reject_extra("limit_if_touched_order", a, k, known=("asynchronous",))
        book = self._require_book()
        qty = int(quantity)
        if qty == 0 or trigger_price is None or limit_price is None:
            return self._invalid_ticket(symbol, qty, tag)
        return book.limit_if_touched(symbol, qty, trigger_price, limit_price,
                                     tag=tag)

    def calculate_order_quantity(self, symbol, target) -> int:
        """Shares needed to reach `target` as a fraction of portfolio value —
        the sizing set_holdings does, exposed so strategies can inspect it."""
        px = self._px(symbol)
        if px <= 0:
            return 0
        sym = str(symbol).upper()
        held = self._book.sleeve.qty.get(sym, 0) if self._book else 0
        tpv = self.portfolio.total_portfolio_value if self.portfolio else 0.0
        return int(float(target) * tpv / px) - int(held)

    def shortable(self, symbol, quantity=0, *a, **k) -> bool:
        """No borrow feed here, so every subscribed symbol is shortable.
        Stated rather than assumed: a strategy that checks this and gets
        True must not be surprised at the broker, and the broker-side
        answer is Caps.supports_short (dqengine/adapters/base.py)."""
        reject_extra("shortable", a, k)
        return str(symbol).upper() in self.securities

    def shortable_quantity(self, symbol, *a, **k):
        reject_extra("shortable_quantity", a, k)
        return None      # LEAN: None means "no limit known"

    def set_maximum_orders(self, maximum: int):
        self._max_orders = int(maximum)

    # ---------- indicator library (design 2026-09-05 §4) ----------
    # LEAN's method name -> our class. Registered below with a shared factory
    # so every one of them gets the same selector/extra-arg rejection as the
    # hand-written seven above; a new indicator cannot be added silently.
    _IND_VALUE = {
        "identity": "Identity", "sum": "Sum", "mom": "Momentum",
        "momp": "MomentumPercent", "roc": "RateOfChange",
        "rocp": "RateOfChangePercent", "logr": "LogReturn",
        "var": "Variance", "mad": "MeanAbsoluteDeviation",
        "lwma": "LinearWeightedMovingAverage",
        "trima": "TriangularMovingAverage",
        "dema": "DoubleExponentialMovingAverage",
        "tema": "TripleExponentialMovingAverage",
        "hma": "HullMovingAverage", "wwma": "WilderMovingAverage",
        "zlema": "ZeroLagExponentialMovingAverage",
        "cmo": "ChandeMomentumOscillator", "trix": "Trix",
        "rocr": "RateOfChangeRatio", "kama": "KaufmanAdaptiveMovingAverage",
        "ker": "KaufmanEfficiencyRatio", "t_3": "T3MovingAverage",
        "mgd": "McGinleyDynamic", "lsma": "LeastSquaresMovingAverage",
        "tsf": "TimeSeriesForecast", "dpo": "DetrendedPriceOscillator",
        "momersion": "Momersion", "cc": "CoppockCurve",
        "tsi": "TrueStrengthIndex",
        "crsi": "ConnorsRelativeStrengthIndex",
        "srsi": "StochasticRelativeStrengthIndex", "he": "HurstExponent",
        "rc": "RegressionChannel", "vidya": "VariableIndexDynamicAverage",
        "kst": "KnowSureThing", "do": "DerivativeOscillator",
        "var": "ValueAtRisk", "tdd": "TargetDownsideDeviation",
        "sr": "SharpeRatio",
        "rma": "RelativeMovingAverage", "alma": "ArnaudLegouxMovingAverage",
        "aps": "AugenPriceSpike", "stc": "SchaffTrendCycle",
    }
    _IND_BAR = {
        "tr": "TrueRange", "natr": "NormalizedAverageTrueRange",
        "midpoint": "MidPoint", "midprice": "MidPrice",
        "ibs": "InternalBarStrength", "bop": "BalanceOfPower",
        "wilr": "WilliamsPercentR", "cci": "CommodityChannelIndex",
        "sto": "Stochastic", "dch": "DonchianChannel",
        "kch": "KeltnerChannels", "adx": "AverageDirectionalIndex",
        "aroon": "AroonOscillator", "obv": "OnBalanceVolume",
        "ad": "AccumulationDistribution", "mfi": "MoneyFlowIndex",
        "cmf": "ChaikinMoneyFlow",
        "vwap": "VolumeWeightedAveragePriceIndicator",
        "vwma": "VolumeWeightedMovingAverage",
        "psar": "ParabolicStopAndReverse", "ar": "AverageRange",
        "sobv": "SmoothedOnBalanceVolume", "chop": "ChoppinessIndex",
        "mass": "MassIndex", "emv": "EaseOfMovementValue",
        "fi": "ForceIndex", "ultosc": "UltimateOscillator",
        "dem": "DeMarkerIndicator", "ao": "AwesomeOscillator",
        "rsv": "RogersSatchellVolatility",
        "adosc": "AccumulationDistributionOscillator",
        "adxr": "AverageDirectionalMovementIndexRating",
        "heikin_ashi": "HeikinAshi", "str": "SuperTrend", "vtx": "Vortex",
        "abands": "AccelerationBands", "si": "WilderSwingIndex",
        "asi": "WilderAccumulativeSwingIndex",
        "pso": "PremierStochasticOscillator", "co": "ChaikinOscillator",
        "ichimoku": "IchimokuKinkoHyo", "cks": "ChandeKrollStop",
        "rvi": "RelativeVigorIndex", "fish": "FisherTransform",
        "frama": "FractalAdaptiveMovingAverage",
        "kvo": "KlingerVolumeOscillator", "wto": "WaveTrendOscillator",
    }
    _IND_MULTI = {
        "macd": "MovingAverageConvergenceDivergence",
        "bb": "BollingerBands", "apo": "AbsolutePriceOscillator",
        "ppo": "PercentagePriceOscillator",
    }

    _IND_DUAL = {"b": "Beta", "c": "Correlation", "cov": "Covariance"}

    def _make_dual(self, call, cls_name, target, reference, period,
                   resolution, extra_a, extra_k):
        """Dual-symbol indicators are registered against BOTH tickers; the
        bar feed routes each symbol's bars in through update_symbol."""
        from . import indicators as _ind
        reject_extra(call, extra_a, extra_k, known=("correlation_type",))
        cls = getattr(_ind, cls_name)
        ind = cls(target, reference, period, **{
            k: v for k, v in extra_k.items() if k == "correlation_type"})
        res = self._ind_res(resolution)
        for sym in (target, reference):
            self._register_indicator(sym, res, ind)
        return ind

    def _make_indicator(self, call, cls_name, symbol, args, resolution,
                        selector, extra_a, extra_k):
        from . import indicators as _ind
        reject_extra(call, extra_a, extra_k)
        _reject_selector(call, selector)
        cls = getattr(_ind, cls_name)
        return self._register_indicator(symbol, self._ind_res(resolution),
                                        cls(*args))

    # ---------- hooks (defaults) ----------

    def initialize(self):
        pass

    def on_data(self, data):
        pass

    def on_order_event(self, order_event):
        pass

    def on_end_of_day(self, symbol=None):
        pass

    def on_end_of_algorithm(self):
        pass

    def on_warmup_finished(self):
        pass

    def on_end_of_time_step(self):
        pass

    def on_margin_call_warning(self):
        pass

    def on_margin_call(self, requests=None):
        """LEAN lets a strategy edit the liquidation requests. There is no
        margin-call engine here, so it is a hook that never fires rather
        than one that fires wrongly — see the note on the property below."""
        return requests

    def on_splits(self, splits):
        pass

    def on_dividends(self, dividends):
        pass

    # ---------- logging ----------

    def log(self, msg):
        if len(self._logs) < MAX_LOG_LINES:
            self._logs.append(str(msg))
        elif len(self._logs) == MAX_LOG_LINES:
            self._logs.append("…log truncated")

    def debug(self, msg):
        self.log(msg)

    def error(self, msg):
        self.log(f"ERROR: {msg}")


# The out-of-subset surface: real methods that fail loudly by name.
def _mk_unsupported(name):
    def _fn(self, *a, **k):
        unsupported(name)
    _fn.__name__ = name
    return _fn


for _name in _UNSUPPORTED:
    setattr(QCAlgorithm, _name, _mk_unsupported(_name))
    _pascal = "".join(p.capitalize() for p in _name.split("_"))
    setattr(QCAlgorithm, _pascal, getattr(QCAlgorithm, _name))
del _name, _pascal


def _mk_indicator_method(call: str, cls_name: str, positional: int):
    """One LEAN indicator helper. `positional` is how many numeric arguments
    come before `resolution` — 1 for the period-only majority, 3 for the
    fast/slow/signal family."""
    if positional == 1:
        def _m(self, symbol, period=None, resolution=None, selector=None,
               *a, **k):
            args = () if period is None else (period,)
            return self._make_indicator(call, cls_name, symbol, args,
                                        resolution, selector, a, k)
    else:
        def _m(self, symbol, fast_period=None, slow_period=None,
               signal_period=None, resolution=None, selector=None, *a, **k):
            args = tuple(x for x in (fast_period, slow_period, signal_period)
                         if x is not None)
            return self._make_indicator(call, cls_name, symbol, args,
                                        resolution, selector, a, k)
    _m.__name__ = call
    _m.__doc__ = f"LEAN {call}() -> {cls_name}"
    return _m


for _call, _cls in {**QCAlgorithm._IND_VALUE, **QCAlgorithm._IND_BAR}.items():
    if not hasattr(QCAlgorithm, _call):
        setattr(QCAlgorithm, _call, _mk_indicator_method(_call, _cls, 1))
for _call, _cls in QCAlgorithm._IND_MULTI.items():
    if not hasattr(QCAlgorithm, _call):
        setattr(QCAlgorithm, _call, _mk_indicator_method(_call, _cls, 3))


def _mk_dual_method(call: str, cls_name: str):
    def _m(self, target, reference, period, resolution=None, *a, **k):
        return self._make_dual(call, cls_name, target, reference, period,
                               resolution, a, k)
    _m.__name__ = call
    _m.__doc__ = f"LEAN {call}(target, reference, period) -> {cls_name}"
    return _m


for _call, _cls in QCAlgorithm._IND_DUAL.items():
    if not hasattr(QCAlgorithm, _call):
        setattr(QCAlgorithm, _call, _mk_dual_method(_call, _cls))
# PascalCase aliases, same rule as the hand-written surface
for _call in list(QCAlgorithm._IND_VALUE) + list(QCAlgorithm._IND_BAR) + \
        list(QCAlgorithm._IND_MULTI) + list(QCAlgorithm._IND_DUAL):
    _p = "".join(part.capitalize() for part in _call.split("_"))
    if not hasattr(QCAlgorithm, _p):
        setattr(QCAlgorithm, _p, getattr(QCAlgorithm, _call))
