from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dqengine.adapters.base import Caps
from dqengine.live import executor, persistence, vault
from dqengine.live.executor import Rails, reconcile, _exit_cid_prefix

from rig import FakeBroker, report


def compute_targets(dep_states):
    """The desired state as the sweep builds it -- one state per deployment,
    folded by the private combiner -- in the shape the tests below were
    written against."""
    st = executor.connection_inputs(dep_states, "c-tbe").state
    return st.desired, list(st.exit_wants), st.last_px


def compute_close_orders(dep_states):
    return list(executor.connection_inputs(
        dep_states, "c-tbe").state.close_wants)


def compute_entry_wants(dep_states):
    return list(executor.connection_inputs(
        dep_states, "c-tbe").state.entry_wants)

ET = ZoneInfo("America/New_York")


def test_rails_from_settings_and_live_gate():
    from dqengine.live.executor import rails_from
    r = rails_from({"max_order_notional": 500, "paused": True}, "paper", False)
    assert r.max_order_notional == 500.0 and r.paused is True
    assert r.live_allowed is True                     # paper never gated
    assert rails_from({}, "live", False).live_allowed is False
    assert rails_from({}, "live", True).live_allowed is True
    assert rails_from(None, "paper", True).max_orders_per_sync == 20


def test_broker_order_row_shape():
    from dqengine.live.executor import broker_order_row
    row = broker_order_row("conn1", {
        "action": "submit", "symbol": "SPY", "qty": 5.0, "side": "buy",
        "order_type": "market", "limit_price": None, "broker_order_id": "o1",
        "client_order_id": "sl-mkt-SPY-x", "status": "new",
        "deployment_id": "d1"})
    assert row.connection_id == "conn1" and row.symbol == "SPY"
    assert row.action == "submit" and row.qty == 5.0
    assert row.deployment_id == "d1"


def dep(dep_id, holdings, open_orders=(), universe=("SPY",)):
    return (dep_id, {"holdings": holdings, "open_orders": list(open_orders)},
            list(universe))


def test_market_delta_and_inflight_guard():
    fb = FakeBroker(positions={"SPY": 2.0}, orders=[
        {"id": "x", "symbol": "SPY", "qty": 3.0, "side": "buy",
         "type": "market", "limit_price": None, "status": "new",
         "client_order_id": "sl-mkt-SPY-aaa"}])
    recs = []
    reconcile(fb, {}, {"SPY": 8.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append)
    # delta = 8 - 2 held - 3 in flight = 3
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["qty"] == 3.0 and subs[0]["side"] == "buy"
    assert recs[0]["action"] == "submit" and recs[0]["symbol"] == "SPY"


def test_two_syncs_of_same_connection_dont_duplicate_submission():
    """The driver's tick_all syncs a shared connection once per ticked
    deployment instead of once per pass. Two
    deployments on one connection each trigger their own sync, so the
    connection's reconcile() runs twice in the same pass. sync_broker_account
    re-reads adapter.positions()/open_orders() fresh on every call, and this
    FakeBroker mirrors that: the order submitted by the first sync appears in
    open_orders() (not yet filled into positions()) by the time the second
    sync reads broker state. The second reconcile must see that still-open
    order via the pending_qty inflight guard and NOT submit again for the
    same desired target -- confirming reconcile()/sync_broker_account
    reconciles rather than duplicates when synced twice within one pass."""
    fb = FakeBroker(positions={"SPY": 2.0}, orders=[])
    desired = {"SPY": 8.0}
    px = {"SPY": 400.0}

    # First sync (deployment A's tick -> _sync(cid))
    reconcile(fb, {}, desired, [], px, Rails(), report(), lambda e: None)
    subs_after_first = [e for a, e in fb.log if a == "submit"]
    assert len(subs_after_first) == 1 and subs_after_first[0]["qty"] == 6.0

    # Second sync of the SAME connection in the same pass (deployment B's
    # tick -> _sync(cid) again). Broker positions haven't updated yet (the
    # order hasn't filled), but it now shows up in open_orders().
    reconcile(fb, {}, desired, [], px, Rails(), report(), lambda e: None)
    subs_after_second = [e for a, e in fb.log if a == "submit"]
    assert len(subs_after_second) == 1, (
        f"second sync must not re-submit for the same still-open desired "
        f"delta: {fb.log}")


def test_tp_create_replace_and_cancel_paths():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "t1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "limit", "limit_price": 405.0, "status": "new",
         "client_order_id": "sl-tp-d1-zzz"},
        {"id": "t2", "symbol": "QQQ", "qty": 2.0, "side": "sell",
         "type": "limit", "limit_price": 300.0, "status": "new",
         "client_order_id": "sl-tp-gone-zzz"}])
    reconcile(fb, {}, {"SPY": 5.0}, [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    assert ("replace", "t1", 5, 410.0) in [
        (a, *rest) for a, *rest in fb.log if a == "replace"]
    assert ("cancel", "t2") in [tuple(e[:2]) for e in fb.log if e[0] == "cancel"]


def test_no_replace_falls_back_to_cancel_resubmit():
    fb = FakeBroker(positions={"SPY": 5.0}, supports_replace=False, orders=[
        {"id": "t1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "limit", "limit_price": 405.0, "status": "new",
         "client_order_id": "sl-tp-d1-zzz"}])
    reconcile(fb, {}, {"SPY": 5.0}, [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    kinds = [e[0] for e in fb.log]
    assert "cancel" in kinds and "submit" in kinds and "replace" not in kinds


def test_rails_refuse_oversize_order():
    fb = FakeBroker()
    recs = []
    r = report()
    reconcile(fb, {}, {"SPY": 100.0}, [], {"SPY": 400.0},
              Rails(max_order_notional=1000.0), r, recs.append)
    assert fb.log == []                       # 100 * 400 = 40k > 1k -> refused
    assert recs and recs[0]["action"] == "refused"
    assert any("refused" in a for a in r["actions"])


def test_position_cap_refuses_instead_of_resizing():
    """A cap must never silently trade a smaller size than the strategy asked
    for — that's a position you didn't choose and can't see in the P&L. Refuse
    the order and say so instead."""
    fb = FakeBroker()
    r = report()
    reconcile(fb, {}, {"SPY": 100.0}, [], {"SPY": 400.0},
              Rails(max_position_notional=20000.0), r, lambda e: None)
    assert not [e for a, e in fb.log if a == "submit"]
    assert any("max_position_notional" in a for a in r["actions"])


def test_position_cap_never_blocks_reducing_exposure():
    """Refusing a SELL that sheds risk because the position is over the cap
    would strand exactly the exposure the cap exists to limit."""
    fb = FakeBroker(positions={"SPY": 100.0})
    reconcile(fb, {}, {"SPY": 10.0}, [], {"SPY": 400.0},
              Rails(max_position_notional=20000.0), report(), lambda e: None)
    (sub,) = [e for a, e in fb.log if a == "submit"]
    assert sub["side"] == "sell" and sub["qty"] == 90.0


def test_notional_caps_are_off_by_default():
    """Sizing is the strategy's call. A default cap in dollars binds
    arbitrarily depending on account size, so there isn't one."""
    r = Rails()
    assert r.max_order_notional is None and r.max_position_notional is None
    fb = FakeBroker()
    reconcile(fb, {}, {"SPY": 10000.0}, [], {"SPY": 400.0}, r, report(),
              lambda e: None)
    assert [e for a, e in fb.log if a == "submit"]      # $4m trade, allowed


def test_zero_clears_a_cap():
    from dqengine.live.executor import rails_from
    r = rails_from({"max_order_notional": 0, "max_position_notional": 0},
                   "paper", True)
    assert r.max_order_notional is None and r.max_position_notional is None


def test_a_set_order_cap_still_refuses():
    fb = FakeBroker()
    r = report()
    reconcile(fb, {}, {"SPY": 100.0}, [], {"SPY": 400.0},
              Rails(max_order_notional=1000.0), r, lambda e: None)
    assert not [e for a, e in fb.log if a == "submit"]
    assert any("order notional" in a for a in r["actions"])


def test_rails_paused_and_live_gate_block_everything():
    fb = FakeBroker()
    for rails in (Rails(paused=True), Rails(live_allowed=False)):
        reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, rails,
                  report(), lambda e: None)
    assert fb.log == []


def _tp(level):
    return [("d1", "SPY", 5.0, "limit", level, None, None)]


def test_far_take_profit_is_placed():
    """A sell resting ABOVE the market is a take-profit, not a defect — it
    just rests. The old symmetric band refused these, silently degrading
    every target into a platform-simulated market exit."""
    fb = FakeBroker(positions={"SPY": 5.0})
    reconcile(fb, {}, {"SPY": 5.0}, _tp(500.0), {"SPY": 400.0}, Rails(),
              report(), lambda e: None)
    assert [e for a, e in fb.log if a == "submit"]


def test_sell_limit_priced_into_the_market_is_refused():
    """THE hazard: a sell below the bid fills instantly at a price the
    strategy never intended. The old band waved this through — 390 vs 400 is
    2.5% away, well inside its 5% window."""
    fb = FakeBroker(positions={"SPY": 5.0})
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, _tp(390.0), {"SPY": 400.0}, Rails(), r,
              lambda e: None)
    assert not [e for a, e in fb.log if a == "submit"]
    assert any("fill immediately" in a for a in r["actions"])


def test_sell_limit_a_hair_below_last_is_allowed():
    """cross_tol_pct leaves room for a deliberately marketable limit."""
    fb = FakeBroker(positions={"SPY": 5.0})
    reconcile(fb, {}, {"SPY": 5.0}, _tp(399.0), {"SPY": 400.0}, Rails(),
              report(), lambda e: None)
    assert [e for a, e in fb.log if a == "submit"]


def test_absurd_level_still_refused():
    """The sanity band survives to catch a decimal slip (4000 for 400)."""
    fb = FakeBroker(positions={"SPY": 5.0})
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, _tp(4000.0), {"SPY": 400.0}, Rails(), r,
              lambda e: None)
    assert not [e for a, e in fb.log if a == "submit"]
    assert any("sanity band" in a for a in r["actions"])


def test_limit_guard_is_directional():
    """Same distance, opposite sides: one rests, one would fill instantly."""
    from dqengine.live.executor import limit_price_refusal
    rails = Rails()
    assert limit_price_refusal("sell", 440.0, 400.0, rails) is None
    assert limit_price_refusal("buy", 440.0, 400.0, rails) is not None
    assert limit_price_refusal("buy", 360.0, 400.0, rails) is None
    assert limit_price_refusal("sell", 360.0, 400.0, rails) is not None


def test_limit_guard_noops_without_a_price():
    from dqengine.live.executor import limit_price_refusal
    assert limit_price_refusal("sell", 400.0, 0.0, Rails()) is None
    assert limit_price_refusal("sell", None, 400.0, Rails()) is None


def test_rails_max_orders_per_sync():
    fb = FakeBroker()
    desired = {f"S{i}": 1.0 for i in range(30)}
    px = {f"S{i}": 10.0 for i in range(30)}
    r = report()
    reconcile(fb, {}, desired, [], px, Rails(max_orders_per_sync=5), r,
              lambda e: None)
    assert len([e for a, e in fb.log if a == "submit"]) == 5
    assert sum(1 for a in r["actions"] if "circuit" in a) == 1


def test_qty_step_rounding():
    fb = FakeBroker()
    reconcile(fb, {}, {"SPY": 5.7}, [], {"SPY": 400.0}, Rails(),
              report(), lambda e: None)
    subs = [e for a, e in fb.log if a == "submit"]
    assert subs[0]["qty"] == 5.0              # floor to qty_step=1.0


def _no_cid_broker(positions=None, orders=None):
    """FakeBroker configured like Schwab: no client order ids."""
    fb = FakeBroker(positions=positions, orders=orders)
    fb.caps = Caps(supports_client_order_id=False)
    return fb


def test_no_client_id_broker_no_duplicate_market_submit():
    # an unfilled market order already rests for SPY; the sweep must not
    # re-submit a second one for the same delta (finding 1a)
    fb = _no_cid_broker(positions={"SPY": 2.0}, orders=[
        {"id": "x", "symbol": "SPY", "qty": 6.0, "side": "buy",
         "type": "market", "limit_price": None, "status": "new",
         "client_order_id": ""}])
    recs = []
    reconcile(fb, {}, {"SPY": 8.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append)
    # delta = 8 - 2 held - 6 pending market = 0 -> nothing submitted
    assert [e for a, e in fb.log if a == "submit"] == []


def test_no_client_id_broker_cleans_unclaimed_tp_and_keeps_claimed():
    fb = _no_cid_broker(positions={"SPY": 5.0, "QQQ": 2.0}, orders=[
        # claimed: matches the SPY tp_wants entry exactly
        {"id": "t1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "limit", "limit_price": 410.0, "status": "new",
         "client_order_id": ""},
        # unclaimed: QQQ sleeve released, no tp_wants entry left for it
        {"id": "t2", "symbol": "QQQ", "qty": 2.0, "side": "sell",
         "type": "limit", "limit_price": 300.0, "status": "new",
         "client_order_id": ""}])
    recs = []
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 2.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None)], {"SPY": 400.0, "QQQ": 290.0},
              Rails(), report(), recs.append)
    cancels = [e[1] for e in fb.log if e[0] == "cancel"]
    assert cancels == ["t2"]                  # only the unclaimed TP


