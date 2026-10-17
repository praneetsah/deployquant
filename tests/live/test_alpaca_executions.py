"""Alpaca's fill feed (/v2/account/activities/FILL) reports a short sale
with side "sell_short" (the FIX side vocabulary its SDK's OrderSide lists),
not "sell". raoc, 2026-10-15: the short legs of a dispersion strategy
filled at Alpaca, the adapter skipped those rows as malformed, nothing was
booked, and the sweep bought the "unknown" short back the same morning."""
from datetime import datetime, timezone

from dqengine.adapters import alpaca as mod


def _fills(monkeypatch, rows):
    calls = []

    def fake_req(creds, method, path, body=None, params=None):
        calls.append((method, path, params))
        return rows if not calls[:-1] else []
    monkeypatch.setattr(mod, "_req", fake_req)
    return calls


def _row(side, qty="218", sym="CAG"):
    return {"id": f"2026101513310100::{sym}", "activity_type": "FILL",
            "transaction_time": "2026-10-15T13:31:01.5Z", "type": "fill",
            "price": "14.47", "qty": qty, "side": side, "symbol": sym,
            "order_id": "0d1e2f3a-aaaa-bbbb-cccc-000000000001",
            "leaves_qty": "0", "cum_qty": qty, "order_status": "filled"}


def test_a_short_sale_fill_is_a_sell(monkeypatch):
    _fills(monkeypatch, [_row("sell_short")])
    batch = mod.AlpacaAdapter().executions({"key": "k", "secret": "s",
                                            "paper": True})
    assert batch.skipped == 0
    assert [(r["symbol"], r["side"], r["qty"]) for r in batch] == \
        [("CAG", "sell", 218.0)]
    assert batch[0]["filled_at"] == datetime(2026, 10, 15, 13, 31, 1, 500000,
                                            tzinfo=timezone.utc)


def test_the_other_fix_sides_map_too(monkeypatch):
    _fills(monkeypatch, [_row("sell_short_exempt", "27", "TSN"),
                         _row("buy_minus", "5", "ADM"),
                         _row("sell_plus", "6", "MDLZ")])
    batch = mod.AlpacaAdapter().executions({"key": "k", "secret": "s",
                                            "paper": True})
    assert batch.skipped == 0
    assert sorted((r["symbol"], r["side"]) for r in batch) == \
        [("ADM", "buy"), ("MDLZ", "sell"), ("TSN", "sell")]


def test_an_unknown_side_is_still_skipped_not_guessed(monkeypatch):
    _fills(monkeypatch, [_row("cross")])
    batch = mod.AlpacaAdapter().executions({"key": "k", "secret": "s",
                                            "paper": True})
    assert batch.skipped == 1 and list(batch) == []
