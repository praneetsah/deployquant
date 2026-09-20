"""LEAN-shaped views over the dqengine.runtime.core Sleeve. Read-only: all mutation
goes through the OrderBook."""
from .aliases import PascalMixin, alias_methods


class PortfolioTarget(PascalMixin):
    """set_holdings(list) element: PortfolioTarget(symbol, 0.25) = 25% of
    portfolio value, LEAN's percent convention."""

    def __init__(self, symbol, quantity):
        self.symbol = str(symbol).upper()
        self.quantity = float(quantity)


@alias_methods
class Holding(PascalMixin):
    def __init__(self, sleeve, prices, symbol: str):
        self._sleeve = sleeve
        self._prices = prices
        self._symbol = symbol

    @property
    def symbol(self):
        return self._symbol

    @property
    def quantity(self) -> int:
        return self._sleeve.qty.get(self._symbol, 0)

    @property
    def invested(self) -> bool:
        return self.quantity != 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def absolute_quantity(self) -> int:
        return abs(self.quantity)

    @property
    def average_price(self) -> float:
        return self._sleeve.avg_cost.get(self._symbol, 0.0)

    @property
    def price(self) -> float:
        return self._prices.get(self._symbol, 0.0)

    @property
    def holdings_value(self) -> float:
        return self.quantity * self.price

    @property
    def absolute_holdings_value(self) -> float:
        return abs(self.holdings_value)

    @property
    def holdings_cost(self) -> float:
        return self.quantity * self.average_price

    @property
    def absolute_holdings_cost(self) -> float:
        return abs(self.holdings_cost)

    @property
    def unrealized_profit(self) -> float:
        return self.quantity * (self.price - self.average_price)

    @property
    def unrealized_profit_percent(self) -> float:
        # LEAN: UnrealizedProfit / AbsoluteHoldingsCost — the sign works
        # for shorts too (price/avg - 1 only held for longs)
        cost = self.absolute_holdings_cost
        return self.unrealized_profit / cost if cost else 0.0


@alias_methods
class PortfolioManager(PascalMixin):
    def __init__(self, sleeve, prices: dict):
        self._sleeve = sleeve
        self._prices = prices

    @property
    def total_portfolio_value(self) -> float:
        return self._sleeve.equity(self._prices)

    @property
    def cash(self) -> float:
        return self._sleeve.cash

    @property
    def margin_remaining(self) -> float:
        return self._sleeve.buying_power(self._prices)

    @property
    def invested(self) -> bool:
        return any(q != 0 for q in self._sleeve.qty.values())

    @property
    def total_holdings_value(self) -> float:
        return sum(q * self._prices.get(s, 0.0)
                   for s, q in self._sleeve.qty.items())

    @property
    def total_unrealized_profit(self) -> float:
        return sum(q * (self._prices.get(s, 0.0)
                        - self._sleeve.avg_cost.get(s, 0.0))
                   for s, q in self._sleeve.qty.items())

    def __getitem__(self, symbol) -> Holding:
        return Holding(self._sleeve, self._prices, str(symbol).upper())

    def __contains__(self, symbol) -> bool:
        return self._sleeve.qty.get(str(symbol).upper(), 0) != 0

    def values(self):
        return [Holding(self._sleeve, self._prices, s)
                for s in self._sleeve.qty]

    def items(self):
        return [(s, Holding(self._sleeve, self._prices, s))
                for s in self._sleeve.qty]

    @property
    def keys(self):
        return list(self._sleeve.qty)