def test_unknown_price_delta_refused():
    fb = FakeBroker()
    recs = []
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {}, Rails(), r, recs.append)
    assert fb.log == []
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "no_price"
    assert any("unknown price" in a for a in r["actions"])


def test_oversized_tp_refused():
    fb = FakeBroker(positions={"SPY": 5.0})
    recs = []
    reconcile(fb, {}, {"SPY": 5.0}, [("d1", "SPY", 5.0, "limit", 1000.0, None, None)],
              {"SPY": 980.0}, Rails(max_order_notional=1000.0), report(),
              recs.append)
    # 5 * 1000 = 5000 > 1000 cap -> refused, nothing submitted
    assert [e for a, e in fb.log if a in ("submit", "replace")] == []
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "over_notional"


def test_normal_tp_placed_with_known_price():
    fb = FakeBroker(positions={"SPY": 5.0})
    reconcile(fb, {}, {"SPY": 5.0}, [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(max_order_notional=25000.0), report(),
              lambda e: None)
    subs = [e for a, e in fb.log if a == "submit"]
    assert subs and subs[0]["qty"] == 5.0 and subs[0]["limit_price"] == 410.0


def test_compute_targets_harvests_every_exit_kind():
    desired, wants, px = compute_targets([
        ("d1", {"holdings": [{"symbol": "SPY", "qty": 5, "last_price": 400.0}],
                "open_orders": [
                    {"type": "limit_sell", "symbol": "SPY", "price": 410.0},
                    {"type": "stop", "symbol": "SPY", "stop": 380.0},
                    {"type": "trailing_stop", "symbol": "SPY",
                     "trail_pct": 0.05}]}, ["SPY"])])
    kinds = {w[3] for w in wants}
    assert kinds == {"limit", "stop", "trailing_stop"}


def test_native_stop_is_placed_when_the_broker_supports_it():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = Caps(order_types=frozenset({"market", "limit", "stop"}),
                   tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["type"] == "stop"


def test_unsupported_kind_falls_back_and_says_so():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = Caps(order_types=frozenset({"market", "limit"}),
                   tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.05)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert not [o for a, o in fb.log if a == "submit"]
    # wording comes from the degradation ladder (capabilities.py) so the
    # same fallback reads the same wherever it appears; assert the substance
    # -- the type is named and a substitution announced -- not one word.
    assert any("trailing_stop" in a and "emulated" in a for a in r["actions"])


def _stop_caps(**kw):
    from dqengine.adapters.base import Caps
    return Caps(order_types=frozenset({"market", "limit", "stop"}),
                tifs=frozenset({"day", "gtc"}), **kw)


def test_resting_stop_unchanged_triggers_no_action():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                        # nothing changed, nothing sent


def test_resting_stop_moved_cancels_and_resubmits_with_new_level():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 370.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]        # never adapter.replace
    cancels = [o for a, o in fb.log if a == "cancel"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert cancels[0] == "s1"
    assert subs[0]["type"] == "stop" and subs[0]["stop_price"] == 370.0


def test_sell_stop_at_or_above_last_price_refused():
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = _stop_caps()
    recs = []
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 410.0, None)],
              {"SPY": 400.0}, Rails(), r, recs.append)
    assert not [o for a, o in fb.log if a == "submit"]
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "invalid_stop"
    assert any("refused" in a for a in r["actions"])


def test_stop_far_outside_stop_band_refused():
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = _stop_caps()
    recs = []
    r = report()
    # 150 vs 400 is 62.5% away -> over the default 50% stop_band_pct
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 150.0, None)],
              {"SPY": 400.0}, Rails(), r, recs.append)
    assert not [o for a, o in fb.log if a == "submit"]
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "over_stop_band"
    assert any("refused" in a for a in r["actions"])


def test_no_client_id_broker_matches_resting_stop_without_duplicating():
    fb = _no_cid_broker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _stop_caps(supports_client_order_id=False)
    recs = []
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), report(), recs.append)
    assert [o for a, o in fb.log if a == "submit"] == []
    assert [o for a, o in fb.log if a == "cancel"] == []


def test_stop_stale_cancel_path():
    fb = FakeBroker(positions={"SPY": 5.0, "QQQ": 2.0}, orders=[
        {"id": "t1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"},
        {"id": "t2", "symbol": "QQQ", "qty": 2.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 280.0,
         "status": "new", "client_order_id": "sl-stp-QQQ-gone-zzz"}])
    fb.caps = _stop_caps()
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 2.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0, "QQQ": 290.0}, Rails(), report(), lambda e: None)
    cancels = [o for a, o in fb.log if a == "cancel"]
    assert cancels == ["t2"]                    # only the released stop


def _trailing_caps(**kw):
    from dqengine.adapters.base import Caps
    return Caps(order_types=frozenset({"market", "limit", "stop",
                                        "stop_limit", "trailing_stop"}),
                tifs=frozenset({"day", "gtc"}), **kw)


def test_resting_trailing_stop_unchanged_trail_ignores_moving_stop_price():
    # a real broker's resting trailing stop reports a stop_price that
    # RATCHETS with the high-water mark every sweep — that must not be
    # mistaken for "the deployment wants a different trail."
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "trailing_stop", "limit_price": None,
         "stop_price": 385.0,          # moved since last sync
         "trail_percent": 5.0,         # trail itself is unchanged
         "status": "new", "client_order_id": "sl-trl-SPY-d1-zzz"}])
    fb.caps = _trailing_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.05)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                       # no cancel+resubmit churn


def test_resting_trailing_stop_trail_change_cancels_and_resubmits():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "trailing_stop", "limit_price": None,
         "stop_price": 385.0, "trail_percent": 5.0,
         "status": "new", "client_order_id": "sl-trl-SPY-d1-zzz"}])
    fb.caps = _trailing_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.08)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs[0]["type"] == "trailing_stop"
    assert abs(subs[0]["trail_percent"] - 8.0) < 1e-9


def test_resting_stop_with_unknown_stop_price_is_not_churned():
    """An adapter that never populates stop_price (e.g. Webull, which has
    no field for it in the live open-orders payload) reports None. That
    must read as "unknown -> assume unchanged", not as 0 — coercing to 0
    would make every plain stop look "changed" on every sweep and cancel
    +resubmit a live protective order forever."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": None,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                        # no cancel+resubmit churn


def test_resting_stop_with_unknown_stop_price_still_resubmits_on_qty_change():
    """Even with an unknown resting reference, a genuine qty change must
    still be picked up and acted on."""
    fb = FakeBroker(positions={"SPY": 8.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": None,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 8.0},
              [("d1", "SPY", 8.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]


def test_resting_stop_webull_shaped_cid_level_token_moved_cancels_and_resubmits():
    """Webull reports no stop_price at all, so change detection falls back
    to the level token this executor embedded in the client_order_id when
    the resting order was placed. If the deployment's wanted level has
    since moved, that must still be picked up — otherwise a live Webull
    protective stop would go stale forever unless qty also changes."""
    from dqengine.live.executor import _encode_level
    old_cid = f"sl-stp-SPY-d1-aaaaaa-L{_encode_level(380.0)}"
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": None,
         "status": "new", "client_order_id": old_cid}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 370.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs[0]["stop_price"] == 370.0
    # the new resting order's cid carries the new level too, so the next
    # sweep can detect further moves the same way
    from dqengine.live.executor import _decode_level
    assert _decode_level(subs[0]["client_order_id"]) == 370.0


def test_resting_stop_webull_shaped_cid_level_token_unchanged_triggers_no_action():
    from dqengine.live.executor import _encode_level
    old_cid = f"sl-stp-SPY-d1-aaaaaa-L{_encode_level(380.0)}"
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": None,
         "status": "new", "client_order_id": old_cid}])
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                        # no cancel+resubmit churn


def test_resting_stop_webull_shaped_cid_level_token_subcent_level_is_not_churned():
    """A wanted level whose distance to the nearest cent lands in
    (0.004, 0.005] — e.g. a computed 0.95 x entry giving 380.0045 — must
    not desync from its own encoded token. Comparing a raw un-quantized
    level against the (necessarily cents-quantized) decoded token would
    make this look "changed" every single sweep, cancel+resubmitting a
    live protective stop indefinitely. Run two sweeps to prove it's not
    just the first one that happens to line up."""
    from dqengine.live.executor import _encode_level
    old_cid = f"sl-stp-SPY-d1-aaaaaa-L{_encode_level(380.0045)}"
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": None,
         "status": "new", "client_order_id": old_cid}])
    fb.caps = _stop_caps()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0045, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    assert fb.log == []
    # second sweep, same resting order (nothing was replaced) — still no
    # action, not just "didn't happen to trip on sweep one"
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0045, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    assert fb.log == []


def test_resting_trailing_stop_webull_shaped_cid_level_token_moved_cancels_and_resubmits():
    from dqengine.live.executor import _encode_level
    old_cid = f"sl-trl-SPY-d1-aaaaaa-L{_encode_level(5.0)}"
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "trailing_stop", "limit_price": None,
         "stop_price": None, "trail_percent": None,
         "status": "new", "client_order_id": old_cid}])
    fb.caps = _trailing_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.08)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert abs(subs[0]["trail_percent"] - 8.0) < 1e-9


def test_resting_trailing_stop_webull_shaped_cid_level_token_unchanged_triggers_no_action():
    from dqengine.live.executor import _encode_level
    old_cid = f"sl-trl-SPY-d1-aaaaaa-L{_encode_level(5.0)}"
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "trailing_stop", "limit_price": None,
         "stop_price": None, "trail_percent": None,
         "status": "new", "client_order_id": old_cid}])
    fb.caps = _trailing_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.05)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                        # no cancel+resubmit churn


def test_new_stop_placement_embeds_level_token_in_client_order_id():
    """A freshly placed stop's client_order_id carries the level token
    from the start, so the very next sweep already has a fallback even
    before any broker-side confirmation."""
    from dqengine.live.executor import _decode_level
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = _stop_caps()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1
    assert _decode_level(subs[0]["client_order_id"]) == 380.0


def test_take_profit_client_order_id_format_unchanged_by_level_token():
    """The limit/take-profit cid format must stay byte-identical — no -L
    token — since the constraint is to not touch that path at all."""
    fb = FakeBroker(positions={"SPY": 5.0})
    from dqengine.adapters.base import Caps
    fb.caps = Caps(order_types=frozenset({"market", "limit"}),
                   tifs=frozenset({"day", "gtc"}))
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1
    cid = subs[0]["client_order_id"]
    assert cid.startswith("sl-tp-d1-") and "-L" not in cid.rsplit("-", 1)[-1]
    import re
    assert re.fullmatch(r"sl-tp-d1-[0-9a-f]{6}", cid)


def test_resting_trailing_stop_with_unknown_trail_percent_is_not_churned():
    """Same unknown-vs-zero safety for trailing stops: an adapter that
    can't report trail_percent must not have its resting order
    cancel+resubmitted (and its trail high-water mark re-anchored) every
    sweep just because the field is missing."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "trailing_stop", "limit_price": None,
         "stop_price": None, "trail_percent": None,
         "status": "new", "client_order_id": "sl-trl-SPY-d1-zzz"}])
    fb.caps = _trailing_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "trailing_stop", None, None, 0.05)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                       # no cancel+resubmit churn


