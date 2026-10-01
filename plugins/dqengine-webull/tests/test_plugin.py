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
