"""The plugin door: pip-installed entry points, bundled fallbacks, and
every way a broker id can fail to resolve -- each loudly."""
from importlib.metadata import EntryPoint

import pytest

from dqengine import brokers
from dqengine.brokers import BadAdapter, BrokerConflict, UnknownBroker


class _Caps:
    order_types = frozenset({"market"})
    tifs = frozenset({"day"})
    qty_step = 1.0
    supports_short = False


class GoodAdapter:
    id = "good"
    caps = _Caps()

    def positions(self, creds): return {}
    def open_orders(self, creds): return []
    def submit(self, creds, *a, **k): return {}
    def cancel(self, creds, order_id): return None
    def fetch_balance(self, creds): return {}


class NoCaps:
    def positions(self, c): ...
    def open_orders(self, c): ...
    def submit(self, c, *a, **k): ...
    def cancel(self, c, i): ...
    def fetch_balance(self, c): ...


class HalfAdapter:
    caps = _Caps()

    def positions(self, c): ...


def _ep(name, target, dist=None):
    ep = EntryPoint(name, target, brokers.GROUP)
    if dist is not None:
        # importlib.metadata attaches `.dist` on real entry points; the
        # conflict message names distributions through it
        object.__setattr__(ep, "dist", type("D", (), {"name": dist})())
    return ep


HERE = __name__


def test_entry_point_wins_and_builtin_fills_the_rest():
    eps = [_ep("good", f"{HERE}:GoodAdapter")]
    assert brokers.available(eps) == ["alpaca", "alpaca-paper", "good"]
    inst = brokers.load("good", eps)
    assert isinstance(inst, GoodAdapter)


def test_unknown_broker_names_what_is_installed_and_the_pip_hint():
    with pytest.raises(UnknownBroker, match=r"installed: alpaca, alpaca-paper, good.*pip install deployquant-webull"):
        brokers.load("webull", [_ep("good", f"{HERE}:GoodAdapter")])


def test_two_distributions_claiming_one_id_is_a_conflict_not_a_coin_flip():
    eps = [_ep("good", f"{HERE}:GoodAdapter", dist="dq-a"),
           _ep("good", f"{HERE}:GoodAdapter", dist="dq-b")]
    with pytest.raises(BrokerConflict, match=r"dq-a.*dq-b"):
        brokers.load("good", eps)
    # the conflict does not hide the other brokers from listing
    assert "alpaca-paper" in brokers.available(eps)


def test_a_plugin_overriding_a_bundled_id_replaces_it_without_conflict():
    """One entry point named like a builtin is a deliberate override (that is
    how the bundled adapter itself is registered once pip-installed)."""
    eps = [_ep("alpaca-paper", f"{HERE}:GoodAdapter")]
    assert isinstance(brokers.load("alpaca-paper", eps), GoodAdapter)


@pytest.mark.parametrize("target,msg", [
    (f"{HERE}:NoCaps", "declares no usable caps"),
    (f"{HERE}:HalfAdapter", r"missing adapter methods: \['open_orders', 'submit', 'cancel', 'fetch_balance'\]"),
    (f"{HERE}:_ep", "not a class"),
])
def test_a_malformed_plugin_is_refused_before_credentials_touch_it(target, msg):
    with pytest.raises(BadAdapter, match=msg):
        brokers.load("bad", [_ep("bad", target)])


def test_builtin_alpaca_paper_resolves_from_the_bundled_adapter():
    """No entry points at all (a source checkout never pip-installed): the
    BUILTIN table alone must still reach the bundled adapter."""
    cls = brokers.load_class("alpaca-paper", [])
    assert cls.__name__ == "AlpacaPaperAdapter"
    assert cls.caps.paper is True and "market" in cls.caps.order_types


def test_real_installed_entry_points_are_read_from_the_group(monkeypatch):
    monkeypatch.setattr(brokers, "_installed",
                        lambda: [_ep("good", f"{HERE}:GoodAdapter")])
    assert "good" in brokers.available()
