"""The supported API surface, introspected from the runtime itself.

This is what the AI porting loop is taught: a mechanically true list of
every public member user code may touch. It is generated from the live
classes — never hand-maintained — so it cannot drift from reality the
way a prose doc can. Members that exist only to fail loudly (the
_UNSUPPORTED roster) are excluded. PascalCase aliases exist for
everything listed but are not repeated.
"""
import inspect

from .algorithm import _UNSUPPORTED, QCAlgorithm
from .bars import Bars, Slice, TradeBar
from .enums import (OrderDirection, OrderStatus, OrderType, Resolution,
                    UpdateOrderFields)
from .orders import OrderTicket, Transactions
from .portfolio_view import Holding, PortfolioManager, PortfolioTarget
from .scheduling import DateRules, Schedule, TimeRules
from .symbol import Security


def _members(cls, exclude=()) -> list[str]:
    out = []
    for name in dir(cls):
        if name.startswith("_") or name in exclude:
            continue
        if name[0].isupper():
            continue                       # PascalCase aliases: implied
        attr = inspect.getattr_static(cls, name, None)
        if attr is None:
            continue
        if isinstance(attr, property):
            out.append(name)
        elif callable(attr):
            try:
                sig = str(inspect.signature(attr)).replace("self, ", "").replace("(self)", "()")
            except (TypeError, ValueError):
                sig = "(...)"
            out.append(f"{name}{sig}")
        else:
            out.append(name)
    return sorted(out)


def _enum_values(cls) -> str:
    return ", ".join(m.name for m in cls)


def supported_surface() -> str:
    """Compact, complete member listing for the porting system prompt."""
    unsupported = set(_UNSUPPORTED)
    pascal_unsupported = {"".join(p.capitalize() for p in n.split("_"))
                          for n in unsupported}
    sections = [
        ("QCAlgorithm (your class inherits this; self.<member>)",
         _members(QCAlgorithm, exclude=unsupported | pascal_unsupported)),
        # instance attributes assigned in __init__ are invisible to class
        # introspection — pinned literally, with a test guarding the pin
        ("Security (self.securities[sym], or the add_equity return)",
         sorted(_members(Security)
                + ["symbol", "resolution", "price", "open", "high", "low",
                   "close", "volume", "invested", "leverage"])),
        ("Holding (self.portfolio[sym])", _members(Holding)),
        ("PortfolioManager (self.portfolio)", _members(PortfolioManager)),
        ("OrderTicket (returned by order methods)", _members(OrderTicket)),
        ("Transactions (self.transactions)", _members(Transactions)),
        ("Slice (the on_data argument; slice[sym] and `sym in slice` work)",
         sorted(_members(Slice) + ["bars", "time"])),
        ("Bars (slice.bars)", _members(Bars)),
        ("TradeBar (slice.bars[sym])",
         sorted(set(_members(TradeBar))
                | {"symbol", "time", "end_time", "open", "high", "low",
                   "close", "volume", "price"})),
        ("Schedule (self.schedule)", _members(Schedule)),
        ("DateRules (self.date_rules)", _members(DateRules)),
        ("TimeRules (self.time_rules)", _members(TimeRules)),
        ("PortfolioTarget", _members(PortfolioTarget)),
        ("UpdateOrderFields", _members(UpdateOrderFields)),
    ]
    lines = ["EXACT SUPPORTED SURFACE (anything not listed raises "
             "AttributeError — re-express it with what IS listed):"]
    for title, members in sections:
        lines.append(f"- {title}: {', '.join(members)}")
    lines.append(f"- Resolution: {_enum_values(Resolution)} (no HOUR)")
    lines.append(f"- OrderStatus: {_enum_values(OrderStatus)}")
    lines.append(f"- OrderType: {_enum_values(OrderType)}")
    lines.append(f"- OrderDirection: {_enum_values(OrderDirection)}")
    lines.append("- Indicators (self.sma/ema/rsi/std/max/min/atr): "
                 "is_ready, current.value; default resolution DAILY")
    lines.append("- NOT AVAILABLE (fail loudly): " + ", ".join(sorted(_UNSUPPORTED))
                 + ", plot, network access (yfinance/requests), matplotlib "
                   "output, cash_book, universe selection, options/futures/"
                   "crypto/forex")
    return "\n".join(lines)
