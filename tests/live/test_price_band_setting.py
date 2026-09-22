"""The limit-price band is a money-path rail: too tight and a legitimate
take-profit is silently downgraded from "rests at the broker" to "platform
watches and market-sells" (rails.price_band_pct, executor.py:969). It has
always been read from connection settings but was not settable through the
API, so the 5% default was effectively hardcoded — which refuses any target
set more than 5% above the market, i.e. most take-profits."""
import os
import re

EXEC = os.path.join(os.path.dirname(__file__), "..", "..",
                    "dqengine", "live", "executor.py")


def test_executor_reads_it_from_connection_settings():
    src = open(EXEC).read()
    assert 'price_band_pct=float(s.get("price_band_pct", 50.0))' in src


def test_band_applies_to_limits_only():
    """Stops legitimately sit far from the market and have their own check."""
    src = open(EXEC).read()
    # The EXIT path used to hard-code "sell". Exits now close shorts too, so
    # it passes the real side — which is strictly more directional, not less.
    assert "limit_price_refusal(side, level, px, rails)" in src
    # ENTRIES now pass their real side too: a python entry may be an opening
    # short, and applying the buy rule to it would refuse every legitimate
    # one and accept the dangerous one.
    assert "limit_price_refusal(e_side, level, px, rails)" in src
    # and nothing runs a stop through the limit band
    assert "limit_price_refusal" not in src[src.index("stop_band_pct"):
                                            src.index("stop_band_pct") + 400]


def test_the_exit_band_reasons_in_both_directions():
    """A sell-exit is refused below the market, a buy-exit above it."""
    from dqengine.live.executor import limit_price_refusal, rails_from
    rails = rails_from({}, "paper", True)
    assert limit_price_refusal("sell", 90.0, 100.0, rails)
    assert limit_price_refusal("buy", 110.0, 100.0, rails)
    assert limit_price_refusal("sell", 110.0, 100.0, rails) is None
    assert limit_price_refusal("buy", 90.0, 100.0, rails) is None
