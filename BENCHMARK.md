# Benchmarks

This file has the details behind the numbers in the README: what was run, how
each number was measured, what the tests do not show, and how to run them
yourself.

There are two tests.

1. A four-engine benchmark. DQengine, LEAN, NautilusTrader and backtrader run
   the same simple strategies on the same SPY minute bars.
2. A fill-by-fill comparison with LEAN. Both engines run a more involved
   strategy (resting limit orders, margin, scheduled events), and every fill is
   compared.

All runs were on one machine: an Apple M5 Mac with 32 GB of memory, Docker
limited to 7.75 GiB.

## 1. Four engines, same strategy

### What was run

| | |
|---|---|
| Data | SPY minute bars, 2021-01-04 to 2026-06-09. 1,364 sessions, 530,151 regular-session bars |
| Engines | DQengine, LEAN (`quantconnect/lean:latest` through the `lean` CLI), NautilusTrader 1.231, backtrader 1.9.78 |
| Strategies | One that does nothing. A moving-average crossover (EMA 2000 and 8000) that changes position 66 times. The same crossover with short averages (EMA 10 and 20) that trades 22,796 times |
| Position size | 100 shares, long when the fast average is at or above the slow one, short otherwise |
| Trials | 5 per engine for DQengine and NautilusTrader, 3 to 5 for LEAN, 3 for backtrader, a new process each time |

DQengine and LEAN ran the same strategy files, unchanged. NautilusTrader ran the
`EMACross` example that ships with it, which has the same logic. Its two
per-bar log calls were removed, and logging was switched off, so that logging is
not what gets timed. backtrader ran the same logic written for its API
(`run_backtrader.py`).

The strategies and one runner per engine are in
[`tools/bench_engines/`](tools/bench_engines).

### Speed

Mean of the trials, with the range in brackets.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Strategy that does nothing | **1.80 s** (1.75 to 2.00) | 3.35 s (3.28 to 3.40) | 4.33 s (4.24 to 4.57) | 18.15 s (17.97 to 18.38) |
| Crossover, 66 trades | **2.66 s** (2.61 to 2.75) | 4.59 s (4.38 to 4.92) | 4.54 s (4.52 to 4.55) | 19.37 s (19.24 to 19.44) |
| Crossover, 22,796 trades | **2.66 s** (2.63 to 2.72) | 5.42 s (4.89 to 6.07) | DNF in 10 minutes | 21.42 s (21.03 to 22.07) |
| Whole process, start to finish (66 trades) | **2.70 s** | 10.98 s | 6.57 s | 20.56 s |
| Time per bar (66 trades) | **5.0 µs** | 8.7 µs | 8.6 µs | 36.5 µs |
| Bars per second (66 trades) | **199,000** | 115,000 | 117,000 | 27,000 |
| CPU cores used | **1** | 2 to 3.5 | **1** | **1** |

What each time covers:

- DQengine: the whole backtest call. This includes 0.33 s of reading the 1,364
  bar files.
- LEAN: the compute time LEAN reports itself. This includes reading its data. It
  leaves out starting the Docker container, which is in the "whole process" row.
- NautilusTrader: its own timer around `engine.run()`, with the bars already
  loaded in memory, so it excludes reading data.
- backtrader: the time of `cerebro.run()`, with the bars already loaded into a
  pandas frame, so it also excludes reading data.

LEAN uses 2 to 3.5 cores to reach its times. The other three use one.

### Memory

Peak, while running.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Strategy that does nothing | **69 MB** | 454 to 474 MB | 498 MB | 245 MB |
| Crossover, 66 trades | **70 MB** | 471 to 526 MB | 498 MB | 275 MB |
| Crossover, 22,796 trades | **102 MB** | 605 to 674 MB | over 600 MB | 374 to 412 MB |

LEAN's number is its whole container, sampled with `docker stats`, so it includes
the .NET runtime. NautilusTrader was given all the bars at once. It can also
stream data in chunks, which would use less memory.

### Startup and install

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Startup before any work | **0.05 to 0.07 s** | 5.5 to 8.5 s (container start) | 0.67 to 0.76 s | 0.08 to 0.09 s |
| Download size | **0.22 MB** | 5.1 GB Docker image | 156 MB | 0.42 MB |
| Installed size, with dependencies | 125 MB | 5.1 GB | 711 MB | **15 MB** |
| Needs Docker | **No** | Yes | **No** | **No** |

### Do the engines agree?

They should, since it is the same strategy on the same bars. Fees are set to
zero on all four, because LEAN charges about $1 per order by default.