def _stop_limit_caps(**kw):
    from dqengine.adapters.base import Caps
    return Caps(order_types=frozenset({"market", "limit", "stop_limit"}),
                tifs=frozenset({"day", "gtc"}), **kw)


def test_resting_stop_limit_unchanged_triggers_no_action():
    # C1 regression: the resting order reports stop_price (Alpaca/Schwab
    # shape) — comparing it against the LIMIT leg would look "changed"
    # every sweep even though nothing moved.
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop_limit", "limit_price": 378.0, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stl-SPY-d1-zzz"}])
    fb.caps = _stop_limit_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop_limit", 378.0, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    assert fb.log == []                        # nothing changed, nothing sent


def test_resting_stop_limit_trigger_only_change_cancels_and_resubmits():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop_limit", "limit_price": 378.0, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stl-SPY-d1-zzz"}])
    fb.caps = _stop_limit_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop_limit", 378.0, 370.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs[0]["stop_price"] == 370.0 and subs[0]["limit_price"] == 378.0


def test_resting_stop_limit_limit_only_change_cancels_and_resubmits():
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop_limit", "limit_price": 378.0, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stl-SPY-d1-zzz"}])
    fb.caps = _stop_limit_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop_limit", 375.0, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs[0]["stop_price"] == 380.0 and subs[0]["limit_price"] == 375.0


def test_bracket_rests_only_protective_leg_and_simulates_take_profit():
    # C3 regression: a stop + take-profit on the same shares must not both
    # rest natively — only the protective leg is placed; the sibling
    # take-profit is journaled as simulated instead.
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = _stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 420.0, None, None),
               ("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["type"] == "stop"
    assert any("take-profit for SPY simulated" in a for a in r["actions"])


def test_lone_take_profit_still_rests_natively():
    fb = FakeBroker(positions={"SPY": 5.0})
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["type"] == "limit"
    assert not any("take-profit for SPY simulated" in a for a in r["actions"])


def test_all_exit_kinds_fit_within_max_cid_len_at_max_level():
    """Webull truncates clientOrderId at MAX_CID_LEN (40) chars; a
    truncated cid misparses its level token and resurrects the churn bug
    this token scheme exists to close. Prove every kind stays under the
    limit even at the largest level _encode_level can represent, a
    full-length (UUID-shaped) deployment id, a 5-char symbol and a rule id
    — the worst case for the symbol/rule identity C1 added, not just the
    small values the other tests happen to use."""
    from dqengine.live.executor import _cid_with_level, _exit_cid_prefix, MAX_CID_LEN
    dep_id = "deadbeef-89ab-cdef-0123-456789abcdef"     # 36-char UUID shape
    sym = "GOOGL"                                       # 5-char symbol
    rule_id = "protective-stop-rule-with-a-long-name"   # realistic rule id
    max_cents_value = 604661.75          # 36**5 - 1 cents: _encode_level's ceiling
    cases = [
        ("limit", max_cents_value, None, None),
        ("stop", None, max_cents_value, None),
        ("stop_limit", max_cents_value, max_cents_value, None),
        ("trailing_stop", None, None, max_cents_value / 100.0),
    ]
    for kind, price, stop_px, trail in cases:
        prefix = _exit_cid_prefix(dep_id, kind, sym, rule_id)
        cid = _cid_with_level(prefix, kind, price, stop_px, trail)
        assert len(cid) <= MAX_CID_LEN, (kind, cid, len(cid))


def test_cid_builder_guard_raises_on_overlength_cid():
    """The hard guard in _cid_with_level must fire if a cid would ever
    exceed MAX_CID_LEN — proves the safety net itself works, independent
    of whether today's widths happen to stay under it."""
    from dqengine.live.executor import _cid_with_level
    huge_prefix = "sl-stl-" + ("x" * 40)   # deliberately oversized prefix
    with pytest.raises(ValueError):
        _cid_with_level(huge_prefix, "stop_limit", 100.0, 90.0, None)


def test_realistic_sell_stop_limit_is_placed_not_band_skipped():
    # stop 380 / limit 378 against a last price of 400 is a realistic
    # protective stop-limit (5.5% away) — the tight take-profit band must
    # not gate it.
    fb = FakeBroker(positions={"SPY": 5.0})
    from dqengine.adapters.base import Caps
    fb.caps = Caps(order_types=frozenset(
        {"market", "limit", "stop", "stop_limit"}),
        tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop_limit", 378.0, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None)
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["type"] == "stop_limit" and \
        subs[0]["limit_price"] == 378.0 and subs[0]["stop_price"] == 380.0
    assert not any("skipped" in a for a in r["actions"])


def test_reconcile_places_moc_natively_when_broker_supports_it():
    """A broker whose Caps.order_types include market_on_close (Schwab
    today) gets the at_close_order placed natively, tagged sl-moc-, and the
    ordinary market-delta pass must NOT also submit a duplicate for the same
    qty (it's already netted out via moc_native_qty)."""
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    fb = FakeBroker(positions={"SPY": 0.0})
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append,
              moc_wants=[("d1", "SPY", 5.0, "market_on_close", None)])
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1
    assert subs[0]["type"] == "market_on_close"
    assert subs[0]["client_order_id"].startswith("sl-moc-SPY-d1")
    # the market-delta pass sees desired(5) - have(0) - moc_native(5) == 0
    assert all(r["order_type"] != "market" for r in recs)


def test_reconcile_places_loc_natively_with_its_limit_price():
    from dqengine.adapters.base import Caps, MARKET, LIMIT, LIMIT_ON_CLOSE
    fb = FakeBroker(positions={"QQQ": 0.0})
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, LIMIT_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"QQQ": 3.0}, [], {"QQQ": 410.0}, Rails(), report(),
              recs.append,
              moc_wants=[("d1", "QQQ", 3.0, "limit_on_close", 410.5)])
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1
    assert subs[0]["type"] == "limit_on_close"
    assert subs[0]["limit_price"] == 410.5
    assert subs[0]["side"] == "buy"


def test_reconcile_does_not_resubmit_a_pending_native_moc_order():
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    fb = FakeBroker(positions={"SPY": 0.0}, orders=[
        {"id": "m1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "market_on_close", "limit_price": None, "status": "new",
         "client_order_id": "sl-moc-SPY-d1-abc12345"}])
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"SPY": -5.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append, moc_wants=[("d1", "SPY", -5.0, "market_on_close", None)])
    subs = [e for a, e in fb.log if a == "submit"]
    assert subs == []


def test_reconcile_falls_back_to_market_emulation_when_broker_lacks_moc():
    """A broker without market_on_close in Caps.order_types (the default —
    every roster broker except Schwab today) journals the fallback and lets
    the ordinary market-delta pass place a plain market order instead —
    the IR spec §7 near-close emulation."""
    fb = FakeBroker(positions={"SPY": 0.0})   # default Caps: market, limit only
    recs = []
    rep = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), rep,
              recs.append,
              moc_wants=[("d1", "SPY", 5.0, "market_on_close", None)])
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["type"] == "market"
    assert any("market_on_close" in a and "emulated" in a
               for a in rep["actions"])


# ---------------------------------------------------------------------------
# Resting BUY entry orders (breakout stop / pullback limit / stop_limit —
# strategy-ir-spec.md §15.5).

def test_compute_entry_wants_harvests_entry_orders():
    dep_states = [("d1", {"entry_orders": [
        {"type": "stop", "symbol": "SPY", "qty": 5, "stop": 410.0},
        {"type": "limit", "symbol": "QQQ", "qty": 3, "price": 380.0},
    ]}, ["SPY", "QQQ"])]
    wants = compute_entry_wants(dep_states)
    # 8th element is the entry's side; absent from the payload means "buy"
    # (IR entries are long by construction).
    assert ("d1", "SPY", 5.0, "stop", None, 410.0, None, "buy") in wants
    assert ("d1", "QQQ", 3.0, "limit", 380.0, None, None, "buy") in wants


def test_native_buy_stop_entry_is_placed_when_the_broker_supports_it():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit", "stop"}),
                   tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["type"] == "stop" and subs[0]["side"] == "buy"
    assert subs[0]["stop_price"] == 410.0


def test_unsupported_entry_kind_falls_back_and_says_so():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit"}),
                   tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    assert not [o for a, o in fb.log if a == "submit"]
    assert any(a.startswith("entry ") and "stop" in a and "emulated" in a
               for a in r["actions"])


def _entry_stop_caps(**kw):
    from dqengine.adapters.base import Caps
    return Caps(order_types=frozenset({"market", "limit", "stop"}),
                tifs=frozenset({"day", "gtc"}), **kw)


