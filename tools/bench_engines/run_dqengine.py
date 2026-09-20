"""DQengine side.

    python run_dqengine.py DATA_ROOT ALGORITHM.py
    DQENGINE_FAST_PATH=0 python run_dqengine.py DATA_ROOT do_nothing.py
"""
import sys, time, resource, json
T0 = time.time()
from dqengine.runtime import run_python_backtest
t = time.time()
res = run_python_backtest(open(sys.argv[2]).read(), sys.argv[1])
assert "error" not in res, res.get("error")
print(dict(engine="dqengine", run_s=round(time.time() - t, 2), total_s=round(time.time() - T0, 2), fills=len(res["fills"]),
           end_equity=res["stats"]["end_equity"], fast_path=res.get("fast_path", {}).get("eligible"),
           logs=[l for l in res.get("logs", []) if "ON_DATA" in str(l)][:1],
           peak_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 1)))