| | DQengine | LEAN | NautilusTrader | backtrader |
|---|---|---|---|---|
| Bars in the data given to the engine | 530,151 | 530,151 | 530,151 | 530,151 |
| Times the strategy was called | 530,151 | 530,160 | 530,151 | 530,151 |
| 66-trade crossover, position changes | 66 | 66 | 66 (132 fills, a flip is a close and an open) | 66 |
| 66-trade crossover, ending balance | $1,018,802 | $1,018,808 | $1,018,812 | $1,018,808 |
| 22,796-trade crossover, orders | 22,796 | 22,796 | DNF in 10 minutes | 22,796 |
| 22,796-trade crossover, ending balance | $991,113 | $991,119 | DNF in 10 minutes | $991,978 |

On the 66-trade strategy the four engines make the same 66 position changes and
end within $10 of each other on a million dollars.

On the 22,796-trade strategy DQengine and LEAN place the same 22,796 orders and
end $6 apart. 22,768 of the fills are identical in day, minute, quantity and
price. The other 28 are on two days. On 2023-08-03 LEAN's prices are 3.8% above
the bar file all day, which accounts for 26 fills. On 2023-06-05 LEAN fills four
minutes that have no trades with copies of the previous bar, so an average
crosses three minutes sooner, which accounts for 2 fills. backtrader ends $865
away because it fills a market order at the next bar's open, where LEAN and
DQengine fill it at the current bar's close.

LEAN calls the strategy 9 more times than the others. The data has no trades
in 9 minutes (5 on 2021-05-05 and 4 on 2023-06-05). LEAN fills each gap with a
copy of the previous bar and calls the strategy anyway. The other three move on
to the next real bar. It made no difference to the orders
here, but a strategy that counts bars could see it.

### Why DQengine takes about half the time per bar

DQengine does the same work per bar as the others. A profile of the 66-trade
run shows 530,151 bar steps, 530,151 calls to the strategy, 1,060,302 indicator
updates and one new bar object per bar. DQengine has a shortcut for bars where a
strategy has nothing to do, and it was off in every run because these strategies
look at every bar. The orders it produces are the same as LEAN's.

LEAN and NautilusTrader do more around each bar:

- LEAN is written in C#. A Python strategy runs through a bridge between .NET and
  Python, and every call crosses it in both directions. LEAN also runs a
  multi-threaded data feed built for thousands of symbols and many asset
  classes. That feed runs even for one symbol.
- NautilusTrader turns every bar into four price updates (open, high, low, close)
  and runs them through an exchange matching simulator, whether or not an order
  is resting. With that turned off its run drops from 4.5 s to 1.5 s, but then no
  order fills, so it has to stay on.
- DQengine checks orders only when some are open, and it stays in Python the
  whole time.

backtrader is also pure Python. It keeps every indicator and data line as its
own object with a long chain of calls per bar, and it spends about 34 µs on a
bar even when the strategy does nothing.

### What this test does not show

- It is one symbol on minute bars with simple strategies, on one machine. Many
  symbols, daily bars or second bars would give different ratios.
- LEAN and NautilusTrader were run in their default backtest setup. Someone who
  knows them well may get more out of them.
- NautilusTrader is built for tick and order book data across many instruments.
  This test does not cover that.
- NautilusTrader slowed down as closed trades piled up in the 22,796-trade run:
  1.1 s for 3 months of data, 3.3 s for 6 months, 12 s for 12 months, 28 s for 18
  months, and the full window was stopped after 10 minutes. It was fine on the
  66-trade run. This may be caused by a setting, not by the engine itself.

### Run it yourself

You need SPY minute bars for the window, the `lean` CLI with Docker for the LEAN
runs, `pip install nautilus_trader` for the NautilusTrader runs and
`pip install backtrader pandas` for the backtrader runs.

```bash
dqengine data fetch SPY --from 2021-01-04 --to 2026-06-09 --data ./data
cd tools/bench_engines

python run_dqengine.py ../../data ema_cross_slow.py
DQENGINE_FAST_PATH=0 python run_dqengine.py ../../data do_nothing.py

FAST=2000 SLOW=8000 python run_nautilus.py ../../data
NOOP=1 python run_nautilus.py ../../data

FAST=2000 SLOW=8000 python run_backtrader.py ../../data
NOOP=1 python run_backtrader.py ../../data

python run_lean.py ~/lean-workspace ema_cross_slow.py bench_ema_slow
```

Each runner prints one line with the time, memory, fills and ending balance.

## 2. Fill by fill against LEAN