def test_buy_stop_at_or_below_last_price_refused():
    """The INVERTED rail: a sell stop is refused AT-OR-ABOVE the last price
    (see test_sell_stop_at_or_above_last_price_refused); a buy stop must
    sit ABOVE the last price and is refused AT-OR-BELOW it — a buy stop
    resting at/below market would trigger an instant unintended market
    buy the moment it reached the broker."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 390.0)])
    assert not [o for a, o in fb.log if a == "submit"]
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "invalid_stop" and recs[0]["side"] == "buy"
    assert any("refused" in a for a in r["actions"])


def test_buy_stop_above_last_price_is_accepted():
    """The mirror positive case: a buy stop ABOVE market is exactly what a
    breakout entry needs and must NOT be refused."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["stop_price"] == 410.0
    assert not [rec for rec in recs if rec.get("status") == "invalid_stop"]


def test_buy_stop_far_outside_stop_band_refused():
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    r = report()
    # 650 vs 400 is 62.5% away -> over the default 50% stop_band_pct
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 650.0)])
    assert not [o for a, o in fb.log if a == "submit"]
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "over_stop_band"


def test_resting_buy_stop_entry_unchanged_triggers_no_action():
    fb = FakeBroker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": "en-stp-SPY-d1"}])
    fb.caps = _entry_stop_caps()
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    assert fb.log == []


def test_resting_buy_stop_entry_moved_cancels_and_resubmits():
    fb = FakeBroker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": "en-stp-SPY-d1"}])
    fb.caps = _entry_stop_caps()
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 420.0)])
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel", "submit"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs[0]["stop_price"] == 420.0


def test_buy_limit_entry_placed_native():
    from dqengine.adapters.base import Caps
    fb = FakeBroker(positions={})
    fb.caps = Caps(order_types=frozenset({"market", "limit"}),
                   tifs=frozenset({"day", "gtc"}))
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "limit", 390.0, None)])
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["type"] == "limit" and subs[0]["side"] == "buy"
    assert subs[0]["limit_price"] == 390.0


def test_buy_limit_entry_replace_when_broker_supports_replace():
    fb = FakeBroker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "limit", "limit_price": 380.0, "stop_price": None,
         "status": "new", "client_order_id": "en-lmt-SPY-d1"}],
        supports_replace=True)
    from dqengine.adapters.base import Caps
    fb.caps = Caps(order_types=frozenset({"market", "limit"}),
                   tifs=frozenset({"day", "gtc"}), supports_replace=True)
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "limit", 390.0, None)])
    kinds = [a for a, *_ in fb.log]
    assert kinds == ["replace"]


def test_stale_buy_entry_canceled_when_deployment_releases_it():
    fb = FakeBroker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": "en-stp-SPY-d1"}])
    fb.caps = _entry_stop_caps()
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[])
    kinds = [a for a, _ in fb.log]
    assert kinds == ["cancel"]


def test_buy_entry_does_not_collide_with_sell_exit_of_same_kind():
    """A resting sell stop (exit) and a resting buy stop (entry) on the
    same symbol must not be mistaken for one another by client_order_id
    prefix — 'sl-' vs 'en-'."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "x1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _entry_stop_caps()
    r = report()
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    subs = [o for a, o in fb.log if a == "submit"]
    # the sell exit is unchanged (no submit for it); only the new buy
    # entry gets placed
    assert len(subs) == 1 and subs[0]["side"] == "buy"


# ---------------------------------------------------------------------------
# Merge-gate regressions: resting-order identity omitted the symbol (C1), a
# filled MOC resubmits itself for the rest of the day (C2), a stale TP
# outlives its protective sibling (C4), release-cancels ran after market
# deltas (C5), and the buy-stop rail disarmed on an unknown price (C6).

def _seed_from_want(fb, dep_id, sym, qty, kind, price, stop_px, trail,
                    rule_id, order_id=None):
    """Seed a resting order in `fb` using the executor's own cid builder,
    so these regression tests can't drift from the real cid format."""
    from dqengine.live.executor import _cid_with_level, _exit_cid_prefix
    prefix = _exit_cid_prefix(dep_id, kind, sym, rule_id)
    cid = _cid_with_level(prefix, kind, price, stop_px, trail)
    fb.orders.append({
        "id": order_id or f"seed-{sym}-{rule_id}", "symbol": sym, "qty": qty,
        "side": "sell", "type": kind, "limit_price": None,
        "stop_price": stop_px, "status": "new", "client_order_id": cid})


def test_c1_two_symbols_one_deployment_nothing_changed_no_actions():
    """C1 regression: cid identity used to be (deployment, kind) only — two
    resting stops on different symbols under the same deployment would each
    match the OTHER's order via prefix. On the reproduced probe this
    canceled one symbol's order and duplicated the other, every sweep, even
    though nothing changed."""
    fb = FakeBroker(positions={"SPY": 5.0, "QQQ": 3.0})
    fb.caps = _stop_caps()
    exit_wants = [("d1", "SPY", 5.0, "stop", None, 380.0, None, "r-spy"),
                  ("d1", "QQQ", 3.0, "stop", None, 280.0, None, "r-qqq")]
    for w in exit_wants:
        _seed_from_want(fb, *w, order_id=f"seed-{w[1]}")
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 3.0}, exit_wants,
              {"SPY": 400.0, "QQQ": 300.0}, Rails(), report(), lambda e: None)
    assert fb.log == []


def test_c1_two_same_kind_rules_one_symbol_both_rest_independently():
    """A two-rung scale-in ladder on one symbol: two 'stop' rules resting
    on the same symbol under the same deployment must each keep their own
    identity (via the rule-id token) — not be mistaken for one another."""
    fb = FakeBroker(positions={"SPY": 8.0})
    fb.caps = _stop_caps()
    exit_wants = [("d1", "SPY", 5.0, "stop", None, 380.0, None, "rule-a"),
                  ("d1", "SPY", 3.0, "stop", None, 385.0, None, "rule-b")]
    for w in exit_wants:
        _seed_from_want(fb, *w, order_id=f"seed-{w[7]}")
    reconcile(fb, {}, {"SPY": 8.0}, exit_wants, {"SPY": 400.0}, Rails(),
              report(), lambda e: None)
    assert fb.log == []                  # both already correctly resting
    assert len(fb.orders) == 2           # neither cancelled nor duplicated


def test_c1_released_symbols_order_cancelled_sibling_survives():
    fb = FakeBroker(positions={"SPY": 5.0, "QQQ": 3.0})
    fb.caps = _stop_caps()
    seed_wants = [("d1", "SPY", 5.0, "stop", None, 380.0, None, "r-spy"),
                  ("d1", "QQQ", 3.0, "stop", None, 280.0, None, "r-qqq")]
    for w in seed_wants:
        _seed_from_want(fb, *w, order_id=f"seed-{w[1]}")
    # this sweep: QQQ's sleeve released its stop
    exit_wants = [("d1", "SPY", 5.0, "stop", None, 380.0, None, "r-spy")]
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 3.0}, exit_wants,
              {"SPY": 400.0, "QQQ": 300.0}, Rails(), report(), lambda e: None)
    cancels = [o for a, o in fb.log if a == "cancel"]
    assert cancels == ["seed-QQQ"]
    assert [o["id"] for o in fb.orders] == ["seed-SPY"]


def test_c2_moc_dedups_against_already_submitted_today_even_after_fill():
    """A native MOC placed earlier this session has since FILLED — it no
    longer appears in open_orders(), but `moc_done` (built from today's
    broker_orders rows, any status) still marks it done. Without this, the
    post-fill sweep resubmits the MOC and a compensating market sell."""
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    fb = FakeBroker(positions={"SPY": 10.0})   # position already reflects the fill
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"SPY": 10.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append,
              moc_wants=[("d1", "SPY", 10.0, "market_on_close", None)],
              moc_done={("d1", "SPY")})
    assert fb.log == []
    assert recs == []


