"""Broker-execution ledger, engine side.

The engine replays a strategy to decide what it WANTS. When a ledger is
present the engine no longer invents the fill: it takes the broker's.

The three-valued return of `take()` is the whole point of this module:
  [rows]  the broker filled this, at these prices
  []      the broker affirmatively did NOT fill this
  None    UNKNOWN — no data (outside the reconciled window, or the broker
          is unreachable). The caller keeps the model fill and marks it
          unconfirmed.

Conflating None with [] is how an API outage becomes a duplicate position:
the sleeve concludes it never bought, the replay still wants the position,
and the executor buys it again. Keep the three cases distinct.

No DB imports here, and nothing from platform/api/ — this module has to
stay usable by the pure engine, which does not know the api package exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


def _norm_sym(s) -> str:
    """Symbol case is normalized ONCE, here. `normalize_execution` uppercases
    on the way in and `_unknown_symbols` uppercases the IR's symbols, but the
    engine hands `take()` the raw IR symbol — so a lowercase-universe IR
    would match no ledger row and every slot would read as a confirmed
    no-fill. That is this module's inversion, not a cosmetic mismatch."""
    return str(s).upper() if s is not None else ""


@dataclass
class LedgerFill:
    day: date
    time_ms: int
    symbol: str
    qty: int                 # signed
    price: float
    fees: float = 0.0
    rule_tag: Optional[str] = None
    # The broker's order id, carried so `take()` can tell "the rest of THIS
    # order" (a partial fill reported as several executions) apart from "a
    # different order in the same symbol today".
    broker_order_id: Optional[str] = None

    def __post_init__(self):
        self.symbol = _norm_sym(self.symbol)


