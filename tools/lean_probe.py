"""Interrogate LEAN's definitions directly, rather than guessing at them."""
import json
import sys

from clr_loader import get_coreclr
from pythonnet import set_runtime

set_runtime(get_coreclr(runtime_config="QuantConnect.Lean.Launcher.runtimeconfig.json"))
import clr                                                    # noqa: E402

sys.path.append(".")
clr.AddReference("QuantConnect.Indicators")
import System                                                  # noqa: E402
from System import DateTime                                    # noqa: E402
from QuantConnect.Indicators import (                          # noqa: E402
    ChandeMomentumOscillator, AbsolutePriceOscillator,
    PercentagePriceOscillator, SimpleMovingAverage,
    ExponentialMovingAverage, IndicatorDataPoint)

def D(x):
    return System.Convert.ToDecimal(float(x))

def run(ind, vals):
    out = []
    for i, v in enumerate(vals):
        ind.Update(IndicatorDataPoint(DateTime(2024, 1, 2, 9, 30 + i, 0), D(v)))
        out.append((round(float(System.Convert.ToDouble(ind.Current.Value)), 6),
                    bool(ind.IsReady)))
    return out

RAMP = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
MIX = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5]

print(json.dumps({
    "cmo3_ramp": run(ChandeMomentumOscillator(3), RAMP),
    "cmo3_mix": run(ChandeMomentumOscillator(3), MIX),
    "apo_3_5_ramp": run(AbsolutePriceOscillator(3, 5), RAMP),
    "ppo_3_5_ramp": run(PercentagePriceOscillator(3, 5), RAMP),
    "sma3_ramp": run(SimpleMovingAverage(3), RAMP),
    "sma5_ramp": run(SimpleMovingAverage(5), RAMP),
    "ema3_ramp": run(ExponentialMovingAverage(3), RAMP),
    "ema5_ramp": run(ExponentialMovingAverage(5), RAMP),
}, indent=1))
