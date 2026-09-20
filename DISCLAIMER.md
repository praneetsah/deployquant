# Disclaimer

DQengine is software for researching and automating trading strategies. Please
read this before you connect it to a brokerage account.

## No warranty

The software is provided "as is", without warranty of any kind, as set out in
the [licence](LICENSE). The authors and contributors are not liable for any loss
or damage that comes from using it.

## Not financial advice

Nothing in this repository is investment, legal or tax advice, or a
recommendation to buy or sell any security. That covers the code, the examples,
the documentation, the benchmark results and any backtest. The example
strategies are there to show and test the engine. They are not recommendations,
and their past results do not predict future results.

## Backtests differ from live trading

A backtest assumes fills, prices, liquidity and market data that live trading
does not guarantee. Slippage, partial fills, trading halts, corporate actions,
data errors, delays and broker behaviour all differ from the model. A strategy
that makes money in a backtest can lose money live.

## You can lose money

You can lose more than you deposited. Leveraged and inverse products, margin and
automated order placement make losses larger. Software bugs, network failures,
stale or missing market data, expired credentials, broker outages and rejected
or duplicated orders can all cause losses. The safety checks in this software do
not remove that risk.

## You are responsible for your orders

When you run this software against a brokerage account, every order it sends is
your order. You are responsible for supervising it, for how you configure it,
for following your broker's terms and the laws and regulations that apply to
you, and for any tax that results.

## Paper trade first

Run a strategy against a paper account and check that it behaves the way you
expect before you risk money. Start small when you do.

DQengine is not affiliated with, endorsed by or sponsored by any broker,
exchange or data vendor named in this repository. All trademarks belong to their
owners.
