"""Which ids `catalog.get_adapter` answers for, and which the platform's
broker dropdown shows.

They are not the same list, and treating them as one list is what made
`dqengine live --broker alpaca-paper` create its rows, tick, publish
intents and then sweep nothing: the loader offers `alpaca-paper`, the LEAN
roster does not carry it, and the sweep's `except (KeyError, LookupError)`
turned the resolution failure into a silent "skipped".
"""
import pytest

from dqengine import brokers
from dqengine.adapters import catalog as registry
from dqengine.adapters.alpaca import AlpacaAdapter
from dqengine.adapters.alpaca_paper import AlpacaPaperAdapter


def test_the_cli_default_broker_resolves():
    """`alpaca-paper` is the id docs/live.md tells people to start with."""
    got = registry.get_adapter("alpaca-paper")
    assert isinstance(got, AlpacaPaperAdapter)
    assert isinstance(got, AlpacaAdapter)


def test_the_loader_and_the_catalog_agree_on_every_installed_id():
    for broker_id in brokers.available():
        assert type(registry.get_adapter(broker_id)) is \
            brokers.load_class(broker_id), broker_id


def test_the_dropdown_is_the_lean_roster_and_nothing_else():
    """`catalog()` is what /api/brokers/catalog returns. A paper-pinned
    variant of a broker the user can already connect is not a row: the
    platform's Alpaca connection carries paper or live in its credentials."""
    rows = registry.catalog()
    assert [r["id"] for r in rows] == list(registry.BROKERS)
    assert "alpaca-paper" not in {r["id"] for r in rows}


def test_an_id_nothing_claims_names_what_is_installed():
    with pytest.raises(brokers.UnknownBroker) as e:
        registry.get_adapter("etrade")
    assert "alpaca" in str(e.value)
    # every caller catches LookupError, and UnknownBroker is one
    assert isinstance(e.value, LookupError)


def test_a_roster_slot_with_no_adapter_still_refuses_by_name():
    with pytest.raises(LookupError) as e:
        registry.get_adapter("ibkr")
    assert "Interactive Brokers" in str(e.value)
