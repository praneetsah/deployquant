"""Run an IR weight tree inside a QCAlgorithm.

This is what lets a block ALLOCATION strategy (`set_weights`) run on the
python engine's event loop, which is the last thing standing between blocks
and one engine.
"""
from __future__ import annotations

from datetime import date

from .blocks import WARMUP_SESSIONS, BlockContext   # noqa: F401 — re-export
from .enums import OrderStatus

__all__ = ["Allocator", "WARMUP_SESSIONS"]


class Allocator:
    """Weight-tree evaluation + rebalance for one QCAlgorithm.

    A thin client of BlockContext since the one-engine cleanup: the
    indicator/weight state, the warm-up and the EvalContext all live there,
    so an allocation rule and a per-symbol rule in the same strategy share
    ONE indicator engine exactly as they do inside the IR engine.

    The four positional arguments are FROZEN: ejected forks saved in the
    database call `Allocator(self, WEIGHTS, PARAMS, SYMS)` and those files
    must keep running unchanged. `ctx` is the new, optional fifth.

        self._alloc = Allocator(self, WEIGHTS, PARAMS, SYMS, ctx=self._bc)
        # then, once the run is under way:
        self._alloc.close_session(day, bars)     # each session close
        self._alloc.rebalance(day, tag)          # on the rebalance trigger
    """

    def __init__(self, algo, weights_node: dict, params: dict, symbols,
                 ctx: BlockContext | None = None):
        self.algo = algo
        self.node = weights_node
        self.ctx = ctx if ctx is not None else BlockContext(
            algo, params, symbols, warm=True, track_ohlc=True)
        self.params = self.ctx._params
        self.symbols = self.ctx.symbols
        self.ind = self.ctx.ind
        self.weights = self.ctx.weights

    # ------------------------------------------------------------ feeding

    def warm(self) -> None:
        """Forwards to the context, which owns the warm-up depth.

        No `sessions` argument: the depth is BlockContext's (260 on the
        multi path, none on the single-symbol one, matching the IR engine's
        own dispatch), and an argument that silently changed nothing is
        worse than one that is not there. Every generator that ever emitted
        a call emitted `warm()`, so ejected forks keep working.
        """
        self.ctx.warm()

    def ensure_warm(self) -> None:
        self.ctx.ensure_warm()

    def close_session(self, day: date, closes: dict) -> None:
        """One session's closes, {sym: (close, high, low)}.

        The dict argument is the LEGACY contract, still used by every
        already-ejected file: it pushes the generated on_data accumulator
        into the context, which then applies the IR engine's carry-forward
        for anything missing."""
        self.ctx._day = dict(closes or {})
        self.ctx.close_session(day)

    def mark_prices(self, prices: dict) -> None:
        self.ctx.ensure_warm()
        for sym, px in (prices or {}).items():
            if px:
                self.ctx.ind.last_price[str(sym).upper()] = float(px)

    # --------------------------------------------------------- evaluation

    def target_weights(self, day: date) -> dict:
        return self.ctx.target_weights(self.node, day)

    # -------------------------------------------------------- rebalancing

    def rebalance(self, day: date, tag: str = "rebalance",
                  on_flat=None) -> list:
        """Delta to the target vector — CANCEL, then SELLS, then buys.

        The arithmetic is Backtester._rebalance_to's, deliberately down to
        the int() truncation and the sort: a share count computed by
        rounding instead of truncating is a different order, and buying
        before selling can breach margin on a fully-invested rebalance.

        The cancel is not housekeeping. `_rebalance_to` deletes every
        managed target before it sizes, because a rebalance owns the book.
        Without it a protective sell limit rests under a position the
        rebalance just resized, and the two engines hold different shares
        the moment it fills. It was harmless while allocation strategies
        were pure (no rule could place a target); Task 5 ends that.

        `on_flat(sym)` is called for each symbol the rebalance took to
        zero — the IR engine releases that symbol's once_per=position
        guards there, and only there.
        """
        self.ctx.ensure_warm()
        algo = self.algo
        for t in list(algo.transactions.get_open_order_tickets()):
            t.cancel()
        weights = self.target_weights(day)
        # Iterate the SLEEVE's own quantity dict, not the universe list.
        # _rebalance_to walks `set(self.sleeve.qty) | set(weights)`, and
        # with equal weights several symbols get the same delta -- so the
        # sort ties, Python's stable sort keeps insertion order, and a
        # different iteration source submits the same orders in a different
        # sequence. Matters when buying power binds part-way through a
        # fully-invested rebalance.
        sleeve = algo.portfolio._sleeve
        prices = {}
        for sym in set(sleeve.qty) | set(weights):
            px = self.ctx.ind.last_price.get(sym)
            if px:
                prices[sym] = px

        equity = float(sleeve.equity(prices))
        want = {}
        for sym, w in weights.items():
            if abs(w) < 0.001 or sym not in prices:
                continue
            want[sym] = int(equity * w / prices[sym])

        # the same trace the IR engine journals (equity, marked prices,
        # wanted shares): a one-share difference between the engines is
        # read from the two, not guessed at
        algo.log("[rebalance] %s equity=%.2f want=%s prices=%s" % (
            day.isoformat(), equity,
            ",".join(f"{s}={q}" for s, q in sorted(want.items())),
            ",".join(f"{s}@{prices[s]:.4f}" for s in sorted(prices))))

        deltas = []
        for sym in set(sleeve.qty) | set(want):
            if sym not in prices:
                continue
            dq = want.get(sym, 0) - int(sleeve.qty.get(sym, 0))
            if dq != 0:
                deltas.append((sym, dq))

        placed = []
        for sym, dq in sorted(deltas, key=lambda x: x[1]):
            ticket = algo.market_order(sym, dq, tag=tag)
            placed.append(ticket)
            # `qty == 0` is only a "this position just closed" signal when a
            # fill was actually APPLIED — the IR engine gates the identical
            # test on `if self._fill(...)` (_rebalance_to) for this reason:
            # on a confirmed no-fill of a BUY delta from flat the quantity
            # is still 0, and firing on_flat there would release the
            # once_per=position guards of a rebalance that never happened,
            # letting the rule re-enter with real shares. Reachable on a
            # live replay whose broker ledger confirms a no-fill (ticket
            # CANCELED) and on a buying-power rejection (INVALID).
            if (on_flat is not None
                    and ticket.status == OrderStatus.FILLED
                    and sleeve.qty.get(sym, 0) == 0):
                on_flat(sym)
        return placed
