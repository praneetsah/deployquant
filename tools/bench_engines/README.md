# Engine benchmark

The same strategy on DQengine, LEAN, NautilusTrader and backtrader, over the same SPY minute
bars. See the Benchmarks section of the main README for results.

- `ema_cross_slow.py`, `ema_cross_fast.py`, `do_nothing.py` are the strategies.
  DQengine and LEAN run these files as they are. NautilusTrader runs the
  equivalent `EMACross` example that ships with it.
- `run_dqengine.py`, `run_lean.py`, `run_nautilus.py`, `run_backtrader.py` run one trial and print
  one line of results.

You need SPY minute bars from 2021-01-04 to 2026-06-09 in the bar store
(`dqengine data fetch SPY --from 2021-01-04 --to 2026-06-09`), the `lean` CLI
with Docker for the LEAN runs, `pip install nautilus_trader` for the
NautilusTrader runs, and `pip install backtrader pandas` for the backtrader
runs.
