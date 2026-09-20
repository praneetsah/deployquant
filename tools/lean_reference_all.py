"""Golden values for EVERY LEAN indicator we can construct.

Auto-discovery rather than a hand-maintained list: for each indicator type in
QuantConnect.Indicators, try a few plausible constructor shapes, feed the
shared 60-bar series both ways (value and bar), and record whatever works.
What cannot be constructed or fed is reported, so the gap is visible.
"""
import json
import math
import sys
from datetime import datetime, timedelta

from clr_loader import get_coreclr
from pythonnet import set_runtime

set_runtime(get_coreclr(runtime_config="QuantConnect.Lean.Launcher.runtimeconfig.json"))
import clr                                                    # noqa: E402

sys.path.append(".")
clr.AddReference("QuantConnect.Indicators")
clr.AddReference("QuantConnect.Common")

import System                                                  # noqa: E402
from System import Activator, DateTime                         # noqa: E402
from QuantConnect.Indicators import IndicatorDataPoint         # noqa: E402


def safe_types(asm):
    try:
        return [t for t in asm.GetTypes() if t is not None]
    except Exception as e:
        return [t for t in (getattr(e, "Types", None) or []) if t is not None]


def D(x):
    return System.Convert.ToDecimal(float(x))


_ASMS = System.AppDomain.CurrentDomain.GetAssemblies()
_COMMON = [a for a in _ASMS if "QuantConnect.Common" in str(a)][0]
_IND = [a for a in _ASMS if "QuantConnect.Indicators" in str(a)][0]
_TB = [t for t in safe_types(_COMMON)
       if t.FullName == "QuantConnect.Data.Market.TradeBar"][0]
_SET = {n: _TB.GetProperty(n) for n in
        ("Time", "Open", "High", "Low", "Close", "Volume")}

N = 60


def series():
    out = []
    t = datetime(2024, 1, 2, 9, 31)
    for i in range(N):
        base = 100 + i * 0.5 + 3 * math.sin(i / 3.0)
        out.append((t + timedelta(minutes=i), base, base + 1 + (i % 3) * 0.25,
                    base - 1 - (i % 5) * 0.2,
                    base + math.sin(i / 2.0) * 0.75, 1000 + (i % 7) * 100))
    return out


BARS = series()


def make_bar(t, o, h, lo, c, v):
    bar = Activator.CreateInstance(_TB)
    _SET["Time"].SetValue(bar, DateTime(t.year, t.month, t.day, t.hour,
                                        t.minute, 0))
    for name, x in (("Open", o), ("High", h), ("Low", lo), ("Close", c),
                    ("Volume", v)):
        _SET[name].SetValue(bar, D(x))
    return bar


BAR_OBJS = [make_bar(*row) for row in BARS]
DP_OBJS = [IndicatorDataPoint(DateTime(t.year, t.month, t.day, t.hour,
                                       t.minute, 0), D(c))
           for t, o, h, lo, c, v in BARS]


def val(x):
    try:
        return round(float(System.Convert.ToDouble(x)), 8)
    except Exception:
        return None


# constructor shapes to try, in order
SHAPES = [
    (), (14,), (10,), (20,), (5, 10), (12, 26, 9), (10, D(2)), (10, 10),
    (14, 14, 3), (10, D(2.0), 10), (D(2),), (10, 5), (9, 25), (7, 14, 28),
    (10, 21), (20, D(2), 20, D(1.5)), (9, 26, 17, 52, 26, 26),
    (5, 5, 8, 5, 11, 5, 14, 8, 9), (14, 5, 3, 9), (3, 2, 10), (16,),
    (10, 10000), (20, 5), (5, D(2), 10), (D(0.05), 1), (10, D(3)),
]

out, errors = {}, {}
for t in sorted(safe_types(_IND), key=lambda x: x.Name):
    if not t.IsPublic or t.IsAbstract or t.IsInterface or t.IsGenericType:
        continue
    name = t.Name
    made = None
    for shape in SHAPES:
        try:
            arr = System.Array.CreateInstance(
                System.Type.GetType("System.Object"), len(shape))
            for i, a in enumerate(shape):
                arr.SetValue(System.Convert.ToInt32(a)
                             if isinstance(a, int) else a, i)
            made = (Activator.CreateInstance(t, arr), shape)
            break
        except Exception:
            continue
    if made is None:
        errors[name] = "no constructor shape matched"
        continue
    ind, shape = made
    fed = None
    for kind, objs in (("bar", BAR_OBJS), ("value", DP_OBJS)):
        try:
            probe = Activator.CreateInstance(
                t, System.Array.CreateInstance(
                    System.Type.GetType("System.Object"), 0)) \
                if not shape else ind
            for o in objs:
                probe.Update(o)
            fed = (kind, probe)
            break
        except Exception:
            # rebuild: a half-fed indicator cannot be reused
            try:
                arr = System.Array.CreateInstance(
                    System.Type.GetType("System.Object"), len(shape))
                for i, a in enumerate(shape):
                    arr.SetValue(System.Convert.ToInt32(a)
                                 if isinstance(a, int) else a, i)
                ind = Activator.CreateInstance(t, arr)
            except Exception:
                pass
            continue
    if fed is None:
        errors[name] = "neither bar nor value updates were accepted"
        continue
    kind, probe = fed
    out[name] = {"args": [float(System.Convert.ToDouble(a))
                          if not isinstance(a, int) else a for a in shape],
                 "feed": kind,
                 "value": val(probe.Current.Value),
                 "is_ready": bool(probe.IsReady)}

print(json.dumps({"bars": [[t.isoformat(), o, h, lo, c, v]
                           for t, o, h, lo, c, v in BARS],
                  "values": out, "errors": errors}, indent=1))
