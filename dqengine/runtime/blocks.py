"""Block-strategy runtime context: ONE IndicatorEngine, ONE WeightEngine.

Generated code (dqengine.codegen) does not reimplement the indicator layer. It
evaluates every market-data leaf through this object, which drives exactly
the objects the IR engine drives:

    dqengine.runtime.core.indicators.IndicatorEngine  daily closes, prices,
                                                indicators
    dqengine.runtime.core.weights.WeightEngine        asset/equal/weighted/
                                                inverse_vol/if/best, with
                                                the shadow-return state
                                                `best` ranks on
    dqengine.runtime.core.exprs.EvalContext           the expression environment

so an sma, an rsi, an atr, a cross-symbol leaf or a pct_rank computed here
and the same leaf computed by the IR engine are ONE computation, not two
that have to be kept equal by hand. That is dqengine/runtime/core/fills.py's
argument applied to the indicator layer — and it is what makes multi-symbol,
cross-symbol, custom metrics and derived series parity by construction
rather than by test.

This is a LIBRARY module, like allocation.py. It is NOT part of the
QCAlgorithm API surface: user code never sees it, only generated code
imports it.

Feeding order mirrors Backtester._prepare_multi / _end_session_multi:

  warm()            the last 260 union-calendar sessions BEFORE the run's
                    start, close-only, from MINUTE bars (never the daily
                    zips: they disagree by about a cent, and a cent flips a
                    `best` ranking at the margin);
  start_session()   this session's opening print per symbol;
  on_bar()          per-session OHLC — only when the strategy reads `atr`,
                    the one indicator that reads highs and lows;
  close_session()   on_session_close + prior_close + last_price pinned to
                    the session's close for every symbol, a symbol with no
                    data today carrying its prior close forward (a 0.0
                    return) rather than dropping out of the series and
                    shifting every rolling window after it;
  mark_prices()     at each trigger batch, last_price is the current price
                    — what the IR engine sets before firing rules on a bar.
"""
from __future__ import annotations

from datetime import date

from dqengine.runtime.core.exprs import NOT_READY, EvalContext
from dqengine.runtime.core.indicators import IndicatorEngine
from dqengine.runtime.core.weights import WeightEngine

# Pre-start daily closes so a 200-day gate is ready on day one. Same
# constant as Backtester.WARMUP_SESSIONS — if they drift, a strategy warms
# differently on the two engines and the first weeks of a run diverge.
WARMUP_SESSIONS = 260


class NotReady(Exception):
    """A leaf the IR engine would answer NOT_READY.

    Generated code absorbs it in two places, which together reproduce the
    IR semantics exactly: inside `_cmp` (a comparison touching a not-ready
    operand is False, never an error) and around each rule call (a rule
    whose sizing or price is not ready sits this fire out).
    """


