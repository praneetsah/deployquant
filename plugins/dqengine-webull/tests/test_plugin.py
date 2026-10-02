def test_entry_point_resolves_and_passes_the_shape_check():
    from dqengine import brokers
    cls = brokers.load_class("webull")
    assert cls.__name__ == "WebullAdapter"
    assert cls.caps.order_types


def test_webull_says_it_cannot_report_fills_of_earlier_orders():
    """executions() is list_today_orders. A GTC order placed on an earlier
    day that fills today never reaches the ledger, and the deploy endpoint
    has to know that."""
    from dqengine import brokers
    assert brokers.load_class("webull").caps.fills_of_earlier_orders is False


# the SHAPE query_order_detail returned live on 2026-10-01 for a GTC
# take-profit that filled days after it was placed. Every value is made up;
# the keys, their nesting and the timestamp format are the venue's.
_DETAIL_FILLED = {
    "items": [{"symbol": "TQQQ", "commission": "0.000", "category": "US_ETF",
               "filled_price": "61.27", "filled_qty": "40",
               "last_filled_time": "2026-09-16 13:30:01.179+0000",
               "order_status": "FILLED", "order_type": "LIMIT",
               "place_time": "2026-09-10 13:32:04.309+0000", "qty": "40",
               "entrust_type": "QTY", "side": "SELL", "limit_price": "60.50",
               "instrument_id": "900000001", "currency": "USD",
               "transaction_fee": "0.08"}],
    "combo_type": "NORMAL", "combo_ticker_type": "ETF",
    "client_order_id": "sl-tp-x", "order_id": "WBX0000000000000000000001A",
    "extended_hours_trading": False, "tif": "GTC", "order_type": "LMT"}
_DETAIL_OPEN = {
    "items": [{"symbol": "TQQQ", "commission": "0.000", "filled_qty": "0",
               "order_status": "SUBMITTED", "order_type": "LIMIT",
               "qty": "40", "side": "SELL", "limit_price": "70.10",
               "transaction_fee": "0.09"}],
    "client_order_id": "sl-tp-y", "order_id": "WBX0000000000000000000002B",
    "tif": "GTC", "order_type": "LMT"}


def _adapter_returning(payload, seen):
    from datetime import datetime  # noqa: F401
    from dqengine_webull.adapter import WebullAdapter

    class _Order:
        def query_order_detail(self, account_id, client_order_id):
            seen.append((account_id, client_order_id))
            return payload

    class _Api:
        order = _Order()

    a = WebullAdapter()
    a._api = lambda creds: _Api()
    return a


def test_order_executions_reads_a_fill_of_an_order_placed_days_earlier():
    from datetime import datetime, timezone
    seen = []
    a = _adapter_returning(_DETAIL_FILLED, seen)
    status, rows = a.order_executions({"account_id": "A"}, "sl-tp-x")
    assert seen == [("A", "sl-tp-x")]
    assert status == "FILLED"
    assert len(rows) == 1 and rows.skipped == 0
    r = rows[0]
    assert (r["side"], r["qty"], r["price"]) == ("sell", 40.0, 61.27)
    assert r["filled_at"] == datetime(2026, 9, 16, 13, 30, 1, 179000,
                                      tzinfo=timezone.utc)
    assert r["fees"] == 0.08
    # the id executions() would have built for the same item
    assert r["broker_exec_id"] == "WBX0000000000000000000001A:0"
    assert r["client_order_id"] == "sl-tp-x"


def test_order_executions_of_a_resting_order_has_no_rows():
    status, rows = _adapter_returning(_DETAIL_OPEN, []).order_executions(
        {"account_id": "A"}, "sl-tp-y")
    assert status == "SUBMITTED" and list(rows) == []


def test_webull_recovers_earlier_fills_through_its_order_lookup():
    from dqengine import brokers
    from dqengine.live import capabilities
    assert capabilities.reports_earlier_fills(brokers.load_class("webull")())
