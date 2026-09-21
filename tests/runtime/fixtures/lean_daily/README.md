# LEAN recordings: daily-resolution scheduling and order conversion

What LEAN does with a daily-resolution strategy, recorded so
`tests/runtime/test_daily_lean_parity.py` can hold this engine to it.

## How these were produced

Run on the LEAN CLI 1.0.228 (engine 2.5.0.0) on 2026-09-21, one local
project per file, against the SPY and SGOV daily files shipped in this
repo's data tree (`equity/usa/daily/spy.zip`, `sgov.zip`). Each probe
algorithm logs a JSON line per scheduled callback and per order event;
the lines are normalised into the shape the tests compare:

- `checks` — one row per callback: the clock, the price the algorithm could
  read for each symbol, that symbol's daily SMA and whether it was ready.
  Rows logged while LEAN was warming up are dropped, because this engine
  fires no scheduled event during warm-up.
- `orders` — one row per order LEAN accepted, with the type it converted to.
- `fills` — one row per fill: the clock LEAN stamped it with, the symbol,
  the quantity and the price.

Public market data only. No account, broker or live-trading data is in
these files.

## What each file covers

| File | Window | What it is for |
|---|---|---|
| `checks_march.json` | 2025-03-03..03-14 | five checks a day plus `on_data`, no orders: what a scheduled callback sees |
| `orders_market.json` | 2025-03-03..03-14 | a market order from each of those times; the 15.5-minute conversion boundary |
| `orders_moc.json` | 2025-03-03..03-14 | a market-on-close order from each; the submission buffer, and an order placed after the close |
| `combined_july.json` | 2025-06-27..07-09 | the 07-03 early close (13:00) and the 07-04 holiday |
| `combined_thanks.json` | 2025-11-21..12-03 | the 11-27 holiday and the 11-28 early close |
| `combined_twosym.json` | 2020-05-20..06-05 | no warm-up, and SGOV's first bar lands mid-window |
| `sizing.json` | 2025-03-03..03-21 | `set_holdings` and `liquidate` either side of the buffer |
| `userexplicit.json` | 2025 | the reported user's shape, sized by the strategy itself |
| `userlike.json` | 2025 | the same shape sized with `set_holdings` |

The last two zero LEAN's fee model and its free-portfolio reserve, and set
leverage to 2 (LEAN's own equity default), so the comparison is about the
daily rules rather than about defaults this engine sets elsewhere.