class ExecutionLedger:
    def __init__(self, fills: list, unknown=frozenset(),
                 reconciled_from: Optional[date] = None,
                 unknown_from: Optional[date] = None):
        self._fills = sorted(fills, key=lambda f: (f.day, f.time_ms))
        self._taken: set[int] = set()
        self.unknown = {_norm_sym(s) for s in unknown}
        self.reconciled_from = reconciled_from
        # Unknown-ness is a statement about what we cannot SEE YET, so it
        # is day-scoped: absence demotes to None only for days >=
        # unknown_from (live passes today). Days that closed under a
        # healthy poll are settled — absence there really is no-fill.
        # None = the set applies to every day (pure-engine tests; the
        # pre-2026-08-25 behavior, whose history-wide reach flapped a live
        # sleeve's cost basis and churned real orders — see the lean-fills
        # spec §1).
        self.unknown_from = unknown_from

    def _absence_unknown(self, day: date, symbol: str) -> bool:
        if symbol not in self.unknown:
            return False
        return self.unknown_from is None or day >= self.unknown_from

    def take(self, day: date, symbol: str, rule_id=None, side=None):
        """`side` is the asking order's direction (+1 buy, -1 sell; None =
        any, for callers that have no order). A row is only ever given to an
        order on its own side. Without this, an order whose own execution
        never reached the ledger fell through to "any row for this symbol
        today" and a take-profit SELL was booked with the market BUY that
        followed it: +shares for a sell, and an executor trading against the
        result. Rows on the other side are simply not candidates, so a day
        that holds only those reads as absence, with absence's usual
        meaning (no fill, or unknown)."""
        symbol = _norm_sym(symbol)
        if self.reconciled_from is None or day < self.reconciled_from:
            return None
        avail = [(i, f) for i, f in enumerate(self._fills)
                 if i not in self._taken and f.day == day
                 and f.symbol == symbol
                 and (side is None or (f.qty > 0) == (side > 0))]
        if not avail:
            # PRESENCE WINS: only absence can be unknown. A captured row
            # never becomes less true because new data is hard to see, so
            # the unknown check lives here — after the row lookup — never
            # before it. (Checking first is how a routine poll hiccup hid
            # a confirmed prior-day fill and flapped a live cost basis.)
            return None if self._absence_unknown(day, symbol) else []
        # Rule-tagged rows are preferred, so an intended fill for this rule
        # is not stolen by an untagged row (Schwab, or a netted market
        # delta) that happens to sort earlier. Fall back to day+symbol
        # order — the engine's own fill order — when nothing is tagged.
        tagged = [(i, f) for i, f in avail if f.rule_tag == rule_id]
        pool = tagged or avail
        first = pool[0][1]
        # Spec §4 stores rows per EXECUTION so a partial fill survives as
        # two rows, and §6 has the engine consume matching rows (plural).
        # Group by order identity: every execution of the chosen row's order
        # is one fill event and applies together. Taking only the first would
        # leave the sleeve permanently short shares it really holds — and the
        # executor, seeing want < have, would sell the remainder.
        if first.broker_order_id:
            chosen = [(i, f) for i, f in pool
                      if f.broker_order_id == first.broker_order_id]
        elif first.rule_tag is not None:
            # No order id (an older row, or a broker that does not report
            # one): the rule tag is the only other order-ish identity we
            # have. Spec §6 precedence 2.
            chosen = [(i, f) for i, f in pool
                      if f.rule_tag == first.rule_tag
                      and not f.broker_order_id]
        else:
            # Nothing to group on. Take exactly one: over-consuming would
            # swallow a second rule's fill on the same day, which is as
            # wrong as under-consuming.
            chosen = pool[:1]
        for i, _ in chosen:
            self._taken.add(i)
        return [f for _, f in chosen]

    def take_rule_fill(self, day: date, symbol: str, rule_tag,
                       upto_ms: int):
        """The fill pump's read (lean-fills spec §3.3): broker fills of the
        PLACED order resting for `rule_tag`, visible by `upto_ms`.

        Returns rows or None — never []. The pump only ADOPTS fills that
        exist; "the broker did not fill" is not a conclusion it can reach
        (a resting order may fill any second). Tag match is strict — the
        pump must never steal untagged rows (manual trades, market
        deltas); those stay leftovers for the rail and the residual panel.

        `upto_ms` gates on the row's fill time so a batch replay of the
        whole day applies the fill at the same bar a live step did —
        warm-vs-batch equivalence depends on it. Consumption shares
        `_taken` with take(), so a pumped row can never be double-applied
        by the rule's own later bar-cross."""
        symbol = _norm_sym(symbol)
        if rule_tag is None:
            return None
        if self.reconciled_from is None or day < self.reconciled_from:
            return None
        avail = [(i, f) for i, f in enumerate(self._fills)
                 if i not in self._taken and f.day == day
                 and f.symbol == symbol and f.rule_tag == rule_tag
                 and f.time_ms < upto_ms]
        if not avail:
            return None
        first = avail[0][1]
        if first.broker_order_id:
            chosen = [(i, f) for i, f in avail
                      if f.broker_order_id == first.broker_order_id]
        else:
            # no order id to group on: same-tag no-id rows are one order's
            # executions (take()'s spec §6 precedence 2, mirrored)
            chosen = [(i, f) for i, f in avail if not f.broker_order_id]
        for i, _ in chosen:
            self._taken.add(i)
        return [f for _, f in chosen]

    def leftovers(self) -> list:
        return [f for i, f in enumerate(self._fills) if i not in self._taken]


class LiveCappedLedger:
    """A warm engine's view of the ledger: past days keep full batch
    semantics; on live days (>= `live_from`, the day the engine warmed) an
    ABSENT row is demoted from "confirmed no fill" to UNKNOWN.

    The engine's live step runs seconds BEFORE the sweep submits the order
    it just signaled, so on a live day absence can never mean the broker
    declined — it means the round trip has not happened yet. Rows that DO
    exist are broker truth and apply normally; step order plus the shared
    `_taken` state keeps an earlier rule's fill from being stolen by a
    later one, exactly as in batch.

    `live_from` never moves. Post-warm days converge to broker truth via
    the stale-on-ingest rebuild (api side), not by mutating this object.
    """

    def __init__(self, inner: ExecutionLedger, live_from: date):
        self._inner = inner
        self.live_from = live_from

    def take(self, day: date, symbol: str, rule_id=None, side=None):
        real = self._inner.take(day, symbol, rule_id, side=side)
        if real == [] and day >= self.live_from:
            return None
        return real

    def __getattr__(self, name):
        return getattr(self._inner, name)