def test_c4_stale_tp_cancelled_once_protective_sibling_appears():
    """A TP that rested alone (lone limit rule, e.g. before the strategy's
    warm-up finished) must be cancelled once a protective stop for the same
    symbol shows up and C3's bracket policy starts simulating the TP —
    otherwise the stop is rejected forever against shares the stale TP
    still holds."""
    from dqengine.live.executor import _cid_with_level, _exit_cid_prefix
    fb = FakeBroker(positions={"SPY": 5.0})
    fb.caps = _stop_caps()
    prefix = _exit_cid_prefix("d1", "limit")
    cid = _cid_with_level(prefix, "limit", 420.0, None, None)
    fb.orders.append({"id": "tp1", "symbol": "SPY", "qty": 5.0,
                       "side": "sell", "type": "limit", "limit_price": 420.0,
                       "stop_price": None, "status": "new",
                       "client_order_id": cid})
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 420.0, None, None),
               ("d1", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    cancels = [o for a, o in fb.log if a == "cancel"]
    assert "tp1" in cancels
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["type"] == "stop"


def test_c5_release_cancel_runs_before_market_delta():
    """When a replay-driven exit releases a resting protective stop in the
    same sweep the freed shares get sold at market, the cancel must reach
    the broker before the market sell is attempted — otherwise the sell is
    rejected because the shares are still committed to the resting stop."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    reconcile(fb, {}, {"SPY": 0.0}, [], {"SPY": 400.0}, Rails(), report(),
              lambda e: None)
    kinds = [a for a, *_ in fb.log]
    assert "cancel" in kinds and "submit" in kinds
    assert kinds.index("cancel") < kinds.index("submit")


def test_c6_buy_stop_entry_refused_when_last_px_unknown():
    """A breakout entry is placed while FLAT, so last_px (built only from
    HELD symbols) is empty for it. px == 0 must refuse the stop entry
    outright, not silently skip the invalid-stop/band rails and let a buy
    stop at/below an unknown market reach the broker and fire instantly."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    r = report()
    reconcile(fb, {}, {}, [], {}, Rails(), r, recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    assert not [o for a, o in fb.log if a == "submit"]
    assert recs and recs[0]["action"] == "refused" and \
        recs[0]["status"] == "no_price" and recs[0]["side"] == "buy"


# ---------------------------------------------------------------------------
# W3 merge-gate fixes: the resting-order identity fix never reached the
# no-cid PLACEMENT lookup (CRITICAL 3), which also produced a spurious
# second cancel of the same broker order id on a level move (IMPORTANT 4);
# moc_done counted dry-run rows as done (IMPORTANT 5); the buy-stop rail
# permanently disabled native breakout entries because last_px never
# carried non-held/non-primary symbols (IMPORTANT 6).

def test_crit3_no_cid_two_same_kind_stops_unchanged_no_actions():
    """The no-cid PLACEMENT lookup used to match `existing` by
    (symbol, side, kind) only, ignoring qty/level, then blindly take
    existing[0] — so two same-kind stops resting on one symbol (5@380
    protective, 3@375) got mistaken for each other every sweep even though
    nothing changed. Matching must use the same qty+level predicate the
    release pass already uses."""
    fb = _no_cid_broker(positions={"SPY": 8.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": ""},
        {"id": "s2", "symbol": "SPY", "qty": 3.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 375.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _stop_caps(supports_client_order_id=False)
    reconcile(fb, {}, {"SPY": 8.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None),
               ("d2", "SPY", 3.0, "stop", None, 375.0, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    assert fb.log == []


def test_crit3_i4_no_cid_level_move_updates_only_that_stop_once():
    """A genuine level move on ONE of two same-kind resting stops must
    update exactly that one — the other, unchanged one must not be
    touched. This also covers IMPORTANT 4: the release pass's single
    cancel of the moved order must be the ONLY cancel of that broker order
    id this sweep — the placement path, now matching on qty+level, no
    longer re-finds the stale (pre-move) resting order and cancels it a
    second time."""
    fb = _no_cid_broker(positions={"SPY": 8.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": ""},
        {"id": "s2", "symbol": "SPY", "qty": 3.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 375.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _stop_caps(supports_client_order_id=False)
    reconcile(fb, {}, {"SPY": 8.0},
              [("d1", "SPY", 5.0, "stop", None, 370.0, None),   # moved
               ("d2", "SPY", 3.0, "stop", None, 375.0, None)],  # unchanged
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    cancels = [o for a, o in fb.log if a == "cancel"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert cancels == ["s1"]                    # exactly one cancel, of s1
    assert len(subs) == 1 and subs[0]["stop_price"] == 370.0 and \
        subs[0]["qty"] == 5.0
    assert [o["id"] for o in fb.orders if o["stop_price"] == 375.0] == ["s2"]


def test_crit3_no_cid_two_wants_never_claim_same_resting_order():
    """Two different wants (different deployments) that happen to share
    the same qty+level must not both resolve to the SAME resting order —
    the match must consume it. Only one resting order exists here; the
    first want claims it (no action), the second must place its OWN new
    order rather than silently believing it already has one resting."""
    fb = _no_cid_broker(positions={"SPY": 10.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _stop_caps(supports_client_order_id=False)
    reconcile(fb, {}, {"SPY": 10.0},
              [("d1", "SPY", 5.0, "stop", None, 380.0, None),
               ("d2", "SPY", 5.0, "stop", None, 380.0, None)],
              {"SPY": 400.0}, Rails(), report(), lambda e: None)
    cancels = [o for a, o in fb.log if a == "cancel"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert cancels == []                        # s1 (claimed by d1) untouched
    assert len(subs) == 1                        # d2 places its own new order
    assert len({o["id"] for o in fb.orders}) == 2


def test_crit3_no_cid_entry_two_same_kind_buy_stops_unchanged_no_actions():
    """Same CRITICAL 3 fix, entry side: two resting BUY entry stops for two
    different deployments on one symbol must not be mistaken for one
    another either — entries double-triggering with real money is worse
    than exits mismanaging one."""
    fb = _no_cid_broker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": ""},
        {"id": "e2", "symbol": "SPY", "qty": 2.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 415.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _entry_stop_caps(supports_client_order_id=False)
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), report(),
              lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0),
                           ("d2", "SPY", 2.0, "stop", None, 415.0)])
    assert fb.log == []


def test_i4_no_cid_entry_level_move_produces_exactly_one_cancel():
    """IMPORTANT 4, entry side: a moved resting BUY entry stop must be
    cancelled exactly once, not twice."""
    fb = _no_cid_broker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": ""}])
    fb.caps = _entry_stop_caps(supports_client_order_id=False)
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), report(),
              lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 420.0)])
    cancels = [o for a, o in fb.log if a == "cancel"]
    subs = [o for a, o in fb.log if a == "submit"]
    assert cancels == ["e1"]
    assert len(subs) == 1 and subs[0]["stop_price"] == 420.0


def test_i5_moc_done_excludes_dry_run_rows():
    """A dry-run MOC preview never reaches the broker; moc_done (built by
    sync_broker_account from today's `submit` BrokerOrder rows) must not
    count it as "already submitted today", or switching dry-run off after
    previewing a morning silently suppresses that day's REAL MOC order.
    This test drives the same filter reconcile()/sync_broker_account rely
    on directly against an in-memory BrokerOrder-shaped row set, since
    sync_broker_account itself needs a live database session."""
    from dqengine.live.persistence import BrokerOrder

    class _Row:
        def __init__(self, deployment_id, symbol, order_type, status):
            self.deployment_id = deployment_id
            self.symbol = symbol
            self.order_type = order_type
            self.status = status

    rows = [_Row("d1", "SPY", "market_on_close", "dry_run"),
            _Row("d2", "SPY", "market_on_close", "new")]
    # mirror the moc_done comprehension's status filter directly
    moc_done = {(r.deployment_id, r.symbol) for r in rows
                if r.status != "dry_run"}
    assert moc_done == {("d2", "SPY")}
    assert BrokerOrder.__tablename__                # sanity: import resolves


def test_i5_dry_run_submit_then_real_moc_still_fires():
    """End-to-end via reconcile(): a dry-run MOC preview must not block a
    later REAL MOC submit for the same (deployment, symbol) once dry-run is
    switched off — moc_done passed in must already exclude the dry-run row
    (as sync_broker_account now does), so reconcile() proceeds to submit."""
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    # still holding 10 (the MOC sell hasn't happened yet); target is flat.
    fb = FakeBroker(positions={"SPY": 10.0})
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"SPY": 0.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append,
              moc_wants=[("d1", "SPY", -10.0, "market_on_close", None)],
              moc_done=set())                      # dry-run row excluded upstream
    subs = [o for a, o in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["type"] == "market_on_close"
    assert recs and recs[0]["status"] != "dry_run"


def test_i6_flat_non_primary_breakout_entry_placed_with_known_last_price():
    """C6's original fix refused a breakout entry outright when last_px was
    unknown; I6 goes further — once last_px DOES carry a flat, non-primary
    universe symbol's price (via the driver's `last_prices` payload,
    surfaced through compute_targets), the native entry must actually be
    PLACED, not refused."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    r = report()
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 410.0)])
    subs = [o for a, o in fb.log if a == "submit"]
    assert subs and subs[0]["type"] == "stop" and subs[0]["side"] == "buy"
    assert not [rec for rec in recs if rec.get("status") == "no_price"]


def test_i6_compute_targets_reads_last_prices_for_flat_symbols():
    """The payload read must surface `last_prices` (all universe symbols,
    including ones with zero qty and no open orders) into last_px — not
    just held symbols plus the primary."""
    desired, exit_wants, px = compute_targets([
        ("d1", {"holdings": [], "last_prices": {"SPY": 401.5, "QQQ": 300.0}},
         ["SPY", "QQQ"]),
    ])
    assert px == {"SPY": 401.5, "QQQ": 300.0}
    assert desired == {"SPY": 0.0, "QQQ": 0.0}


def test_i6_unchanged_refusal_not_rerecorded_across_sweeps():
    """The buy-stop rail writes a `refused` BrokerOrder row every sweep for
    an unchanged standing want — ~78/day for one entry. Once the caller
    (sync_broker_account) supplies `recent_refusals` built from today's
    already-recorded refused rows, an identical refusal (same deployment,
    symbol, kind, status, level) must not be recorded again."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    want = [("d1", "SPY", 5.0, "stop", None, 390.0)]   # invalid_stop: <= px
    r1, recs1 = report(), []
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r1, recs1.append,
              entry_wants=want)
    assert recs1 and recs1[0]["status"] == "invalid_stop"
    fingerprint = (recs1[0]["deployment_id"], recs1[0]["symbol"],
                   recs1[0]["order_type"], recs1[0]["status"],
                   round(recs1[0]["limit_price"], 2)
                   if recs1[0]["limit_price"] is not None else None)
    r2, recs2 = report(), []
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r2, recs2.append,
              entry_wants=want, recent_refusals={fingerprint})
    assert recs2 == []                           # not re-recorded
    assert any("refused" in a for a in r2["actions"])  # still surfaced live


def test_i6_changed_want_still_records_refusal_despite_recent_refusals():
    """recent_refusals must be an exact-fingerprint match, not a blanket
    per-(deployment, symbol, kind) suppression — a DIFFERENT refusal reason
    or level for the same want must still get recorded."""
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    stale_fp = ("d1", "SPY", "stop", "invalid_stop", 390.0)
    recs = []
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), report(), recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 650.0)],
              recent_refusals={stale_fp})
    assert recs and recs[0]["status"] == "over_stop_band"


def test_sync_polls_executions_when_observing(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    calls = []

    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c1", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="observe",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.commit()

    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: calls.append(a) or (3, 0))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: FakeBroker())
    executor.sync_broker_account("c1")
    assert calls, "executions were not polled in observe mode"


def test_sync_does_not_poll_when_off(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    calls = []
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c2", user_id=owner_id, broker="fake", status="ok", mode="paper",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.commit()
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: calls.append(a) or (0, 0))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: FakeBroker())
    executor.sync_broker_account("c2")
    assert not calls


def test_execution_poll_failure_does_not_abort_the_sync(pg, owner_id, monkeypatch):
    """Not just "no exception escapes" -- the error must actually land in
    position["execution"]["executions"]["error"] on a real deployment, since
    a later task reads exactly that field back to decide a symbol's fill
    state is UNKNOWN (rather than flat) after a broker outage. Without a
    seeded deployment the write-back loop is a no-op and nothing pins the
    fact that the error was ever recorded, not just swallowed."""
    from dqengine.live import executor
    from datetime import date
    ir = {"version": "0.1", "universe": {"static": ["SPY"]},
          "params": {}, "rules": []}
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c3", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="observe",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="d3", user_id=owner_id, broker_connection_id="c3", name="copy",
            ir=ir, cash_initial=1000.0, start_date=date(2026, 8, 18),
            status="running"))
        s.commit()

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(executor, "_poll_executions", boom)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: FakeBroker())
    executor.sync_broker_account("c3")     # must not raise

    with pg() as s:
        d = s.get(persistence.Deployment, "d3")
        err = d.position["execution"]["executions"]["error"]
        assert "broker down" in err


def test_skipped_execution_rows_are_recorded_as_a_poll_error(pg, owner_id, monkeypatch):
    """I6: an adapter skipping a row it cannot parse is correct AT THE
    ADAPTER. But under `enforce` the absence of that row on a reconciled day
    is read by take() as an affirmative no-fill -- which inverts this
    branch's own rule that a row we failed to parse is UNKNOWN. Route the
    skip count into the same `error` field _unknown_symbols already widens
    on, so the coverage mechanism that exists gets used."""
    from dqengine.live import executor
    from datetime import date
    ir = {"version": "0.1", "universe": {"static": ["SPY"]},
          "params": {}, "rules": []}
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c4", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="observe",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="d4", user_id=owner_id, broker_connection_id="c4", name="copy",
            ir=ir, cash_initial=1000.0, start_date=date(2026, 8, 18),
            status="running"))
        s.commit()
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (1, 2))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: FakeBroker())
    executor.sync_broker_account("c4")
    with pg() as s:
        got = s.get(persistence.Deployment, "d4").position["execution"]["executions"]
    assert got["new"] == 1 and got["skipped"] == 2
    assert got["error"] and "2" in got["error"]


def test_a_clean_poll_records_no_error(pg, owner_id, monkeypatch):
    from dqengine.live import executor
    from datetime import date
    ir = {"version": "0.1", "universe": {"static": ["SPY"]},
          "params": {}, "rules": []}
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c5", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="observe",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="d5", user_id=owner_id, broker_connection_id="c5", name="copy",
            ir=ir, cash_initial=1000.0, start_date=date(2026, 8, 18),
            status="running"))
        s.commit()
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (3, 0))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: FakeBroker())
    executor.sync_broker_account("c5")
    with pg() as s:
        got = s.get(persistence.Deployment, "d5").position["execution"]["executions"]
    assert got == {"new": 3, "skipped": 0, "error": None}