### What was run

| | |
|---|---|
| Strategy | `dqengine/examples/tqqq_weekly.py`, the same file on both engines |
| Data | TQQQ minute bars, 2021-01-04 to 2026-06-09. 1,364 sessions, about 530,000 bars |
| Settings | $1,000 starting cash, leverage capped at 1.33, no fees, no slippage |
| LEAN | `quantconnect/lean:latest` through `lean backtest` |
| Trials | 3 per engine, a new process each time |

This strategy uses resting limit orders that are replaced every morning,
several scheduled events a day, market exits near the close, margin with a
leverage cap that rejects some orders, and a volatility measure it computes
itself. It is a harder test than the crossover above.

### Result

| | DQengine | LEAN |
|---|---|---|
| Ending equity | $13,603.38 | $13,603.38 |
| Orders | 597 | 597 |
| Fills | 520 | 520 |
| Orders rejected for buying power | 5 | 5 |

All 520 fills match on day, minute, quantity and price. These are also the
numbers you get from a free data download:

```bash
dqengine example tqqq_weekly
dqengine data fetch TQQQ --from 2021-01-04
dqengine backtest tqqq_weekly.py
```

Earlier versions of this page reported 519 fills and $13,925.39, which was also
identical on both engines. The bars used then were missing the regular session
on 2024-12-31 and 2025-12-31. Those two days were added on 2026-09-19 and both
engines were run again, and both moved to the numbers above. Everything before
2024-12-31 (404 fills and 1,004 daily equity values) stayed the same.

### Speed

| | DQengine | LEAN |
|---|---|---|
| Compute time reported by LEAN | n/a | 5.25 s (5.19 to 5.31) |
| Wall clock, start to finish | **1.92 s** (1.89 to 1.99) | 9.93 s (9.89 to 9.99) |
| Peak memory | **71 MB** | 499 MB |
| CPU | **1 core** | 2.75 cores |

Against LEAN's own compute time DQengine is about 2.7 times faster. Against
LEAN's wall clock, which includes starting Docker, it is about 5.2 times faster. These timings are from the run made before
the two missing days were added. The work differs by two trading days out of
1,364.

### Run it yourself

```bash
python tools/bench_vs_lean.py --trials 3       # needs the lean CLI and a LEAN workspace
LEAN_WORKSPACE=~/lean python tools/bench_vs_lean.py
python tools/bench_vs_lean.py --skip-lean      # DQengine only, checked against the recorded LEAN result
```

The script does not print a timing if the two engines end on different equity.
It exits with an error instead.

## Orders placed at the close

The 22,796-trade strategy places 75 of its orders on the last bar of a session.
By the time that bar is complete the exchange is closed. LEAN turns such a
market order into a market-on-open order and fills it at the next session's
first open, before the strategy sees that bar. DQengine does the same. The same
rule covers daily bars, which arrive at the close: a market order placed from a
daily bar fills at the next day's open. On SPY daily bars for 2025-03-03 and
2025-03-06, LEAN and DQengine both fill at 569.93 and 561.26, the next days'
opens.

`tests/runtime/test_close_bar_parity.py` pins the 22,796-trade result, and
`tests/runtime/test_order_types.py` covers the rule on small synthetic sessions.

## Tests that back this up

- `tests/runtime/test_acceptance_tqqq_weekly.py` runs the strategy above and
  checks every fill, every fill time and the whole daily equity curve against the
  recorded result, and the totals against LEAN's.
- `tests/runtime/test_indicator_library.py` checks the indicators against values
  recorded by running LEAN (`tools/lean_reference.py`), not against textbook
  formulas.
- `tests/runtime/test_lean_parity.py` keeps count of how much of LEAN's algorithm
  API is implemented. The count is only allowed to go up.
- `tests/runtime/test_loud_gaps.py` checks that anything unsupported raises an
  error with the name of the feature, and that no method quietly ignores an
  argument.
- `tests/runtime/test_bench_gate.py` checks that the benchmark script refuses to
  report when the engines disagree.

The tests that need exact dollar results run against one specific set of bars,
which cannot be redistributed. They skip if you do not have that set.

## Not measured yet

Live trading latency, from a bar arriving to an order being handed to the
broker, has not been measured against another engine. No number for it will be
published until there is a test that runs both engines side by side.

---

LEAN is a trademark of QuantConnect Corporation. NautilusTrader and backtrader
belong to their owners. They are named here only to say what was benchmarked.
DQengine is not affiliated with or endorsed by either. LEAN is licensed under
Apache-2.0.