class BlockContext:
    """Indicator/weight state for one generated block strategy.

    `warm` is NOT an optimisation flag. The IR engine's single-symbol path
    (`_run_single`) does NO warm-up at all — it starts cold at cfg.start —
    while its multi path (`_prepare_multi`) warms 260 sessions. A generated
    single-symbol strategy whose indicators were warm would be READY on day
    one where the oracle is still warming, and the first weeks would trade
    differently. dqengine.codegen therefore passes `warm=True` exactly when the
    IR engine would take its multi path:

        len(collect_ir_symbols(ir)) > 1 or any set_weights rule
    """

    def __init__(self, algo, params: dict, symbols, *, universe=None,
                 warm: bool = True, track_ohlc: bool = False):
        self.algo = algo
        self._params = dict(params or {})
        self.symbols = [str(s).upper() for s in symbols]
        # `universe` is ir["universe"]["static"]; `symbols` is
        # collect_ir_symbols (universe + every referenced ticker). The IR
        # engine's default symbol for a bare expression leaf is
        # universe[0], never the rule's action symbol — see Backtester._ctx,
        # which passes self.default_symbol for EVERY rule.
        self.universe = [str(s).upper() for s in (universe or self.symbols)]
        self.default_symbol = self.universe[0] if self.universe else None
        self.ind = IndicatorEngine()
        self.weights = WeightEngine(self.ind)
        self.track_ohlc = bool(track_ohlc)
        self._warm_sessions = WARMUP_SESSIONS if warm else 0
        self._warmed = False
        self._day: dict = {}            # per-session OHLC (track_ohlc only)
        self._session_open: dict = {}
        self.snap_entry: dict = {}      # entry prices at the batch start
        self.open_snap: dict = {}       # ... at the session open

    # --------------------------------------------------------- warm-up

    def warm(self) -> None:
        """Pre-start session closes, oldest first. Idempotent.

        Mirrors Backtester._prepare_multi's warm-up EXACTLY, including its
        data source: the last `WARMUP_SESSIONS` union-calendar days before
        the run's start and, for each symbol present on that day, the
        MINUTE bars' session_close.

        MUST NOT be called from initialize(). RunOverrides.start is applied
        AFTER initialize() returns (PyBacktester._setup), and this anchors
        on algo._start_date — so warming in initialize reads history
        relative to whatever placeholder date the generated file carries,
        splicing a 260-session series ending in the wrong YEAR onto the
        real first session. `ensure_warm()` is the safe entry point.
        """
        self._warmed = True
        if not self._warm_sessions:
            return
        store = getattr(self.algo, "_store", None)
        start = getattr(self.algo, "_start_date", None)
        if store is None or start is None:
            return
        day_sets = {}
        for sym in self.symbols:
            try:
                day_sets[sym] = set(store.minute_days(sym))
            except Exception:       # noqa: BLE001 — a symbol with no data
                day_sets[sym] = set()
        all_days = sorted(set().union(*day_sets.values())) if day_sets else []
        for day in [d for d in all_days if d < start][-self._warm_sessions:]:
            for sym in self.symbols:
                if day not in day_sets.get(sym, ()):
                    continue
                bars = store.load_minute_day(sym, day)
                if bars is None or bars.n < 1:
                    continue
                close = float(bars.session_close)
                # close-only, exactly as the IR engine feeds it: highs and
                # lows equal the close over the warm-up window, which is
                # what ATR sees on the block side too
                self.ind.on_session_close(sym, close)
                self.ind.prior_close[sym] = close
                self.ind.last_price[sym] = close

    def ensure_warm(self) -> None:
        if not self._warmed:
            self.warm()

    # ---------------------------------------------------------- params

    def params(self) -> dict:
        """Param values as the indicators should read them.

        The algorithm's class attributes WIN over the baked-in PARAMS dict:
        an ejected file is meant to be edited, and `VOL_W = 25` at the top
        of the class has to be what the indicators compute with. Two
        sources of truth for one number is how it stops being editable.
        """
        algo = self.algo
        return {k: getattr(algo, k, v) for k, v in self._params.items()}

    # --------------------------------------------------------- feeding

    def start_session(self, day: date) -> None:
        """Open-bell bookkeeping: this session's opening print per symbol
        (a market_order sized with ref='session_open' reads it).

        Deliberately does NOT reset the OHLC accumulator. The runtime
        delivers on_data BEFORE the scheduled events of the same bar, so
        this handler runs after the session's first bar has already been
        accumulated — clearing here would drop that bar's high and low from
        every atr. `close_session` empties it instead.
        """
        self.ensure_warm()
        for sym in self.symbols:
            sec = self.algo.securities.get(sym)
            o = float(getattr(sec, "open", 0.0) or 0.0) if sec else 0.0
            if o > 0:
                self._session_open[sym] = o

    def on_bar(self, data) -> None:
        """Per-session OHLC accumulation, in the shape
        Backtester._end_session_multi feeds the daily series.

        Only emitted (and only called) when the strategy actually reads
        `atr` — it is the one indicator that reads highs and lows, and
        overriding on_data forfeits the runtime's quiet-bar fast path for
        the whole run."""
        for sym in self.symbols:
            bar = data.bars.get(sym)
            if bar is None:
                continue
            c, h, l = float(bar.close), float(bar.high), float(bar.low)
            prev = self._day.get(sym)
            self._day[sym] = (c, max(h, prev[1]) if prev else h,
                              min(l, prev[2]) if prev else l)

    def close_session(self, day: date) -> None:
        """One session's closes. Mirrors Backtester._end_session_multi: a
        symbol with no data today carries its prior close forward (a 0.0
        return). The runtime's Security.price already carries the last
        known print across a data-less day, which IS that carried close."""
        self.ensure_warm()
        for sym in self.symbols:
            bar = self._day.get(sym)
            if bar is not None:
                close, hi, lo = bar
            else:
                hi = lo = None
                sec = self.algo.securities.get(sym)
                px = float(getattr(sec, "price", 0.0) or 0.0) if sec else 0.0
                close = px if px > 0 else self.ind.prior_close.get(sym)
                if close is None:
                    continue        # never traded yet: no series to shift
            self.ind.on_session_close(sym, close, hi, lo)
            self.ind.prior_close[sym] = close
            self.ind.last_price[sym] = close
        self._day = {}

    def mark_prices(self) -> None:
        """Current prices at evaluation time — `last_price` is what the
        indicator layer reads as today's price.

        Warms FIRST: warm() pins last_price to each symbol's last
        historical close, so warming after marking would replace today's
        price with a stale one and the first fire would read yesterday."""
        self.ensure_warm()
        for sym in self.symbols:
            sec = self.algo.securities.get(sym)
            px = float(getattr(sec, "price", 0.0) or 0.0) if sec else 0.0
            if px:
                self.ind.last_price[sym] = px

    # ----------------------------------------------------------- leaves

    def indicator(self, node: dict, symbol: str | None = None) -> float:
        """One indicator leaf, computed by the IR engine's own
        IndicatorEngine. NOT_READY becomes NotReady, which generated code
        already knows how to absorb."""
        sym = str(symbol or node.get("symbol")
                  or self.default_symbol or "").upper()
        v = self.ind.value(node, sym, self.params())
        if v is NOT_READY:
            raise NotReady
        return float(v)

    def price(self, symbol: str) -> float:
        """The live price, as `{"ind": "price"}` reads it."""
        px = self.ind.last_price.get(str(symbol).upper())
        if not px or px <= 0:
            raise NotReady
        return float(px)

    def session_open(self, symbol: str) -> float:
        px = self._session_open.get(str(symbol).upper())
        if not px or px <= 0:
            raise NotReady
        return float(px)

    def entry_price(self, symbol: str) -> float:
        """Sleeve.get_entry_price — the LAST BUY FILL under the
        entry_price_mode both engines run ("last_fill"), NOT average cost.
        Holding.average_price is avg_cost and would be a different number
        the moment a position is averaged into."""
        v = self._sleeve().get_entry_price(str(symbol).upper())
        if not v or v <= 0:
            raise NotReady
        return float(v)

    def days_held(self, symbol: str) -> float:
        """Calendar sessions since the entry fill — the IR engine's
        `cal._index[today] - cal._index[entry_day]`. Deliberately the
        CALENDAR difference and not a counter of sessions this run has
        seen: a deployment that adopted an existing position must report
        the same number the block strategy reports."""
        ed = self._sleeve().entry_day.get(str(symbol).upper())
        if ed is None:
            raise NotReady
        idx = getattr(getattr(self.algo, "_calendar", None), "_index", None)
        today = self.algo.time.date()
        if not idx or today not in idx or ed not in idx:
            raise NotReady
        return float(idx[today] - idx[ed])

    def pnl_pct(self, symbol: str, basis: str = "last",
                use_open: bool = False) -> float:
        """Return against the entry price SNAPSHOTTED at the start of this
        trigger batch (IR `_take_pnl_snapshot` / `_take_snap_multi`) — not
        the live entry price, so a rule that averages down still sizes its
        tier off the PRE-avg-down basis. `use_open` is compiled in for a
        rule whose trigger carries assess='session_open'."""
        sym = str(symbol).upper()
        e = (self.open_snap if use_open else self.snap_entry).get(sym)
        if e is None or e <= 0:
            raise NotReady
        ref = (self.ind.prior_close.get(sym) if basis == "prior_close"
               else self.ind.last_price.get(sym))
        if ref is None:
            raise NotReady
        return ref / e - 1.0

    def take_snapshot(self, freeze_open: bool = False) -> None:
        sleeve = self._sleeve()
        syms = set(self.symbols) | set(sleeve.qty)
        self.snap_entry = {s: sleeve.get_entry_price(s) for s in syms}
        if freeze_open:
            self.open_snap = dict(self.snap_entry)

    def held_symbols(self) -> list:
        """Symbols the sleeve is holding, in the sleeve's OWN order — the
        order `_fire_rule_inner`'s multi liquidate walks. A liquidate closes
        the whole sleeve there, and two exits submitted in a different
        sequence are two different fill orderings."""
        return [s for s, q in self._sleeve().qty.items() if q != 0]

    def _sleeve(self):
        return self.algo.portfolio._sleeve

    # ------------------------------------------------------ weight trees

    def eval_context(self) -> EvalContext:
        """The IR engine's expression environment, for the WeightEngine.

        Weight trees are evaluated by the SAME evaluator on both engines —
        they are a tree walk with shadow state, and reimplementing them is
        exactly the mistake this module exists to avoid. Rule expressions
        are compiled to python instead: they are on the hot path, and in an
        ejected file they have to READ like code."""
        algo = self.algo

        def indicator_fn(spec, symbol):
            return self.ind.value(spec, symbol, self.params())

        def position_fn(name, symbol, node):
            sleeve = self._sleeve()
            sym = str(symbol).upper()
            if name == "invested":
                return sleeve.qty.get(sym, 0) != 0
            if name == "qty":
                return float(sleeve.qty.get(sym, 0))
            if name == "entry_price":
                v = sleeve.get_entry_price(sym)
                return NOT_READY if v is None else v
            if name == "peak_close":
                v = sleeve.peak_close.get(sym)
                return NOT_READY if v is None else v
            for fn, args in (("days_held", (sym,)),
                             ("pnl_pct", (sym, node.get("basis", "last")))):
                if name == fn:
                    try:
                        return getattr(self, fn)(*args)
                    except NotReady:
                        return NOT_READY
            raise ValueError(f"unknown pos accessor {name}")

        def sleeve_fn(name):
            if name == "cash":
                return float(algo.portfolio.cash)
            if name == "equity":
                return float(algo.portfolio.total_portfolio_value)
            raise ValueError(f"unknown sleeve accessor {name}")

        return EvalContext(self.params(), self.default_symbol,
                           indicator_fn, position_fn, sleeve_fn)

    def target_weights(self, node: dict, day: date) -> dict:
        return self.weights.evaluate(node, self.eval_context(), day)
