"""backtrader side. Same logic as the other runners: always in the market, long
100 shares while the fast EMA is at or above the slow EMA, short 100 otherwise.

    FAST=2000 SLOW=8000 python run_backtrader.py DATA_ROOT [END_YYYYMMDD]
    NOOP=1 python run_backtrader.py DATA_ROOT        # strategy that does nothing

DATA_ROOT is the same bar store DQengine reads (equity/usa/minute/spy/*.zip)."""
import os, resource, sys, time, zipfile
T0 = time.time()
import backtrader as bt
import pandas as pd

EARLY_CLOSE = {"20211126", "20221125", "20230703", "20231124", "20240703", "20241129",
               "20241224", "20250703", "20251128", "20251224"}
ROOT, START, END = sys.argv[1], "20210104", (sys.argv[2] if len(sys.argv) > 2 else "20260609")
FAST, SLOW = int(os.environ.get("FAST", "10")), int(os.environ.get("SLOW", "20"))


def load():
    d = os.path.join(ROOT, "equity", "usa", "minute", "spy")
    frames = []
    for fn in sorted(os.listdir(d)):
        day = fn[:8]
        if not (START <= day <= END) or not fn.endswith("_trade.zip"):
            continue
        with zipfile.ZipFile(os.path.join(d, fn)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]), header=None,
                             names=["ms", "open", "high", "low", "close", "volume"])
        close = 46800000 if day in EARLY_CLOSE else 57600000
        df = df[(df.ms >= 34200000) & (df.ms < close)]
        df = df.assign(datetime=pd.Timestamp(day) + pd.to_timedelta(df.ms + 60000, unit="ms"))
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] / 10000.0
        frames.append(df.drop(columns="ms"))
    return pd.concat(frames).set_index("datetime")


class Noop(bt.Strategy):
    calls = 0
    def next(self):
        Noop.calls += 1


class EmaCross(bt.Strategy):
    def __init__(self):
        self.fast = bt.indicators.EMA(self.data.close, period=FAST)
        self.slow = bt.indicators.EMA(self.data.close, period=SLOW)
        self.n = 0
    def notify_order(self, order):
        if order.status == order.Completed:
            self.n += 1
    def next(self):
        want = 100 if self.fast[0] >= self.slow[0] else -100
        have = self.position.size
        if have != want:
            self.order_target_size(target=want)


t = time.time(); df = load(); t_load = time.time() - t
cerebro = bt.Cerebro(stdstats=False)
cerebro.adddata(bt.feeds.PandasData(dataname=df, timeframe=bt.TimeFrame.Minutes, compression=1))
cerebro.broker.setcash(1_000_000); cerebro.broker.setcommission(commission=0.0)
cerebro.broker.set_shortcash(False)
strat_cls = Noop if os.environ.get("NOOP") == "1" else EmaCross
cerebro.addstrategy(strat_cls)
t = time.time(); res = cerebro.run(); t_run = time.time() - t
st = res[0]
print(dict(engine="backtrader", bars=len(df), load_s=round(t_load, 2), run_s=round(t_run, 2), total_s=round(time.time() - T0, 2),
           fills=getattr(st, "n", 0), noop_calls=Noop.calls, end_balance=round(cerebro.broker.getvalue(), 2),
           peak_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 1)))
