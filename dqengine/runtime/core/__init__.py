"""The shared engine library: bars and calendar, order fills, the sleeve,
the expression evaluator, indicators, weight trees, the broker ledger, plus
the IR-document and statistics helpers.

Named `core` rather than re-homed under the top level because it IS
dqengine.runtime -- the runtime imports all of it. Keeping it a subpackage is also
what kept Phase 3 from touching a single Dockerfile: all three already copy
platform/engine/dqengine.runtime, so dropping their COPY line for the old package
was the whole deployment change.

Import the modules by name (`dqengine.runtime.core.data` for `DayBars`); only the
five helpers are re-exported here, because they are functions rather than
modules and had no natural module of their own.
"""
from .irdoc import (collect_ir_symbols, collect_tradeable_symbols,
                    expand_metrics)
from .stats import flow_adjusted_returns, stats_from_equity

__all__ = ["collect_ir_symbols", "collect_tradeable_symbols",
           "expand_metrics", "flow_adjusted_returns", "stats_from_equity"]
