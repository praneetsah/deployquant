def test_entry_point_resolves_and_passes_the_shape_check():
    from dqengine import brokers
    cls = brokers.load_class("webull")
    assert cls.__name__ == "WebullAdapter"
    assert cls.caps.order_types