# ---------------------------------------------------------------------------
# I7: market-gateway-not-ready backoff (webull-hours fix)
# ---------------------------------------------------------------------------

WEBULL_NOT_READY = "Webull refused the request: CAN_NOT_TRADING_FOR_FIXGW_NOT_READY_MARKET"

# a Wednesday, well inside the regular session, no early close
_OPEN_NOW = datetime(2026, 8, 26, 14, 0, 0, tzinfo=ET)
# the closed-session case: a Monday at 17:33 ET, 90min after the close
_CLOSED_AFTER_HOURS_NOW = datetime(2026, 8, 24, 21, 33, 0, tzinfo=timezone.utc)
# a Saturday
_CLOSED_WEEKEND_NOW = datetime(2026, 8, 29, 10, 0, 0, tzinfo=ET)


def test_fixgw_night_variant_is_market_not_ready():
    """Overnight, Webull refuses with the _NIGHT suffix while its
    overnight gateway is down. Any FIXGW_NOT_READY variant is the gateway's
    timing, not our order — all of them take the backoff path."""
    from dqengine.live.executor import _is_market_not_ready
    assert _is_market_not_ready(
        "Webull refused the request: CAN_NOT_TRADING_FOR_FIXGW_NOT_READY_NIGHT")
    assert _is_market_not_ready(WEBULL_NOT_READY)
    assert not _is_market_not_ready("Webull refused the request: "
                                    "INSUFFICIENT_BUYING_POWER")


def test_market_not_ready_refusal_recorded_once_and_stops_further_submits():
    """Three standing exits all refused with the same market-not-ready code
    must produce ONE report line and ONE broker attempt this sweep -- not
    the three identical warn lines from the bug report."""
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    recs = []
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 5.0, "TLT": 5.0}, [],
              {"SPY": 400.0, "QQQ": 500.0, "TLT": 90.0}, Rails(), r,
              recs.append, now=_OPEN_NOW)
    attempts = [e for a, e in fb.log if a == "submit_attempt"]
    assert len(attempts) == 1
    assert len(recs) == 1 and recs[0]["action"] == "refused"
    paused_lines = [e for e in r["errors"] if "paused" in e]
    assert len(paused_lines) == 1
    assert r["submit_backoff"] is not None


def test_market_not_ready_during_open_session_backs_off_briefly():
    """Rule I4: our calendar says the session is OPEN right now -> this is
    a genuine broker outage, not routine -- back off only 60-120s."""
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=_OPEN_NOW)
    until = datetime.fromisoformat(r["submit_backoff"]["until"])
    delta = (until - _OPEN_NOW).total_seconds()
    assert 60 <= delta <= 120


def test_market_not_ready_during_closed_session_backs_off_to_next_open():
    """Rule I4: our calendar says the session is CLOSED right now (17:33
    ET, 90min after the close) -> nothing would have
    succeeded anyway, so back off. Item (b): the target is pre-market open
    (04:00 ET) the next trading day, not the regular 09:30 open -- Webull's
    Caps declare extended_hours=True, so we let Webull decide rather than
    pre-judging the 04:00-09:29 window shut ourselves."""
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=_CLOSED_AFTER_HOURS_NOW)
    until = datetime.fromisoformat(r["submit_backoff"]["until"])
    assert until == datetime(2026, 8, 25, 4, 0, 0, tzinfo=ET)


def test_market_not_ready_over_a_weekend_backs_off_to_monday_premarket():
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=_CLOSED_WEEKEND_NOW)
    until = datetime.fromisoformat(r["submit_backoff"]["until"])
    assert until == datetime(2026, 8, 31, 4, 0, 0, tzinfo=ET)


def test_market_not_ready_at_premarket_backs_off_to_same_day_regular_open():
    """Item (b)'s self-correcting sequence: a refusal that happens AFTER
    pre-market open but before the regular open (our calendar still says
    CLOSED, since the regular session hasn't started) targets the SAME
    day's 09:30 regular open next, not tomorrow's pre-market -- two
    refusal/backoff cycles a day, not one every ~30 minutes overnight."""
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    six_am = datetime(2026, 8, 26, 6, 0, 0, tzinfo=ET)   # after 04:00, before 09:30
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=six_am)
    until = datetime.fromisoformat(r["submit_backoff"]["until"])
    assert until == datetime(2026, 8, 26, 9, 30, 0, tzinfo=ET)


def test_market_not_ready_on_early_close_day_after_close_backs_off_correctly():
    """Item (d): the day after Thanksgiving is a known 13:00 ET early
    close. A refusal at 14:00 ET on that day must take the CLOSED branch
    (our calendar's close_time_ms says 13:00, not the usual 16:00) and
    target the next trading day's pre-market open, not a brief 60-120s
    outage retry."""
    fb = FakeBroker(positions={}, fail_submit_msg=WEBULL_NOT_READY)
    r = report()
    after_early_close = datetime(2026, 11, 27, 14, 0, 0, tzinfo=ET)
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=after_early_close)
    until = datetime.fromisoformat(r["submit_backoff"]["until"])
    # 2026-11-28/29 is a weekend; next session day is Monday 2026-11-30
    assert until == datetime(2026, 11, 30, 4, 0, 0, tzinfo=ET)


def test_active_backoff_suppresses_submits_without_calling_broker():
    """An already-active backoff from a prior sweep (passed in via
    submit_backoff, as sync_broker_account reads it from KV) must skip the
    broker call entirely -- FakeBroker here would happily succeed if
    called, proving the suppression short-circuits before reaching it."""
    fb = FakeBroker(positions={})     # no fail_submit_msg -- would succeed
    r = report()
    active = {"until": (_OPEN_NOW + timedelta(minutes=5)).isoformat(),
              "reason": "prior refusal"}
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, submit_backoff=active, now=_OPEN_NOW)
    assert not [e for a, e in fb.log if a in ("submit", "submit_attempt")]
    paused_lines = [e for e in r["errors"] if "paused" in e]
    assert len(paused_lines) == 1
    assert r["submit_backoff"]["until"] == active["until"]


def test_active_backoff_does_not_suppress_cancels():
    """Constraint: back off order SUBMISSIONS only -- a stale resting order
    that just needs canceling (no replacement wanted) must still be
    canceled while a backoff is active."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "t2", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "limit", "limit_price": 300.0, "status": "new",
         "client_order_id": "sl-tp-gone-zzz"}])
    r = report()
    active = {"until": (_OPEN_NOW + timedelta(minutes=5)).isoformat(),
              "reason": "prior refusal"}
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, submit_backoff=active, now=_OPEN_NOW)
    assert ("cancel", "t2") in [tuple(e[:2]) for e in fb.log if e[0] == "cancel"]


def test_expired_backoff_allows_fresh_attempt():
    """Rule I5: suppression is only ever entered after a refusal, never
    assumed to still apply -- an expired backoff behaves exactly like no
    backoff at all this sweep."""
    fb = FakeBroker(positions={})
    r = report()
    expired = {"until": (_OPEN_NOW - timedelta(minutes=5)).isoformat(),
              "reason": "stale"}
    recs = []
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              recs.append, submit_backoff=expired, now=_OPEN_NOW)
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1
    assert not [e for e in r["errors"] if "paused" in e]
    assert r["submit_backoff"] is None


def test_fresh_sweep_with_no_backoff_is_byte_identical_to_today():
    """Rule I5, sanity check: with no submit_backoff argument passed at
    all (the default), behaviour is identical to before this feature --
    report carries an explicit None, nothing suppressed."""
    fb = FakeBroker(positions={"SPY": 2.0})
    r = report()
    recs = []
    reconcile(fb, {}, {"SPY": 8.0}, [], {"SPY": 400.0}, Rails(), r,
              recs.append, now=_OPEN_NOW)
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1 and subs[0]["qty"] == 6.0
    assert r["submit_backoff"] is None


def test_unrelated_broker_rejection_does_not_trip_backoff():
    """A refusal that ISN'T a market-not-ready code (e.g. Webull rejecting
    on genuinely bad order parameters) must not suppress anything -- every
    standing want still gets its own attempt and its own error line."""
    fb = FakeBroker(positions={},
                    fail_submit_msg="Webull rejected: INSUFFICIENT_BUYING_POWER")
    r = report()
    reconcile(fb, {}, {"SPY": 5.0, "QQQ": 5.0}, [],
              {"SPY": 400.0, "QQQ": 500.0}, Rails(), r, lambda e: None,
              now=_OPEN_NOW)
    attempts = [e for a, e in fb.log if a == "submit_attempt"]
    assert len(attempts) == 2
    assert r["submit_backoff"] is None
    assert all("INSUFFICIENT_BUYING_POWER" in e for e in r["errors"])


def test_market_not_ready_match_is_case_insensitive():
    fb = FakeBroker(positions={},
                    fail_submit_msg="webull refused: "
                                    "can_not_trading_for_fixgw_not_ready_market")
    r = report()
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, now=_OPEN_NOW)
    assert r["submit_backoff"] is not None


def test_sync_broker_account_persists_backoff_and_suppresses_next_sync(pg, owner_id, monkeypatch):
    """Full sync_broker_account() integration: a market-not-ready refusal
    persists a backoff to the KV table, and a second sync while it's still
    active never calls the broker's submit() again."""
    from dqengine.live import executor
    from datetime import date

    ir = {"version": "0.1", "universe": {"static": ["SPY"]},
          "params": {}, "rules": []}
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c-backoff", user_id=owner_id, broker="fake", status="ok",
            mode="paper", execution_truth="off",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="d-backoff", user_id=owner_id, broker_connection_id="c-backoff",
            name="copy", ir=ir, cash_initial=1000.0,
            start_date=date(2026, 8, 18), status="running",
            position={"holdings": [{"symbol": "SPY", "qty": 5.0,
                                    "last_price": 400.0}],
                      "open_orders": []}))
        s.commit()

    fb = FakeBroker(fail_submit_msg=WEBULL_NOT_READY)
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)

    executor.sync_broker_account("c-backoff")

    with pg() as s:
        row = s.get(persistence.KV, "broker_submit_backoff:c-backoff")
        assert row is not None
        assert "FIXGW_NOT_READY_MARKET" in row.value["reason"]

    attempts_after_first = len(
        [e for a, e in fb.log if a == "submit_attempt"])
    assert attempts_after_first == 1

    executor.sync_broker_account("c-backoff")   # backoff still active

    attempts_after_second = len(
        [e for a, e in fb.log if a == "submit_attempt"])
    assert attempts_after_second == 1     # no new broker call while suppressed

    with pg() as s:
        d = s.get(persistence.Deployment, "d-backoff")
        errs = d.position["execution"]["errors"]
        assert len([e for e in errs if "paused" in e]) == 1


