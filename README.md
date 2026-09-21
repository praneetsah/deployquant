# DQengine

[DeployQuant](https://deployquant.com) is a hosted trading platform where you can
run several strategies at once on one broker account, each with its own slice
of the account.

DQengine is the engine underneath it, with the source published. It is the same
code, not a cut-down copy. On its own it does backtesting today, and live trading
of one strategy per broker account is coming in version 0.2.

It is the fastest event-driven backtester we know of for US stocks on bars. In
our benchmarks it was about 1.7 times faster than LEAN and NautilusTrader and
about 7 times faster than backtrader on the same strategy, and it used the least
memory of the four. The example in the quick start below is a 5.5 year
minute-bar backtest, and it takes about 2 seconds and 70 MB of memory on a
laptop.

It is written in Python and installs with pip. There is no Docker image and
nothing else to set up. You write a strategy as a Python class and backtest it
at second, minute or daily resolution.

It covers US stocks and ETFs for now. More asset classes and features are
planned, and they are listed further down.

## Quick start

```bash
pip install deployquant

# free Alpaca keys (a paper trading account is enough)
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...

dqengine example tqqq_weekly                 # writes tqqq_weekly.py into this folder
dqengine data fetch TQQQ --from 2021-01-04   # 5 years of minute bars, takes a few minutes
dqengine backtest tqqq_weekly.py
```

The package installs as `deployquant`. The module and the command are
`dqengine`.

The data download is about 1,400 daily files and can take a few minutes,
depending on your internet connection. It only has to run once. After that,
`fetch` downloads just the days you are missing. The backtest then prints:

```
Window        2021-01-04 → 2026-06-09  (1364 sessions)
Start equity  $1,000.00
End equity    $13,603.38
CAGR          +61.78%
Max drawdown  37.90%
Fills         520  (597 orders)
Ran in        1.90s
```

## Benchmarks

DQengine was the fastest engine in every test we ran. This is how it compares
with three other engines: LEAN, the most widely used open-source trading engine,
NautilusTrader 1.231, which has a Rust core, and backtrader 1.9.78, the long
established Python backtester. These runs use separate test strategies, not the
quick start example.

All four engines ran the same strategy on the same data: SPY minute bars,
2021-01-04 to 2026-06-09, about 530,000 bars, on an Apple M5 Mac with 32 GB.
DQengine and LEAN ran the same strategy file. NautilusTrader ran the `EMACross`
example that ships with it, which has the same logic, with its per-bar logging
turned off. backtrader ran the same logic written for its API.

Three strategies were used: one that does nothing, a moving-average crossover
that trades 66 times, and the same crossover with short averages that trades
22,796 times. The scripts are in [`tools/bench_engines/`](https://github.com/praneetsah/deployquant/tree/main/tools/bench_engines).

**Speed.** Time to run the backtest, mean of 3 to 5 runs. DQengine's and LEAN's
times include reading the bars from disk. NautilusTrader's and backtrader's do
not.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Strategy that does nothing | **1.80 s** | 3.35 s | 4.33 s | 18.15 s |
| Crossover, 66 trades | **2.66 s** | 4.59 s | 4.54 s | 19.37 s |
| Crossover, 22,796 trades | **2.66 s** | 5.42 s | DNF in 10 minutes | 21.42 s |
| Whole process, start to finish (66 trades) | **2.70 s** | 10.98 s | 6.57 s | 20.56 s |
| Time per bar (66 trades) | **5.0 µs** | 8.7 µs | 8.6 µs | 36.5 µs |
| Bars per second (66 trades) | **199,000** | 115,000 | 117,000 | 27,000 |
| CPU cores used | **1** | 2 to 3.5 | **1** | **1** |

**Memory.** Peak, while running.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Strategy that does nothing | **69 MB** | 454 to 474 MB | 498 MB | 245 MB |
| Crossover, 66 trades | **70 MB** | 471 to 526 MB | 498 MB | 275 MB |
| Crossover, 22,796 trades | **102 MB** | 605 to 674 MB | over 600 MB | 374 to 412 MB |

**Startup and install.**

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Startup before any work | **0.05 to 0.07 s** | 5.5 to 8.5 s (container start) | 0.67 to 0.76 s | 0.08 to 0.09 s |
| Download size | **0.22 MB** | 5.1 GB Docker image | 156 MB | 0.42 MB |
| Installed size, with dependencies | 125 MB | 5.1 GB | 711 MB | **15 MB** |
| Needs Docker | **No** | Yes | **No** | **No** |

**Do the engines agree?** They should, since it is the same strategy on the same
bars. Fees are set to zero on all four. On the 22,796-trade strategy DQengine
and LEAN place the same orders and end $6 apart. LEAN calls the strategy 9 more
times because 9 minutes have no trades in the data, and LEAN fills each gap with
a copy of the previous bar.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Bars in the data given to the engine | 530,151 | 530,151 | 530,151 | 530,151 |
| Times the strategy was called | 530,151 | 530,160 | 530,151 | 530,151 |
| 66-trade crossover, position changes | 66 | 66 | 66 (132 fills, a flip is a close and an open) | 66 |
| 66-trade crossover, ending balance | $1,018,802 | $1,018,808 | $1,018,812 | $1,018,808 |
| 22,796-trade crossover, orders | 22,796 | 22,796 | DNF in 10 minutes | 22,796 |
| 22,796-trade crossover, ending balance | $991,113 | $991,119 | DNF in 10 minutes | $991,978 |

How each number was measured, and the limits of this test, are written up in
[BENCHMARK.md](https://github.com/praneetsah/deployquant/blob/main/BENCHMARK.md).

## What DQengine does

- Backtests Python strategies on US equities and ETFs.
- Second, minute and daily resolution.
- Market, limit, stop, stop-limit, trailing stop, limit-if-touched,
  market-on-open and market-on-close orders, with updates and cancels.
- Scheduled events, consolidators, warm-up, `history()`.
- 70 indicators, each tested against independent reference values.
- Leverage caps with buying-power rejection. Constant fee and slippage models.
- Downloads historical minute bars from Alpaca, which is free.
- Both `snake_case` and `PascalCase` method names work (`set_holdings` or
  `SetHoldings`).
- A real-time engine is included. It stays warm between bars, fires scheduled
  rules on the clock from a live quote instead of waiting for the next candle,
  and builds one-second bars from trades the same way live as in a backtest.
- Broker adapters for Alpaca (included), Webull and Charles Schwab (plugins).

Live trading from the command line (`dqengine live`) is not in this release.
The order handling code exists and runs real accounts today, but it still lives
in a private codebase and is being moved here. That will be version 0.2.

## What DQengine does not do yet

- Options, futures, forex and crypto
- Tick and hourly data
- Dynamic universe selection
- The framework modules (alpha models, portfolio construction, execution and
  risk models)
- Fundamentals and custom data
- Combo orders
- Parameter optimization

All of these are planned for later versions. If you need one of them, please
[open an issue](https://github.com/praneetsah/deployquant/issues). Requests are
how the order gets decided.

Until a feature is supported, using it raises `UnsupportedApiError` with the
name of the feature. It does not get skipped quietly.

## Writing your own strategy

You need Python 3.11 or newer, the package, and the free Alpaca keys from the
quick start. A paper trading account is enough for the keys. You do not need to
fund it.

First download bars for the symbols your strategy trades:

```bash
dqengine data fetch SPY QQQ --from 2019-01-01
```

Bars are saved under `./data` as one zip per symbol per day
(`equity/usa/minute/<symbol>/<yyyymmdd>_trade.zip`). Running `fetch` again only
downloads missing days.

Then write the strategy. It is a class that extends `QCAlgorithm`. `initialize` sets it up:
dates, cash, symbols, indicators and schedules. The trading happens in
`on_data`, which is called on every bar, or in functions you schedule. This one
holds SPY while its 50-day average is above its 200-day average:

```python
from AlgorithmImports import *

class SmaTrend(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2020, 1, 2)
        self.set_end_date(2025, 12, 31)
        self.set_cash(10_000)
        self.spy = self.add_equity("SPY", Resolution.MINUTE).symbol
        self.fast = self.sma(self.spy, 50, Resolution.DAILY)
        self.slow = self.sma(self.spy, 200, Resolution.DAILY)
        self.set_warm_up(200, Resolution.DAILY)
        self.schedule.on(self.date_rules.every_day(self.spy),
                         self.time_rules.after_market_open(self.spy, 5),
                         self.rebalance)

    def rebalance(self):
        if self.is_warming_up or not self.slow.is_ready:
            return
        long = self.fast.current.value > self.slow.current.value
        if long and not self.portfolio[self.spy].invested:
            self.set_holdings(self.spy, 1.0)
        elif not long and self.portfolio[self.spy].invested:
            self.liquidate(self.spy)
```

Save it as `sma_trend.py` and run it:

```bash
dqengine backtest sma_trend.py
dqengine backtest sma_trend.py --from 2023-01-01 --cash 25000 --json result.json
```

`--json` writes the fills, orders, daily equity and statistics to a file.

You can also run a backtest from Python:

```python
from dqengine.runtime import run_python_backtest

result = run_python_backtest(open("sma_trend.py").read(), data_root="./data")
print(result["stats"], len(result["fills"]))
```

Three example strategies come with the package: `sma_trend`, `rsi_dip` and
`tqqq_weekly`. `dqengine example` lists them, and `dqengine example NAME` writes
one into the current folder. The source is in
[`dqengine/examples/`](https://github.com/praneetsah/deployquant/tree/main/dqengine/examples). They are examples, not
recommendations.

## Settings

| Variable | What it does | Default |
|---|---|---|
| `DQENGINE_DATA_ROOT` | Where bars are stored. `--data` overrides it | `./data` |
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Alpaca keys for `data fetch` and the Alpaca adapter | none |
| `DQENGINE_FAST_PATH` | Set to `0` to turn off a speed optimization for quiet bars. Results are the same either way | `1` |
| `DQENGINE_WARM_IDLE_S`, `DQENGINE_SERVE_IDLE_S` | Sandbox only. How long idle workers stay up | `1800`, `14400` |

## Market data

Backtest history is free. Alpaca's free tier serves full-market (SIP) minute
history, and `data fetch` uses that.

Second-resolution backtests work if you have second bars in the store.
`data fetch` only downloads minute bars for now. Second bars are coming.

For live trading in 0.2: Alpaca's free real-time feed is IEX only, which is
roughly 2 to 3 percent of volume. That is fine for liquid ETFs and large caps.
A thinly traded symbol can go minutes without a print. Full-market real-time
data is a paid Alpaca plan.

You can also use your own data. A feed is a class with one method,
`fetch_days(symbol, start, end)`. See `dqengine/feed.py`.

## Brokers

Broker adapters are how the engine talks to a brokerage. Alpaca's comes with the
package. The others install separately:

```bash
dqengine brokers                    # lists installed adapters
pip install deployquant-webull      # adds webull
pip install deployquant-schwab      # adds schwab
```

| Adapter | Package | Notes |
|---|---|---|
| `alpaca`, `alpaca-paper` | included | Uses the same free keys. Easiest way to try things |
| `webull` | [`plugins/dqengine-webull`](https://github.com/praneetsah/deployquant/tree/main/plugins/dqengine-webull) | The one used with real money so far |
| `schwab` | [`plugins/dqengine-schwab`](https://github.com/praneetsah/deployquant/tree/main/plugins/dqengine-schwab) | Needs your own approved developer app. The refresh token expires every week |

To add a broker, implement `dqengine.adapters.base.BrokerAdapter`, register it
as an entry point in the `dqengine.brokers` group, and run
`tests/test_adapter_conformance.py` against it.

## If you use LEAN

DQengine is LEAN-compatible. It understands the same algorithm API
(`QCAlgorithm`, `from AlgorithmImports import *`, the same method names in
`snake_case` or `PascalCase`), so a strategy you wrote for LEAN runs here
without changes. It also reads the same data folder layout, so you can point
`--data` at a LEAN data folder you already have.

The quick start example was run on both engines with the same file
and the same bars. LEAN gives 597 orders and an ending equity of $13,603.38,
the same as DQengine. All 520 fills match on day, minute, quantity and price.
That comparison is a test in this repo
(`tests/runtime/test_acceptance_tqqq_weekly.py`), and those are the numbers you
get from a fresh data download, so you can check it yourself. The 70 indicators
are tested against values recorded from LEAN.

Daily-resolution strategies follow LEAN in two more places. A scheduled event
runs at its own clock time on the previous session's close, not on the bar that
arrives at the close. A market order it places becomes market-on-close, or
market-on-open once the session is within 15.5 minutes of closing, which is
what LEAN does with it. Both are tested against recordings from LEAN in
`tests/runtime/fixtures/lean_daily/`.

LEAN covers more today. The plan is to bring DQengine to parity with it, and
then past it. Open an issue for anything you want sooner. Feature by feature:

| | DQengine | LEAN |
|---|---|---|
| Written in | Python, with numpy and pandas | C#. Python algorithms run through a .NET to Python bridge |
| Install | `pip install deployquant` | Docker image and a CLI |
| Asset classes | US equities and ETFs. Other asset classes are coming | Equities, options, futures, forex, crypto, CFDs, indices |
| Resolutions | Second, minute, daily. Tick and hour are coming | Tick, second, minute, hour, daily |
| Universe | Any US-listed stock or ETF, added by ticker with `add_equity()`. The list is set in the algorithm. Dynamic selection is coming | Any supported asset, plus dynamic selection (coarse and fine filters, ETF constituents) |
| How you structure a strategy | Logic in the algorithm (`initialize`, `on_data`, scheduled events). The separate framework modules are coming | Logic in the algorithm, or the optional framework of separate alpha, portfolio construction, execution and risk modules |
| Order types | Market, limit, stop, stop-limit, trailing, MOO, MOC, limit-if-touched. Combo orders are coming soon | Market, limit, stop, stop-limit, trailing, MOO, MOC, limit-if-touched, combos, option exercise |
| Indicators | 70. More are coming | 100+ |
| Scheduled events, consolidators, warm-up, history | Yes | Yes |
| Fee, slippage and margin models | Constant fee and slippage, leverage cap. Per-brokerage models are coming | Many, per brokerage |
| Fundamentals and custom data | Coming | Yes |
| Research and strategy development | On [DeployQuant](https://deployquant.com): an AI builder that writes the strategy from a plain English description, and a visual block builder that converts to and from Python. Notebooks and a parameter optimizer are coming | Jupyter research notebooks and a parameter optimizer |
| Live trading | Engine yes, `dqengine live` command in 0.2 | Yes |
| Brokers | Alpaca, Webull, Charles Schwab. More are coming, and you can add your own | Many |
| Hosted platform | [DeployQuant](https://deployquant.com): hosted backtests and live trading, with the AI and block builders above | A paid cloud service for backtests and live trading |

Speed and memory against LEAN are in the Benchmarks section near the top, and
there is a longer write-up in [BENCHMARK.md](https://github.com/praneetsah/deployquant/blob/main/BENCHMARK.md).

LEAN is a trademark of its owner. DQengine is not affiliated with or endorsed
by them.

## Code layout

| Package | What is in it |
|---|---|
| `dqengine.runtime` | The engine: algorithm API, indicators, orders and fills, market calendar, backtester, warm engine |
| `dqengine.codegen` | Turns a JSON block strategy into a Python algorithm |
| `dqengine.feed`, `dqengine.store` | Bar downloads and the on-disk bar store |
| `dqengine.adapters`, `dqengine.brokers` | Broker adapter interface and plugin loading |
| `dqengine.live` | Order book mirror, broker capabilities, determinism check, second-bar builder |
| `dqengine.sandbox` | Runs untrusted algorithm code in a locked-down container |

[DeployQuant](https://deployquant.com) installs this package as it is and adds
the hosted parts on top: an AI strategy builder, a visual block builder, user
accounts, and running several strategies on one broker account.

## Tests

```bash
pip install -e ".[sandbox]" pytest httpx
python -m pytest tests -q
```

Most tests use synthetic data and run anywhere. The tests that check exact
dollar results need one specific set of bars. Market data cannot be
redistributed, so those tests skip if you do not have that set. They run
upstream on every change.

## Risk

This software can place real orders. It comes with no warranty. Nothing here is
financial advice. Backtests do not predict future results, and bugs, bad data,
outages and broker errors can lose you money. You are responsible for every
order it sends. Paper trade first. See [DISCLAIMER.md](https://github.com/praneetsah/deployquant/blob/main/DISCLAIMER.md).

## License

[PolyForm Shield 1.0.0](https://github.com/praneetsah/deployquant/blob/main/LICENSE). You can use, change and run it for anything,
including trading your own money, except building a product or service that
competes with DQengine or with the products built on it. The license does not
expire or convert to something else later. Some parts are under Apache-2.0,
see `NOTICE` and `LICENSES/Apache-2.0.txt`.

Contributions are welcome. Please read [CONTRIBUTING.md](https://github.com/praneetsah/deployquant/blob/main/CONTRIBUTING.md) first.
Report security problems privately, see [SECURITY.md](https://github.com/praneetsah/deployquant/blob/main/SECURITY.md).
