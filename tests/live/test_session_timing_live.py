"""Session timing and real-time quotes.

Only the pieces that need no database and no live quote socket are here:
the breach predicate, the qb_cooldown market-delta skip in `reconcile()`,
and the session-aware ticker schedule.

The close-order INTENT preview that used to live here went with the IR
engine: the python runtime has no on-close order type, so it always emitted
`close_orders: []`, and every deployment is python-routed now. An at-close
order live-routes as a market order just before the close, not as a true
MOC.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dqengine.adapters.base import BrokerAdapter, Caps
from dqengine.live.executor import Rails, quote_breaches_stop, reconcile

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------- breach predicate

def test_quote_breaches_stop_matches_bar_driven_strict_condition():
    """Mirrors engine.py's `_check_targets`: `l < stop` (strict) triggers,
    touching exactly does not."""
    assert quote_breaches_stop("stop", 100.0, 99.99) is True
    assert quote_breaches_stop("stop", 100.0, 100.0) is False   # touch, no breach
    assert quote_breaches_stop("stop", 100.0, 100.01) is False
    assert quote_breaches_stop("trailing_stop", 50.0, 49.5) is True


def test_quote_breaches_stop_ignores_non_stop_kinds():
    # a plain limit (take-profit) is never a "breach" in this sense
    assert quote_breaches_stop("limit", 100.0, 50.0) is False
    assert quote_breaches_stop("stop_limit", 100.0, 50.0) is False


def test_quote_breaches_stop_handles_missing_level_or_price():
    assert quote_breaches_stop("stop", None, 50.0) is False
    assert quote_breaches_stop("stop", 100.0, None) is False


# ---------------------------------------------------- reconcile() qb_cooldown

class FakeBroker(BrokerAdapter):
    id = "fake"
    caps = Caps()

    def __init__(self, positions=None, orders=None):
        self.pos = dict(positions or {})
        self.orders = list(orders or [])
        self.log = []
        self._seq = 0

    def positions(self, creds):
        return dict(self.pos)

    def open_orders(self, creds):
        return list(self.orders)

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        self._seq += 1
        o = {"id": f"o{self._seq}", "symbol": symbol, "qty": float(qty),
             "side": side, "type": order_type, "limit_price": limit_price,
             "status": "new", "client_order_id": client_order_id or ""}
        self.log.append(("submit", o))
        self.orders.append(o)
        return o

    def cancel(self, creds, order_id):
        self.orders = [o for o in self.orders if o["id"] != order_id]


def _report():
    return {"synced_at": "t", "actions": [], "errors": []}


def test_qb_cooldown_skips_market_delta_for_that_symbol_only():
    """The exact race a quote-breach exit creates: the broker already shows
    the reduced position (have=60, post-exit), but `desired` (from the
    still-stale replay) hasn't caught up yet (want=100). Without the
    cooldown skip, the ordinary market-delta pass would BUY BACK the 40
    shares just sold. With `sym` in `qb_cooldown`, it must submit nothing
    for that symbol -- while an unrelated symbol's delta still executes
    normally."""
    fb = FakeBroker(positions={"SPY": 60.0, "QQQ": 10.0})
    recs = []
    reconcile(fb, {}, {"SPY": 100.0, "QQQ": 15.0}, [],
              {"SPY": 400.0, "QQQ": 300.0}, Rails(), _report(), recs.append,
              qb_cooldown={"SPY"})
    subs = [e for a, e in fb.log if a == "submit"]
    assert all(s["symbol"] != "SPY" for s in subs), \
        "qb_cooldown must suppress the SPY buy-back"
    assert any(s["symbol"] == "QQQ" for s in subs), \
        "an unrelated symbol's market delta must still execute"


def test_without_qb_cooldown_the_same_scenario_would_buy_back():
    """Control: confirms the race is real absent the fix (would fail before
    qb_cooldown existed) -- i.e. this test documents the bug the cooldown
    prevents, not a new regression."""
    fb = FakeBroker(positions={"SPY": 60.0})
    recs = []
    reconcile(fb, {}, {"SPY": 100.0}, [], {"SPY": 400.0}, Rails(), _report(),
              recs.append)
    subs = [e for a, e in fb.log if a == "submit"]
    assert subs and subs[0]["symbol"] == "SPY" and subs[0]["side"] == "buy"


# ------------------------------------------------------- ticker schedule

def test_next_tick_delay_s_slow_on_weekend():
    from dqengine.live.driver.deployment import next_tick_delay_s
    sat = datetime(2024, 6, 15, 12, 0, tzinfo=ET)     # a Saturday
    assert next_tick_delay_s(sat, interval_open_s=60,
                             interval_closed_s=1800) == 1800.0


def test_next_tick_delay_s_slow_outside_market_hours():
    from dqengine.live.driver.deployment import next_tick_delay_s
    early = datetime(2024, 6, 17, 6, 0, tzinfo=ET)    # well before the open
    late = datetime(2024, 6, 17, 18, 0, tzinfo=ET)    # well after the close
    assert next_tick_delay_s(early, 60, 1800) == 1800.0
    assert next_tick_delay_s(late, 60, 1800) == 1800.0


def test_next_tick_delay_s_pre_open_sleep_is_capped_at_the_open_anchor():
    """A tick at 09:20:30 used to sleep a full 1800s, so the day's first
    tick (and the day's first order) landed at 09:50:30. The overnight
    sleep must be capped so the wake-up lands AT the 09:31 open anchor,
    exactly like the close-minus-ten anchor inside the session."""
    from dqengine.live.driver.deployment import next_tick_delay_s
    now = datetime(2026, 8, 19, 9, 20, 30, tzinfo=ET)    # a Wednesday
    assert abs(next_tick_delay_s(now, 60, 1800) - 630.0) < 1e-6
    now = datetime(2026, 8, 19, 9, 29, 0, tzinfo=ET)
    assert abs(next_tick_delay_s(now, 60, 1800) - 120.0) < 1e-6
    # far from the open the slow cadence still applies untouched
    dawn = datetime(2026, 8, 19, 4, 0, 0, tzinfo=ET)
    assert next_tick_delay_s(dawn, 60, 1800) == 1800.0


def test_next_tick_delay_s_tight_during_market_hours():
    from dqengine.live.driver.deployment import next_tick_delay_s
    mid = datetime(2024, 6, 17, 12, 0, tzinfo=ET)     # normal day, midday
    assert next_tick_delay_s(mid, 60, 1800) <= 60.0


def test_next_tick_delay_s_lands_exactly_on_close_minus_ten():
    """The tick that makes MOC/LOC reachable: verify the schedule wakes up
    exactly AT close_time - 10 minutes rather than sailing past it by up to
    a full 60s cadence step."""
    from dqengine.live.driver.deployment import next_tick_delay_s
    now = datetime(2024, 6, 17, 15, 49, 30, tzinfo=ET)   # 10.5 min before close
    delay = next_tick_delay_s(now, 60, 1800)
    assert abs(delay - 30.0) < 1e-6


def test_next_tick_delay_s_early_close_day_uses_1300_close():
    """Session-aware: on a half day, close-10-minutes is 12:50 ET, not
    15:50 -- the schedule must consult the calendar, not assume 16:00."""
    from dqengine.live.driver.deployment import next_tick_delay_s
    now = datetime(2024, 11, 29, 12, 49, 30, tzinfo=ET)  # half day
    delay = next_tick_delay_s(now, 60, 1800)
    assert abs(delay - 30.0) < 1e-6
    # the same wall-clock time on a NORMAL day must NOT trigger that anchor
    normal = datetime(2024, 6, 17, 12, 49, 30, tzinfo=ET)
    assert next_tick_delay_s(normal, 60, 1800) == 60.0