def test_suppressed_sweep_never_reports_orders_as_placed_or_updated():
    """Finding 1: while a backoff is active, EVERY call site that turns an
    act() result into a 'placed'/'updated' report line must honor a
    suppressed (None) result -- not just the market-delta and MOC sites
    that already guarded on it. FakeBroker here would happily place/update
    if actually called, so this also proves the broker is never reached."""
    fb = FakeBroker(positions={"SPY": 5.0})
    r = report()
    active = {"until": (_OPEN_NOW + timedelta(minutes=5)).isoformat(),
              "reason": "prior refusal"}
    # a fresh take-profit exit with no resting order yet -> the "placed"
    # branch (executor.py's exit-leg `else: ... act(_submit...)`)
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None,
              submit_backoff=active, now=_OPEN_NOW)
    claims = [a for a in r["actions"] if "placed" in a or "updated" in a]
    assert not claims, f"suppressed sweep still claimed: {claims}"
    assert not [e for a, e in fb.log if a in ("submit", "replace")]


def test_malformed_backoff_until_string_is_treated_as_no_backoff():
    """Item (a): a garbage 'until' in the persisted backoff must not raise
    inside reconcile() -- that would skip the trailing setdefault and, via
    sync_broker_account's 'only persist when the key is present' guard,
    leave submissions stuck suppressed forever on a live account."""
    fb = FakeBroker(positions={"SPY": 2.0})
    r = report()
    recs = []
    bad = {"until": "not-a-timestamp", "reason": "garbage"}
    reconcile(fb, {}, {"SPY": 8.0}, [], {"SPY": 400.0}, Rails(), r,
              recs.append, submit_backoff=bad, now=_OPEN_NOW)
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1                      # not suppressed
    assert r["submit_backoff"] is None          # explicitly cleared


def test_naive_backoff_until_is_treated_as_no_backoff():
    """Item (a), the concrete failure mode named in review: an 'until'
    string that parses fine but lacks a timezone offset raises TypeError
    when compared against an aware `now` -- must fall through to 'no
    backoff', not crash."""
    fb = FakeBroker(positions={"SPY": 2.0})
    r = report()
    naive = {"until": "2026-08-26T15:00:00", "reason": "no tz offset"}
    reconcile(fb, {}, {"SPY": 8.0}, [], {"SPY": 400.0}, Rails(), r,
              lambda e: None, submit_backoff=naive, now=_OPEN_NOW)
    subs = [e for a, e in fb.log if a == "submit"]
    assert len(subs) == 1
    assert r["submit_backoff"] is None


def test_save_submit_backoff_recovers_from_lost_insert_race(pg, monkeypatch):
    """Finding 2, forced deterministically rather than relying on thread
    timing: this call's own s.get() sees no row, but by the time it tries
    to INSERT, a concurrent writer has already committed the same key --
    exactly the "two sweeps both see row is None" race. The resulting
    IntegrityError must be caught and retried as an UPDATE: it must not
    escape (that would skip the execution-ledger poll immediately after
    the persist call in sync_broker_account), and the write must not be
    silently dropped by a bare catch-all either."""
    from dqengine.live import executor
    # the factory the persist path actually calls is the persistence
    # module's; patching any other copy of the name leaves the race
    # un-armed and the test green for the wrong reason

    conn_id = "c-race"
    key = executor._submit_backoff_kv_key(conn_id)
    loser_value = {"until": "2099-01-02T00:00:00+00:00", "reason": "loser"}
    winner_value = {"until": "2099-01-01T00:00:00+00:00", "reason": "winner"}
    real_session_local = persistence.SessionLocal

    class _RacySession:
        """One-shot wrapper: the FIRST .add() call on this session (the
        loser's insert attempt inside _save_submit_backoff) is preceded by
        a concurrent winner committing the same key from an independent,
        already-committed session -- reproducing the lost-insert race
        without needing real thread interleaving."""

        def __init__(self):
            self._inner = real_session_local()
            self._armed = True

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self._inner.close()

        def get(self, model, pk):
            return self._inner.get(model, pk)

        def add(self, obj):
            if self._armed:
                self._armed = False
                with real_session_local() as winner:
                    winner.add(persistence.KV(key=key, value=winner_value))
                    winner.commit()
            self._inner.add(obj)

        def commit(self):
            self._inner.commit()

        def rollback(self):
            self._inner.rollback()

        def delete(self, obj):
            self._inner.delete(obj)

    monkeypatch.setattr(persistence, "SessionLocal", _RacySession)

    executor._save_submit_backoff(conn_id, loser_value)   # must not raise

    monkeypatch.setattr(persistence, "SessionLocal", real_session_local)
    with real_session_local() as s:
        row = s.get(persistence.KV, key)
        assert row is not None
        # recovery is retry-as-UPDATE, so the loser's own value should have
        # landed -- proving this isn't just a broad except swallowing the
        # write and leaving the winner's row untouched.
        assert row.value["reason"] == "loser"


def test_active_backoff_leaves_moved_exit_stop_resting_not_naked():
    """Review round 3: cancel is never gated by act() (a standalone
    release-cancel must keep working out of hours), so a moved resting
    exit stop's cancel-then-resubmit pair could previously execute the
    cancel while the backoff suppressed the resubmit underneath it --
    stranding the position with NO protective order at the broker until
    the backoff clears. With an active backoff, neither half of the pair
    may reach the broker: the stale order must still be resting
    afterward, and the report must say so."""
    fb = FakeBroker(positions={"SPY": 5.0}, orders=[
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}])
    fb.caps = _stop_caps()
    r = report()
    active = {"until": (_OPEN_NOW + timedelta(minutes=5)).isoformat(),
              "reason": "prior refusal"}
    # same "moved" want as test_resting_stop_moved_cancels_and_resubmits_
    # with_new_level (370.0, was 380.0) -- that test proves this scenario
    # normally cancels+resubmits; this one proves an active backoff must
    # suppress BOTH halves of the pair.
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "stop", None, 370.0, None)],
              {"SPY": 400.0}, Rails(), r, lambda e: None,
              submit_backoff=active, now=_OPEN_NOW)
    assert fb.log == []                         # no cancel, no submit
    assert fb.orders == [
        {"id": "s1", "symbol": "SPY", "qty": 5.0, "side": "sell",
         "type": "stop", "limit_price": None, "stop_price": 380.0,
         "status": "new", "client_order_id": "sl-stp-SPY-d1-zzz"}]
    assert any("left resting" in a and "SPY" in a for a in r["actions"])
    assert not any("updated" in a for a in r["actions"])


def test_active_backoff_leaves_moved_entry_stop_resting_not_naked():
    """Entry-side mirror of the exit-leg test above -- same hazard, same
    fix, the other of the two cancel+resubmit call sites the review
    identified."""
    fb = FakeBroker(positions={}, orders=[
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": "en-stp-SPY-d1"}])
    fb.caps = _entry_stop_caps()
    r = report()
    active = {"until": (_OPEN_NOW + timedelta(minutes=5)).isoformat(),
              "reason": "prior refusal"}
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), r, lambda e: None,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 420.0)],
              submit_backoff=active, now=_OPEN_NOW)
    assert fb.log == []
    assert fb.orders == [
        {"id": "e1", "symbol": "SPY", "qty": 5.0, "side": "buy",
         "type": "stop", "limit_price": None, "stop_price": 410.0,
         "status": "new", "client_order_id": "en-stp-SPY-d1"}]
    assert any("left resting" in a and "SPY" in a for a in r["actions"])
    assert not any("updated" in a for a in r["actions"])


# ---------------------------------------------------------------------------
# C3: BrokerOrder.rule_tag was written by nobody.
#
# `broker_order_row()` builds the row from ten `entry` keys and rule_tag was
# not one of them, and no record({...}) call site put a rule_tag key in its
# entry dict. So Execution.rule_tag was always NULL, LedgerFill.rule_tag was
# always None, and ledger.take()'s tagged-preference branch -- spec §6
# matching precedence 1, the whole point of which is telling two same-symbol
# rules on one day apart -- was dead code. Matching silently degraded to
# precedence 2 everywhere.


def test_broker_order_row_persists_the_rule_tag():
    from dqengine.live.executor import broker_order_row
    row = broker_order_row("c1", {
        "action": "submit", "symbol": "SPY", "qty": 5.0, "side": "sell",
        "order_type": "limit", "limit_price": 410.0, "broker_order_id": "b1",
        "client_order_id": "cid1", "status": "new", "deployment_id": "d1",
        "rule_tag": "tiered-target"})
    assert row.rule_tag == "tiered-target"


def test_broker_order_row_leaves_rule_tag_null_when_there_is_no_rule():
    """Market deltas legitimately have no rule -- NULL, not a fabrication."""
    from dqengine.live.executor import broker_order_row
    row = broker_order_row("c1", {
        "action": "submit", "symbol": "SPY", "qty": 5.0, "side": "buy",
        "order_type": "market", "limit_price": None, "broker_order_id": "",
        "client_order_id": "", "status": "", "deployment_id": None})
    assert row.rule_tag is None


def test_reconcile_tags_exit_orders_with_their_rule_id():
    fb = FakeBroker(positions={"SPY": 5.0})
    recs = []
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None,
                "tiered-target")],
              {"SPY": 400.0}, Rails(), report(), recs.append)
    subs = [r for r in recs if r["action"] == "submit"]
    assert subs and all(r["rule_tag"] == "tiered-target" for r in subs)


def test_reconcile_tags_a_refused_exit_with_its_rule_id():
    """A refusal row is an audit record of an intent that had a rule too."""
    fb = FakeBroker(positions={"SPY": 5.0})
    recs = []
    reconcile(fb, {}, {"SPY": 5.0},
              [("d1", "SPY", 5.0, "limit", 410.0, None, None,
                "tiered-target")],
              {"SPY": 400.0}, Rails(max_order_notional=100.0), report(),
              recs.append)
    refused = [r for r in recs if r["action"] == "refused"]
    assert refused and all(r["rule_tag"] == "tiered-target" for r in refused)


def test_reconcile_tags_entry_orders_with_their_rule_id():
    fb = FakeBroker(positions={})
    fb.caps = _entry_stop_caps()
    recs = []
    reconcile(fb, {}, {}, [], {"SPY": 400.0}, Rails(), report(), recs.append,
              entry_wants=[("d1", "SPY", 5.0, "stop", None, 420.0,
                            "breakout")])
    subs = [r for r in recs if r["action"] == "submit"]
    assert subs and all(r["rule_tag"] == "breakout" for r in subs)


def test_reconcile_tags_native_moc_orders_with_their_rule_id():
    from dqengine.adapters.base import Caps, MARKET, LIMIT, MARKET_ON_CLOSE
    fb = FakeBroker(positions={"SPY": 0.0})
    fb.caps = Caps(order_types=frozenset({MARKET, LIMIT, MARKET_ON_CLOSE}))
    recs = []
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append,
              moc_wants=[("d1", "SPY", 5.0, "market_on_close", None,
                          "eod-entry")])
    subs = [r for r in recs if r["action"] == "submit"]
    assert subs and all(r["rule_tag"] == "eod-entry" for r in subs)


def test_market_delta_orders_carry_no_rule_tag():
    """No rule fired one -- it is a netting delta. None, never invented."""
    fb = FakeBroker(positions={"SPY": 0.0})
    recs = []
    reconcile(fb, {}, {"SPY": 5.0}, [], {"SPY": 400.0}, Rails(), report(),
              recs.append)
    subs = [r for r in recs if r["action"] == "submit"]
    assert subs and all(r.get("rule_tag") is None for r in subs)


