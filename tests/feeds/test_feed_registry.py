"""The feed plugin door: the same rules the broker loader applies, in its
own entry-point group."""
from importlib.metadata import EntryPoint

import pytest

from dqengine import cli, feeds
from dqengine.feeds import registry as reg
from dqengine.feeds.alpaca import AlpacaQuoteFeed
from dqengine.feeds.registry import BadFeed, FeedConflict, UnknownFeed


class GoodFeed:
    def __init__(self, **cfg):
        self.cfg = cfg

    def subscribe(self, symbols): ...
    def unsubscribe(self, symbols): ...
    def poll(self, timeout=1.0): return 0
    def health(self, symbols=(), now_et=None, policy=None): return None
    def close(self): ...


class HalfFeed:
    def subscribe(self, symbols): ...


def _ep(name, target, dist=None):
    ep = EntryPoint(name, target, reg.GROUP)
    if dist is not None:
        object.__setattr__(ep, "dist", type("D", (), {"name": dist})())
    return ep


HERE = __name__


def test_the_bundled_alpaca_feed_is_always_there():
    assert "alpaca" in feeds.available([])
    assert feeds.load_class("alpaca", []) is AlpacaQuoteFeed


def test_an_entry_point_is_listed_and_loaded_with_its_config():
    eps = [_ep("good", f"{HERE}:GoodFeed")]
    assert feeds.available(eps) == ["alpaca", "good"]
    inst = feeds.get_feed("good", eps, symbols=["TQQQ"])
    assert isinstance(inst, GoodFeed) and inst.cfg == {"symbols": ["TQQQ"]}


def test_an_entry_point_overrides_the_builtin_of_the_same_id():
    eps = [_ep("alpaca", f"{HERE}:GoodFeed")]
    assert feeds.load_class("alpaca", eps) is GoodFeed


def test_an_unknown_feed_names_what_is_installed_and_how_to_get_more():
    with pytest.raises(UnknownFeed) as e:
        feeds.get_feed("nasdaq", [])
    assert "alpaca" in str(e.value) and "pip install deployquant-nasdaq" in str(e.value)


def test_two_distributions_claiming_one_id_raise_instead_of_picking_one():
    eps = [_ep("dup", f"{HERE}:GoodFeed", dist="deployquant-a"),
           _ep("dup", f"{HERE}:GoodFeed", dist="deployquant-b")]
    with pytest.raises(FeedConflict) as e:
        feeds.load_class("dup", eps)
    assert "deployquant-a" in str(e.value) and "deployquant-b" in str(e.value)


def test_a_class_that_is_not_a_feed_is_refused_before_it_is_constructed():
    with pytest.raises(BadFeed) as e:
        feeds.load_class("half", [_ep("half", f"{HERE}:HalfFeed")])
    assert "poll" in str(e.value) and "close" in str(e.value)
    with pytest.raises(BadFeed):
        feeds.load_class("nope", [_ep("nope", f"{HERE}:HERE")])


def test_the_cli_lists_the_installed_feeds(capsys):
    assert cli.main(["feeds"]) == 0
    assert "alpaca" in capsys.readouterr().out.splitlines()


def test_the_package_exports_the_pieces_a_driver_needs():
    for name in ("QuoteFeed", "MinuteZipStore", "check_feed", "backfill_day",
                 "get_feed", "SilencePolicy", "QuoteBoard"):
        assert hasattr(feeds, name), name
