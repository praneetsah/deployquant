# Live path benchmark

How long DQengine takes between a bar arriving and an order reaching the
broker adapter. See the "Live path" section of [BENCHMARK.md](../../BENCHMARK.md)
for the numbers.

- `run_dqengine_live.py` composes one live session in one process, the way
  `dqengine live` composes it, feeds one real session's minute bars through it
  one bar at a time, and prints the three spans it measured.
- `ema_cross_session.py` is `../bench_engines/ema_cross_fast.py` with its
  backtest window narrowed to the replayed session. The live harness runs the
  original (a live deployment takes its window from its own row); LEAN needs
  the narrowed copy.

```bash
# a scratch Postgres: the harness drops and rebuilds the schema in it
createdb dqengine_bench
export DQENGINE_BENCH_DATABASE_URL=postgresql+psycopg2://me@localhost/dqengine_bench

python run_dqengine_live.py ../bench_engines/ema_cross_fast.py \
    --data ../../data --day 2026-09-18 --bars 390 --warmup 30
python run_dqengine_live.py ../../dqengine/examples/tqqq_weekly.py \
    --data ../../data --day 2026-09-18 --bars 390 --warmup 30

# the same run over a real Redis instead of the in-process bus
REDIS_URL=redis://localhost:6379/9 python run_dqengine_live.py \
    ../bench_engines/ema_cross_fast.py --data ../../data --day 2026-09-18 --redis
```

You need minute bars for the symbol and the day (`dqengine data fetch SPY
--from 2026-09-18`), plus history before it, which is what the replay warms up
on.

The LEAN side is a backtest of the same session, which is the closest
reproducible number and not the same quantity. It runs through the other
benchmark's LEAN runner:

```bash
python ../bench_engines/run_lean.py ~/lean-workspace ema_cross_session.py LiveBench_EmaCross
```

LEAN's own live mode needs a brokerage plugin, a live node and a real-time
data subscription. That was not run, so there is no LEAN number for the span
this benchmark measures.