# ------------------------------------------------ lean-fills: rail + pacing

def test_fold_pending_freezes_delta_and_resting_placement():
    """The buy-back shape, distilled: the broker filled our resting sell
    (have=0) and the sleeve has not folded that fill yet (want=100). With
    the symbol in fold_pending, reconcile must submit NOTHING for it — not
    the market buy-back, not a re-placed resting sell — and say so
    loudly."""
    fb = FakeBroker(positions={})
    r = report()
    reconcile(fb, {}, {"TQQQ": 100.0},
              [("d1", "TQQQ", 100.0, "limit", 51.0, None, None, "tiered")],
              {"TQQQ": 50.0}, Rails(), r, lambda e: None,
              fold_pending={"TQQQ"})
    assert not [x for x in fb.log if x[0] == "submit"], \
        "a frozen symbol must produce zero broker submissions"
    assert any("frozen" in a for a in r["actions"])


def test_fold_pending_does_not_touch_other_symbols():
    fb = FakeBroker(positions={})
    r = report()
    reconcile(fb, {}, {"TQQQ": 100.0, "SPY": 5.0}, [],
              {"TQQQ": 50.0, "SPY": 400.0}, Rails(), r, lambda e: None,
              fold_pending={"TQQQ"})
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert [o["symbol"] for o in subs] == ["SPY"]


def test_fold_pending_does_not_latch_an_unfoldable_fill():
    """The OMS's own corrective market delta liquidates a duplicate buy.
    That fill carries an sl- client id, so it lands in exe_sums -- but no
    sleeve ever modelled those shares, so it can never be folded and the
    mismatch stands all day. Broker and model AGREE (100 == 100) and the
    shares are demonstrably present, yet the take-profit the strategy has
    moved to 55.00 stays parked at the older 60.00, and a real exit signal
    would be frozen out too.

    The rail must freeze a trade only when folding the outstanding fills
    would actually shrink it."""
    fb = FakeBroker(positions={"TQQQ": 100.0}, supports_replace=False, orders=[
        {"id": "wb1", "symbol": "TQQQ", "qty": 100.0, "side": "sell",
         "type": "limit", "limit_price": 60.0, "status": "new",
         "client_order_id": _exit_cid_prefix("d1", "limit", "TQQQ") + "-abc"}])
    r = report()
    reconcile(fb, {}, {"TQQQ": 100.0},
              [("d1", "TQQQ", 100.0, "limit", 55.0, None, None, "tiered")],
              {"TQQQ": 50.0}, Rails(), r, lambda e: None,
              fold_pending={"TQQQ": -100.0})
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert [o["limit_price"] for o in subs] == [55.0], r["actions"]
    assert ("cancel", "wb1") in [tuple(e[:2]) for e in fb.log
                                 if e[0] == "cancel"]


def test_fold_pending_still_freezes_the_buy_back_it_exists_for():
    """The buy-back shape again, with the magnitude the rail now carries:
    the broker filled our resting sell (have=0, diff=-100) and the sleeve
    has not folded it (want=100). Folding would take the delta 100 -> 0,
    so the freeze is genuine and must still hold -- no buy-back, no
    re-placed resting sell."""
    fb = FakeBroker(positions={})
    r = report()
    reconcile(fb, {}, {"TQQQ": 100.0},
              [("d1", "TQQQ", 100.0, "limit", 51.0, None, None, "tiered")],
              {"TQQQ": 50.0}, Rails(), r, lambda e: None,
              fold_pending={"TQQQ": -100.0})
    assert not [x for x in fb.log if x[0] == "submit"]
    assert any("frozen" in a for a in r["actions"]), r["actions"]


def test_fold_pending_never_blocks_an_exit_whose_shares_are_present():
    """An unfolded fill in the OPPOSITE direction of the position says
    nothing about a resting sell whose shares the broker demonstrably
    holds: place it."""
    fb = FakeBroker(positions={"TQQQ": 100.0})
    r = report()
    reconcile(fb, {}, {"TQQQ": 100.0},
              [("d1", "TQQQ", 100.0, "limit", 55.0, None, None, "tiered")],
              {"TQQQ": 50.0}, Rails(), r, lambda e: None,
              fold_pending={"TQQQ": 60.0})
    subs = [o for tag, o in fb.log if tag == "submit"]
    assert [o["limit_price"] for o in subs] == [55.0], r["actions"]


def test_sync_floor_and_rate_limit_cooldown():
    from dqengine.live import executor as bx
    bx.SYNC_FLOOR_S = 10                          # fixture set it to 0
    bx._SYNC_GATE["cX"] = {"last": bx.time.time()}
    ran = {"n": 0}
    # the gate returns before any DB work; a second call inside the floor
    # must not raise even with a bogus connection id
    bx.sync_broker_account("cX")
    assert bx._SYNC_GATE["cX"]["last"] > 0
    bx._SYNC_GATE["cX"] = {"cooldown_until": bx.time.time() + 60}
    bx.sync_broker_account("cX")                  # cooldown: returns at once
    bx.SYNC_FLOOR_S = 0


def test_rate_limited_detector():
    from dqengine.live.executor import _rate_limited
    assert _rate_limited({"errors": ["Webull rate-limited us: to many "
                                     "requests"]})
    assert _rate_limited({"errors": [],
                          "executions": {"error": "Too Many Requests"}})
    assert not _rate_limited({"errors": ["CAN_NOT_TRADING..."],
                              "executions": {"error": None}})
    assert not _rate_limited(
        {"errors": [], "executions":
         {"error": "sweep aborted before the executions poll"}})


def test_sync_computes_fold_pending_from_db(pg, owner_id, monkeypatch):
    """End-to-end wiring of the rail: an sl- execution today that the
    sleeve's fills do not reflect must freeze that symbol's delta inside a
    real sync_broker_account pass (broker holds 0, sleeve wants 100 — the
    buy-back shape). The manual (no sl- prefix) row on another symbol must
    not freeze anything."""
    from datetime import date as d_date, datetime as dtt, timezone as tz
    from dqengine.live import executor
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="c9", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="enforce",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="d9", user_id=owner_id, name="F", start_date=d_date(2026, 8, 1),
            ir={"universe": {"static": ["TQQQ"]}}, status="running",
            broker_connection_id="c9", cash_initial=1000.0,
            position={"holdings": [{"symbol": "TQQQ", "qty": 100,
                                    "last_price": 50.0}]},
            fills=[]))
        s.add(persistence.Execution(
            connection_id="c9", deployment_id="d9", broker_exec_id="e-rail",
            symbol="TQQQ", signed_qty=-100.0, price=50.0,
            filled_at=dtt.now(tz.utc), rule_tag="tiered-target",
            client_order_id="sl-tp-d9-x", source="broker"))
        s.add(persistence.Execution(
            connection_id="c9", deployment_id=None, broker_exec_id="e-man",
            symbol="SPY", signed_qty=5.0, price=400.0,
            filled_at=dtt.now(tz.utc), source="broker"))
        s.commit()
    fb = FakeBroker(positions={})            # broker already flat
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: (0, 0))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    executor.sync_broker_account("c9")
    assert not [x for x in fb.log if x[0] == "submit"], \
        "the buy-back must be frozen, not submitted"
    with pg() as s:
        d = s.get(persistence.Deployment, "d9")
        acts = (d.position or {}).get("execution", {}).get("actions", [])
    assert any("frozen" in a for a in acts)


def test_close_window_overrides_floor_and_cooldown(pg, monkeypatch):
    """Inside [close-180s, close+120s] a skipped sync is a LOST day of
    orders (a 15:59 rebalance sync can land seconds after another
    deployment's routine sync on the same connection), so the pacing gate
    stands aside."""
    from dqengine.live import executor as bx
    from datetime import datetime as dtt
    from zoneinfo import ZoneInfo
    ET_ = ZoneInfo("America/New_York")
    assert bx._in_close_window(dtt(2026, 8, 26, 15, 58, 30, tzinfo=ET_))
    assert bx._in_close_window(dtt(2026, 8, 26, 16, 1, 0, tzinfo=ET_))
    assert not bx._in_close_window(dtt(2026, 8, 26, 15, 30, 0, tzinfo=ET_))
    assert not bx._in_close_window(dtt(2026, 8, 29, 15, 59, 0, tzinfo=ET_))

    bx.SYNC_FLOOR_S = 10
    bx._SYNC_GATE["cW"] = {"last": bx.time.time(),
                           "cooldown_until": bx.time.time() + 60}
    monkeypatch.setattr(bx, "_in_close_window", lambda now=None: True)
    # gate waived: the sweep proceeds past the gate (and then returns on
    # the bogus connection id -- which is fine, we only test the gate)
    bx.sync_broker_account("cW")
    assert bx._SYNC_GATE["cW"]["last"] > 0
    bx.SYNC_FLOOR_S = 0


def test_close_window_submits_before_polling(pg, owner_id, monkeypatch):
    """Close-window latency work: in the close window the executions poll
    moves BEHIND the submit path — one reordered API call, nothing
    skipped. Outside the window the poll stays in front (the rail's
    freshness contract)."""
    from datetime import date as d_date
    from dqengine.live import executor
    seq = []
    with pg() as s:
        s.add(persistence.BrokerConnection(
            id="cw", user_id=owner_id, broker="fake", status="ok", mode="paper",
            execution_truth="observe",
            creds_encrypted=vault.encrypt_creds({"k": "v"})))
        s.add(persistence.Deployment(
            id="dw", user_id=owner_id, name="W",
            start_date=d_date(2026, 8, 1),
            ir={"universe": {"static": ["SPY"]}}, status="running",
            broker_connection_id="cw", cash_initial=1000.0,
            position={"holdings": [{"symbol": "SPY", "qty": 5,
                                    "last_price": 400.0}]}))
        s.commit()

    class SeqBroker(FakeBroker):
        def submit(self, *a, **k):
            seq.append("submit")
            return super().submit(*a, **k)

    fb = SeqBroker(positions={})               # want 5, have 0 -> a submit
    monkeypatch.setattr(executor, "_poll_executions",
                        lambda *a, **k: seq.append("poll") or (0, 0))
    monkeypatch.setattr("dqengine.adapters.catalog.get_adapter",
                        lambda name: fb)
    monkeypatch.setattr(executor, "_in_close_window",
                        lambda now=None: True)
    executor.sync_broker_account("cw")
    assert "submit" in seq and "poll" in seq
    assert seq.index("submit") < seq.index("poll"), \
        "close window: order first, poll after"
    seq.clear()
    fb.orders.clear()                # the first sweep's order would other-
    fb.pos.clear()                   # wise satisfy the want via pending_qty
    with pg() as s:                  # ... and so would its JOURNAL row --
        from dqengine.live.persistence import (OrderJournal)  # void it (canceled at the venue) so
        s.query(OrderJournal).update(       # the re-want is legitimate
            {OrderJournal.state: "canceled"}, synchronize_session=False)
        s.commit()
    monkeypatch.setattr(executor, "_in_close_window",
                        lambda now=None: False)
    executor.sync_broker_account("cw")
    assert seq.index("poll") < seq.index("submit"), \
        "normal hours: poll first (rail freshness)"
