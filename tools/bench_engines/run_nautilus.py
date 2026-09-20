"""NautilusTrader side. Uses the EMACross example that ships with NautilusTrader,
with its two per-bar log calls removed so that logging is not what gets timed.

    FAST=2000 SLOW=8000 python run_nautilus.py DATA_ROOT [END_YYYYMMDD]
    NOOP=1 python run_nautilus.py DATA_ROOT        # strategy that does nothing

DATA_ROOT is the same bar store DQengine reads (equity/usa/minute/spy/*.zip)."""
import os, sys, time, zipfile, resource
from decimal import Decimal
import pandas as pd
T0 = time.time()
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.examples.strategies.ema_cross import EMACross, EMACrossConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider

# NYSE early closes (13:00 ET) inside the benchmark window. Without this the
# loader hands NautilusTrader 1,388 after-hours rows the other engines never see.
EARLY_CLOSE = {"20211126", "20221125", "20230703", "20231124", "20240703", "20241129",
               "20241224", "20250703", "20251128", "20251224"}
ROOT, START, END = sys.argv[1], "20210104", (sys.argv[2] if len(sys.argv) > 2 else "20260609")

class Noop(EMACross):
    """Receives every bar and does nothing: the engine's floor cost per bar."""
    n = 0
    def on_bar(self, bar):
        Noop.n += 1


class QuietEMACross(EMACross):
    """Identical decisions to the shipped example; its two per-bar log calls
    (one builds repr(bar) every bar) are dropped so logging is not what is timed."""
    def on_bar(self, bar):
        if not self.indicators_initialized():
            return
        if bar.is_single_price():
            return
        iid = self.config.instrument_id
        if self.fast_ema.value >= self.slow_ema.value:
            if self.portfolio.is_flat(iid):
                self.buy()
            elif self.portfolio.is_net_short(iid):
                self.close_all_positions(iid); self.buy()
        else:
            if self.portfolio.is_flat(iid):
                self.sell()
            elif self.portfolio.is_net_long(iid):
                self.close_all_positions(iid); self.sell()

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
        close = 46800000 if day in EARLY_CLOSE else 57600000    # 13:00 ET on half days
        df = df[(df.ms >= 34200000) & (df.ms < close)]           # the regular session, exactly what DQengine and LEAN step
        ts = (pd.Timestamp(day).tz_localize("America/New_York") + pd.to_timedelta(df.ms + 60000, unit="ms"))
        df = df.assign(timestamp=ts.dt.tz_convert("UTC") if hasattr(ts, "dt") else ts.tz_convert("UTC"))
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] / 10000.0
        frames.append(df.drop(columns="ms"))
    return pd.concat(frames).set_index("timestamp")

t = time.time(); df = load(); t_load = time.time() - t; print("loaded", len(df), round(t_load,2), flush=True)
inst = TestInstrumentProvider.equity(symbol="SPY", venue="XNAS")
bar_type = BarType.from_str("SPY.XNAS-1-MINUTE-LAST-EXTERNAL")
t = time.time(); bars = BarDataWrangler(bar_type, inst).process(df); t_wrangle = time.time() - t; print("wrangled", len(bars), round(t_wrangle,2), flush=True)

engine = BacktestEngine(config=BacktestEngineConfig(trader_id="BENCH-001", logging=LoggingConfig(bypass_logging=True), run_analysis=os.environ.get("ANALYSIS","1")=="1"))
engine.add_venue(venue=Venue("XNAS"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                 base_currency=USD, starting_balances=[Money(1_000_000, USD)], bar_execution=os.environ.get("BAR_EXEC","1")=="1")
engine.add_instrument(inst); engine.add_data(bars)
engine.add_strategy((Noop if os.environ.get("NOOP")=="1" else QuietEMACross)(EMACrossConfig(instrument_id=inst.id, bar_type=bar_type, trade_size=Decimal(100),
                    fast_ema_period=int(os.environ.get("FAST","10")), slow_ema_period=int(os.environ.get("SLOW","20")), subscribe_trade_ticks=False,
                    subscribe_quote_ticks=False, request_bars=False)))
t = time.time(); engine.run(); t_run = time.time() - t
fills = engine.trader.generate_order_fills_report()
acct = engine.trader.generate_account_report(Venue("XNAS"))
end_bal = float(acct.iloc[-1]["total"]) if len(acct) else None
print(dict(engine="nautilus", bars=len(bars), load_s=round(t_load, 2), wrangle_s=round(t_wrangle, 2), run_s=round(t_run, 2),
           total_s=round(time.time() - T0, 2), fills=len(fills), noop_calls=Noop.n, end_balance=end_bal,
           peak_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 1)))
engine.dispose()
