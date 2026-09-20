"""Run one algorithm file on LEAN via its CLI and sample the container for peak
memory and CPU.

    python run_lean.py LEAN_WORKSPACE ALGORITHM.py PROJECT_NAME

LEAN_WORKSPACE is a folder created with `lean init` whose data folder holds the
same minute bars the other engines read."""
import os, re, shutil, subprocess, sys, threading, time
ws, algo, name = sys.argv[1], sys.argv[2], sys.argv[3]
proj = os.path.join(ws, name); os.makedirs(proj, exist_ok=True)
shutil.copyfile(algo, os.path.join(proj, "main.py"))
with open(os.path.join(proj, "config.json"), "w") as fh:
    fh.write('{"algorithm-language": "Python", "parameters": {}, "description": ""}')
peak = {"mem": 0.0, "cpu": 0.0}; stop = False; seen = set()
def unit(s):
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s); v = float(m.group(1)); u = m.group(2)[0]
    return v * {"K": 1/1024, "M": 1, "G": 1024}[u]
def sample():
    while not stop:
        r = subprocess.run(["docker", "stats", "--no-stream", "--format", "x|{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}"],
                           capture_output=True, text=True).stdout
        seen.update(l.split("|")[0] + "/" + l.split("|")[1] for l in r.splitlines() if l.count("|") == 3)
        for line in r.splitlines():
            if line.count("|") != 3: continue
            img, nm, mem, cpu = line.split("|")
            if nm.lower().startswith("lean_cli"):               # the container the LEAN CLI starts
                peak["mem"] = max(peak["mem"], unit(mem.split("/")[0].strip()))
                peak["cpu"] = max(peak["cpu"], float(cpu.strip("%") or 0))
th = threading.Thread(target=sample, daemon=True); th.start()
t0 = time.time()
out = subprocess.run([shutil.which("lean") or os.path.expanduser("~/Library/Python/3.9/bin/lean"), "backtest", name], cwd=ws, capture_output=True, text=True)
wall = time.time() - t0; stop = True
log = out.stdout + out.stderr
g = lambda p: (re.search(p, log) or [None, None])[1]
print(dict(engine="lean", algo=os.path.basename(algo), wall_s=round(wall, 2), compute_s=g(r"completed in ([\d.]+) seconds"),
           points_per_s=g(r"at ([\d.k]+) data points per second"), orders=g(r"Total Orders\s+(\d+)"),
           end_equity=g(r"End Equity\s+([\d.]+)"), peak_mib=round(peak["mem"], 1), peak_cpu_pct=round(peak["cpu"]),
           calls=g(r"ON_DATA_CALLS=(\d+)"), containers=sorted(seen), rc=out.returncode, err=(re.search(r"(Runtime Error|error).{0,160}", log) or [""])[0][:160] if out.returncode else ""))
shutil.rmtree(proj, ignore_errors=True)
