"""Sandboxed execution of user Python strategies — one Docker container per
run, hard caps, wall-clock kill, global concurrency gate.

This module is the ONLY place sandbox containers are launched from. The
container sees exactly two mounts (the per-run workdir rw, the bar store ro)
and no network; it never sees broker creds, Postgres, Redis, or the API env.
In prod this module runs inside the sandbox service (dqengine.sandbox.service,
the one container that mounts docker.sock); locally the API can call
run_sandboxed() directly.
"""
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import urllib.error
import urllib.request

PYRUN_IMAGE = os.environ.get("PYRUN_IMAGE", "pyrunner:dev")
# Engine-session RPC: response poll interval and how often to confirm the
# container with docker while a call is outstanding.
ENGINE_POLL_S = float(os.environ.get("PYRUN_ENGINE_POLL_S", "0.001"))
ENGINE_ALIVE_CHECK_S = 1.0
# How the driver talks to an engine container (dqengine/runtime/engine_rpc.py):
#   socket  connect the unix socket after readiness; a connect failure FAILS
#           session_start (container killed, raise). Prod sets this.
#   file    the file protocol only (today's behaviour).
#   auto    try the socket, fall to file with one WARNING -- macOS Docker
#           Desktop cannot connect a unix socket across a bind mount. Dev only.
ENGINE_TRANSPORT = os.environ.get("PYRUN_ENGINE_TRANSPORT", "auto")
if ENGINE_TRANSPORT not in ("socket", "file", "auto"):
    raise RuntimeError(f"PYRUN_ENGINE_TRANSPORT={ENGINE_TRANSPORT!r}: "
                       f"expected socket, file or auto")
ENGINE_CONNECT_S = 1.0
# session_id -> why the socket did not connect (cleared on a clean connect);
# surfaced by /engines/{id} and the api health so a fallback is never silent
TRANSPORT_FALLBACKS: dict = {}
ENGINE_MIN_CALL_S = 1.0
# the uid user code runs as inside the sandbox image (Dockerfile.pyrunner, beside this module)
SANDBOX_UID = int(os.environ.get("PYRUN_SANDBOX_UID", "1500"))

# the sandbox image's build context is the open distribution itself: DIST_ROOT
# is the copy this module ships in (platform/engine in a checkout, site-packages
# once installed) and SANDBOX_DOCKERFILE the image definition beside this file.
# The Dockerfile copies `platform/engine` out of its context, so a build's
# context is a directory that holds the distribution at that path (the repo
# root in a checkout; SANDBOX_CONTEXT inside the sandbox service, where
# Dockerfile.pyrunner_svc bakes the Dockerfile at its root and the distribution
# under platform/engine).
DIST_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SANDBOX_DOCKERFILE = os.path.join(os.path.dirname(__file__), "Dockerfile.pyrunner")
SANDBOX_CONTEXT = os.environ.get("PYRUN_SANDBOX_CONTEXT", "/srv/sandbox-context")


def _check_engine_mode(env: dict) -> str:
    """PYRUN_ENGINE_MODE: sandbox (default) | inproc. In-process runs user
    code INSIDE this process with zero isolation (dqengine/runtime/inproc.py); it
    is for a self-hosted box whose operator owns every strategy. Refused
    -- the process does not start -- unless PYRUN_INPROC_ALLOWED=1, and
    refused outright when PYRUNNER_URL is set: the hosted topology has a
    sandbox service, and an operator who set both has misconfigured the
    box. Never fall back to sandbox silently: an operator who set the
    mode expects it."""
    mode = env.get("PYRUN_ENGINE_MODE", "sandbox")
    if mode not in ("sandbox", "inproc"):
        raise RuntimeError(f"PYRUN_ENGINE_MODE={mode!r}: expected sandbox or inproc")
    if mode == "inproc":
        if env.get("PYRUNNER_URL"):
            raise RuntimeError("PYRUN_ENGINE_MODE=inproc with PYRUNNER_URL set: "
                               "the hosted topology never runs user code in-process")
        if env.get("PYRUN_INPROC_ALLOWED") != "1":
            raise RuntimeError("PYRUN_ENGINE_MODE=inproc requires PYRUN_INPROC_ALLOWED=1 "
                               "(user code will run IN THIS PROCESS with no isolation)")
    return mode


ENGINE_MODE = _check_engine_mode(os.environ)
if ENGINE_MODE == "inproc":
    print("[pyrunner] engine mode: inproc -- user code runs IN THIS PROCESS with "
          "no isolation, including across deployments", flush=True)
_INPROC: dict = {}
_INPROC_LOCK = threading.Lock()
PYRUN_CPUS = os.environ.get("PYRUN_CPUS", "1")
PYRUN_MEM = os.environ.get("PYRUN_MEM", "2g")
PYRUN_TMP_MB = int(os.environ.get("PYRUN_TMP_MB", "256"))
PYRUN_TIMEOUT_S = int(os.environ.get("PYRUN_TIMEOUT_S", "180"))
PYRUN_MAX_CONCURRENT = int(os.environ.get("PYRUN_MAX_CONCURRENT", "2"))

_sem = threading.Semaphore(PYRUN_MAX_CONCURRENT)


def _launch_cmd(name: str, workdir: str, data_dir: str, image: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--cpus", str(PYRUN_CPUS),
        "--memory", PYRUN_MEM,
        "--pids-limit", "256",
        "--read-only",
        "--tmpfs", f"/tmp:size={PYRUN_TMP_MB}m",
        "--security-opt", "no-new-privileges",
        "-v", f"{workdir}:/work",
        "-v", f"{data_dir}:/data:ro",
        image,
    ]


def run_sandboxed(code: str, run_cfg: dict, data_dir: str,
                  image: str | None = None,
                  timeout_s: int | None = None) -> dict:
    """Execute user code in a sandbox container. Returns the result dict
    (manifest or backtest result); every failure mode comes back as
    {"error": {"type", "message", ...}} — this function does not raise for
    run problems."""
    image = image or PYRUN_IMAGE
    timeout_s = timeout_s or PYRUN_TIMEOUT_S

    warm = _acquire_warm(data_dir) if _pool_size() else None
    if _pool_size():
        # replenish behind us — on the COLD path too, so a burst after an
        # idle-out refills the pool instead of staying cold forever
        threading.Thread(target=ensure_pool, args=(data_dir, image),
                         daemon=True).start()
    # The concurrency gate is BOUNDED: a live replay queued behind two long
    # backtests must come back as "runner busy" (RunnerUnavailable on the
    # caller: row untouched, next tick retries) inside its own budget, not
    # wait past the API's HTTP timeout and be reported as a strategy error.
    if warm:
        out_name = ("manifest.json" if run_cfg.get("mode") == "manifest"
                    else "result.json")
        try:
            if not _sem.acquire(timeout=timeout_s):
                return _busy(timeout_s)
            try:
                return _execute_warm(warm, code, run_cfg, timeout_s, out_name)
            finally:
                _sem.release()
        finally:
            shutil.rmtree(warm["workdir"], ignore_errors=True)

    name = f"pyrun-{uuid.uuid4().hex[:12]}"
    workdir, out_name = _prepare_workdir(code, run_cfg)
    try:
        if not _sem.acquire(timeout=timeout_s):
            return _busy(timeout_s)
        try:
            return _execute(name, workdir, data_dir, image, timeout_s, out_name)
        finally:
            _sem.release()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _busy(timeout_s) -> dict:
    return {"error": {"type": "RunnerUnavailable",
                      "message": f"sandbox busy: {PYRUN_MAX_CONCURRENT} runs "
                                 f"in flight for {timeout_s}s"}}


def _make_workdir() -> str:
    """Empty per-run dir. PYRUN_WORK_BASE (prod): the sandbox is a SIBLING
    container, so the workdir bind must be a HOST-visible path — a base dir
    bind-mounted into this service at the same path it has on the host.
    Local dev needs no such thing."""
    work_base = os.environ.get("PYRUN_WORK_BASE")
    if work_base:
        os.makedirs(work_base, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="pyrun-", dir=work_base or None)
    # the container's non-root user must be able to write result.json
    os.chmod(workdir, 0o777)
    return workdir


def _write_run_files(workdir: str, code: str, run_cfg: dict,
                     data_root: str = "/data") -> str:
    """main.py first, run.json LAST — a warm container fires the moment
    both exist, so run.json is the trigger. Returns the output filename.

    `data_root` is where the RUNNER will read bars from: `/data` is the
    container's mount, and the in-process mode below passes the real path
    because there is no mount."""
    mode = run_cfg.get("mode", "full")
    out_name = "manifest.json" if mode == "manifest" else "result.json"
    with open(os.path.join(workdir, "main.py"), "w") as fh:
        fh.write(code)
    cfg = dict(run_cfg)
    cfg["data_root"] = data_root
    tmp = os.path.join(workdir, "run.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(cfg, fh)
    os.replace(tmp, os.path.join(workdir, "run.json"))
    return out_name


def _prepare_workdir(code: str, run_cfg: dict) -> tuple[str, str]:
    workdir = _make_workdir()
    return workdir, _write_run_files(workdir, code, run_cfg)


def _execute(name: str, workdir: str, data_dir: str, image: str | None,
             timeout_s: int, out_name: str) -> dict:
    """The blocking container lifecycle: launch, wait (hard kill on the
    deadline), parse the output file."""
    image = image or PYRUN_IMAGE
    try:
        proc = subprocess.run(
            _launch_cmd(name, workdir, data_dir, image),
            capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name],
                       capture_output=True, text=True)
        return {"error": {
            "type": "Timeout",
            "message": f"backtest exceeded the {timeout_s}s time limit "
                       f"and was stopped"}}

    out_path = os.path.join(workdir, out_name)
    if os.path.exists(out_path):
        with open(out_path) as fh:
            return json.load(fh)
    if proc.returncode == 137:
        return {"error": {
            "type": "ResourceLimit",
            "message": f"the run was killed by the sandbox "
                       f"(memory limit {PYRUN_MEM} exceeded, most likely)"}}
    return {"error": {
        "type": "RunnerError",
        "message": f"sandbox produced no output (exit {proc.returncode})",
        "detail": (proc.stderr or "")[-2000:]}}


# --------------------------------------------------------------- warm pool
# Pre-launched sandbox containers that have already paid container-start +
# numpy/pandas import (~1.5-2.5s) and idle waiting for run files. One warm
# container serves exactly one run, then a replacement is spawned.

_POOL: list[dict] = []          # {name, workdir, data_dir}
_POOL_LOCK = threading.Lock()


def _pool_size() -> int:
    return int(os.environ.get("PYRUN_WARM_POOL", "0"))


def _spawn_warm(data_dir: str, image: str | None = None):
    name = f"pyrun-warm-{uuid.uuid4().hex[:12]}"
    workdir = _make_workdir()
    cmd = _launch_cmd(name, workdir, data_dir, image or PYRUN_IMAGE)
    cmd.insert(2, "-d")            # detached; --rm still applies on exit
    cmd.insert(3, "-e")
    cmd.insert(4, f"DQENGINE_WARM_IDLE_S={os.environ.get('DQENGINE_WARM_IDLE_S', '1800')}")
    cmd.append("--warm")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return
    with _POOL_LOCK:
        _POOL.append({"name": name, "workdir": workdir, "data_dir": data_dir})


def ensure_pool(data_dir: str, image: str | None = None):
    """Top the pool up to PYRUN_WARM_POOL entries (no-op when 0). Entries
    whose container idled out are pruned first — counting the dead as
    alive left the pool permanently empty after a quiet half hour, which
    also let the nightly docker prune delete the (then-unreferenced)
    sandbox image out from under us (2026-08-30 incident)."""
    want = _pool_size()
    if want <= 0:
        return
    with _POOL_LOCK:
        entries = [e for e in _POOL if e["data_dir"] == data_dir]
    for e in entries:
        if not _alive(e["name"]):
            with _POOL_LOCK:
                if e in _POOL:
                    _POOL.remove(e)
            shutil.rmtree(e["workdir"], ignore_errors=True)
    with _POOL_LOCK:
        have = len([e for e in _POOL if e["data_dir"] == data_dir])
    for _ in range(max(0, want - have)):
        _spawn_warm(data_dir, image)


def _alive(name: str) -> bool:
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _acquire_warm(data_dir: str):
    """Pop a live warm container bound to this data dir, discarding any
    that idled out; None when the pool is empty or disabled."""
    while True:
        with _POOL_LOCK:
            entry = next((e for e in _POOL if e["data_dir"] == data_dir), None)
            if entry:
                _POOL.remove(entry)
        if entry is None:
            return None
        if _alive(entry["name"]):
            return entry
        shutil.rmtree(entry["workdir"], ignore_errors=True)


def _execute_warm(entry: dict, code: str, run_cfg: dict,
                  timeout_s: int, out_name: str) -> dict:
    """Dispatch onto an already-running warm container and wait for it."""
    workdir = entry["workdir"]
    _write_run_files(workdir, code, run_cfg)
    try:
        proc = subprocess.run(["docker", "wait", entry["name"]],
                              capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", entry["name"]],
                       capture_output=True, text=True)
        return {"error": {
            "type": "Timeout",
            "message": f"backtest exceeded the {timeout_s}s time limit "
                       f"and was stopped"}}
    out_path = os.path.join(workdir, out_name)
    if os.path.exists(out_path):
        with open(out_path) as fh:
            return json.load(fh)
    exit_code = proc.stdout.strip()
    if exit_code == "137":
        return {"error": {
            "type": "ResourceLimit",
            "message": f"the run was killed by the sandbox "
                       f"(memory limit {PYRUN_MEM} exceeded, most likely)"}}
    # No output and a clean-ish exit: almost certainly the idle-deadline
    # race (the warm container timed out between our aliveness check and
    # the file drop). The run files are already in the workdir — a cold
    # container picks them up as-is.
    return _execute(f"pyrun-{uuid.uuid4().hex[:12]}", workdir,
                    entry["data_dir"], None, timeout_s, out_name)


# ------------------------------------------------ start/poll (live progress)
# One registry per process; entries carry the live workdir while running so
# poll_run can read the sandbox's progress.json snapshots, and the final
# result after. Done entries are GC'd after 10 minutes.

_RUNS: dict[str, dict] = {}
_RUNS_LOCK = threading.Lock()
_RUN_TTL_S = 600


def _gc_runs():
    now = time.time()
    with _RUNS_LOCK:
        for rid in [r for r, e in _RUNS.items()
                    if e.get("done_at") and now - e["done_at"] > _RUN_TTL_S]:
            _RUNS.pop(rid, None)


def start_run(code: str, run_cfg: dict, data_dir: str,
              image: str | None = None,
              timeout_s: int | None = None) -> str:
    """Launch a sandboxed run in the background; returns a run id for
    poll_run. Same caps/semaphore/timeout semantics as run_sandboxed."""
    _gc_runs()
    run_id = uuid.uuid4().hex[:16]
    out_name = ("manifest.json" if run_cfg.get("mode") == "manifest"
                else "result.json")

    warm = _acquire_warm(data_dir) if _pool_size() else None
    if _pool_size():
        threading.Thread(target=ensure_pool, args=(data_dir, image),
                         daemon=True).start()      # replenish (cold path too)
    if warm:
        workdir = warm["workdir"]

        def _run_once():
            return _execute_warm(warm, code, run_cfg,
                                 timeout_s or PYRUN_TIMEOUT_S, out_name)
    else:
        workdir, out_name = _prepare_workdir(code, run_cfg)

        def _run_once():
            return _execute(f"pyrun-{run_id[:12]}", workdir, data_dir,
                            image, timeout_s or PYRUN_TIMEOUT_S, out_name)

    entry = {"status": "queued", "workdir": workdir, "result": None,
             "done_at": None}
    with _RUNS_LOCK:
        _RUNS[run_id] = entry

    def _worker():
        try:
            with _sem:
                entry["status"] = "running"
                res = _run_once()
        except Exception as e:  # noqa: BLE001 — surfaced to the poller
            res = {"error": {"type": "RunnerError",
                             "message": f"runner thread failed: {e}"}}
        entry["result"] = res
        entry["status"] = "done"
        entry["done_at"] = time.time()
        shutil.rmtree(workdir, ignore_errors=True)

    threading.Thread(target=_worker, daemon=True, name=f"pyrun-{run_id[:8]}").start()
    return run_id


def _read_json_quiet(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def poll_run(run_id: str) -> dict:
    """{"status": queued|running|done|unknown, "progress"?: snapshot,
    "result"?: final dict}. Never raises."""
    with _RUNS_LOCK:
        entry = _RUNS.get(run_id)
    if entry is None:
        return {"status": "unknown"}
    out = {"status": entry["status"]}
    if entry["status"] == "done":
        out["result"] = entry["result"]
    elif entry["status"] == "running":
        prog = _read_json_quiet(os.path.join(entry["workdir"], "progress.json"))
        if prog:
            out["progress"] = prog
    return out


def _pydata_root() -> str:
    got = os.environ.get("PYDATA_ROOT")
    if got:
        return got
    from dqengine.config import DATA_ROOT
    return DATA_ROOT


def _svc_request(path: str, body: dict | None = None,
                 timeout: float = 30) -> dict:
    url = os.environ["PYRUNNER_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('PYRUN_SVC_TOKEN', '')}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def run_streaming(code: str, run_cfg: dict, timeout_s: int | None = None,
                  on_progress=None) -> dict:
    """Like run(), but relays the sandbox's live snapshots to on_progress
    while the sim executes. Poll cadence ~1s; every failure mode comes back
    as {"error": ...} — never raises."""
    timeout_s = timeout_s or PYRUN_TIMEOUT_S
    deadline = time.time() + timeout_s + 90    # container timeout + slack
    url = os.environ.get("PYRUNNER_URL")

    def _notify(prog):
        if on_progress and prog:
            try:
                on_progress(prog)
            except Exception:  # noqa: BLE001 — observers can't kill the run
                pass

    try:
        if url:
            run_id = _svc_request(
                "/runs", {"code": code, "run_cfg": run_cfg,
                          "timeout_s": timeout_s})["run_id"]
        else:
            run_id = start_run(code, run_cfg, data_dir=_pydata_root(),
                               timeout_s=timeout_s)
        while time.time() < deadline:
            if url:
                st = _svc_request(f"/runs/{run_id}")
            else:
                st = poll_run(run_id)
            if st["status"] == "done":
                return st.get("result") or {"error": {
                    "type": "RunnerError", "message": "run finished with no result"}}
            if st["status"] == "unknown":
                return {"error": {"type": "RunnerError",
                                  "message": "runner lost track of the run "
                                             "(restarted mid-backtest?)"}}
            _notify(st.get("progress"))
            time.sleep(1.0)
        return {"error": {"type": "Timeout",
                          "message": f"run did not finish within "
                                     f"{timeout_s}s (+slack)"}}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return {"error": {"type": "RunnerUnavailable",
                          "message": f"python runner unreachable: {e}"}}


def run_inproc(code: str, run_cfg: dict) -> dict:
    """One shot with no container: the sandbox's OWN entry point, called in
    this process (PYRUN_ENGINE_MODE=inproc).

    The run.json contract, the overrides it builds, the ledger it rebuilds
    and the result it writes are `dqengine.runtime.sandbox_entry`'s, the
    same module the image runs — so an in-process result is the container's
    result, not a second implementation of it.

    No isolation and no timeout: the same trade a self-hosted operator
    makes for the long-lived engine (dqengine/runtime/inproc.py), for the
    same reason. Every strategy on the box is their own."""
    from dqengine.runtime import sandbox_entry
    workdir = _make_workdir()
    try:
        out_name = _write_run_files(workdir, code, run_cfg,
                                    data_root=_pydata_root())
        sandbox_entry.main(workdir)
        with open(os.path.join(workdir, out_name)) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run(code: str, run_cfg: dict, timeout_s: int | None = None) -> dict:
    """The one entry the job worker calls. In-process when the engine mode
    says so (a self-hoster running their own code). Local (dev): docker
    directly. Prod: the sandbox service over the internal network
    (PYRUNNER_URL) — the API container never holds docker.sock."""
    url = os.environ.get("PYRUNNER_URL")
    if not url:
        if ENGINE_MODE == "inproc":
            return run_inproc(code, run_cfg)
        return run_sandboxed(code, run_cfg, data_dir=_pydata_root(),
                             timeout_s=timeout_s)
    body = json.dumps({"code": code, "run_cfg": run_cfg,
                       "timeout_s": timeout_s}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/run", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('PYRUN_SVC_TOKEN', '')}"})
    try:
        with urllib.request.urlopen(
                req, timeout=(timeout_s or PYRUN_TIMEOUT_S) + 30) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": {"type": "RunnerUnavailable",
                          "message": f"python runner unreachable: {e}"}}


# ------------------------------------------------- long-lived engine sessions
# A session is one sandbox container running `sandbox_entry --serve` over a
# dedicated workdir, driven through the file protocol in
# dqengine.runtime.engine_rpc. It outlives many calls: this is the sub-second live
# path, replacing a full replay per tick with "step the bars that closed".
#
# Same isolation as every other sandbox: --network none, two mounts, user
# code never in this process. The driver only ever touches files.

_SESSIONS: dict[str, dict] = {}       # session_id -> {name, workdir, data_dir, seq}
_SESSIONS_LOCK = threading.Lock()
ENGINE_LABEL = "strategy-lab.engine"


def kill_labelled(session_id: str | None = None) -> int:
    """Kill engine containers by label -- one deployment's, or all of them
    (svc startup sweep: the registry that knew their names is gone)."""
    flt = f"label={ENGINE_LABEL}" + (f"={session_id}" if session_id else "")
    r = subprocess.run(["docker", "ps", "-q", "--filter", flt],
                       capture_output=True, text=True)
    ids = [x for x in r.stdout.split() if x]
    if ids:
        subprocess.run(["docker", "kill", *ids], capture_output=True, text=True)
    return len(ids)


def _engine_root() -> str | None:
    """Where dqengine.runtime lives for THIS process: ENGINE_ROOT when set; else
    the distribution this module ships in (DIST_ROOT -- dqengine.runtime is part
    of the same distribution, so this resolves in a checkout and in an
    installed copy alike); else the sandbox service's baked build context,
    a second copy of the distribution (SANDBOX_CONTEXT/platform/engine).
    The first prod engine session failed with 'No module named dqengine.runtime'
    because only the repo layout was tried."""
    for cand in (os.environ.get("ENGINE_ROOT"),
                 DIST_ROOT,
                 os.path.join(SANDBOX_CONTEXT, "platform", "engine")):
        if cand and os.path.isdir(os.path.join(cand, "dqengine", "runtime")):
            return os.path.abspath(cand)
    return None


_RPC_MOD = None


def _rpc():
    """dqengine/runtime/engine_rpc.py, loaded BY FILE. Importing it as a package
    member runs dqengine/runtime/__init__ and pulls the whole engine (numpy,
    pandas) into this process. The driver needs only the wire format, which
    is pure stdlib, so it takes the file and the sandbox service's process
    stays free of the engine -- and when that service shipped without numpy
    (before it installed the distribution) the first prod engine session
    died on 'No module named numpy' for exactly this reason."""
    global _RPC_MOD
    if _RPC_MOD is not None:
        return _RPC_MOD
    eng = _engine_root()
    if not eng:
        raise RuntimeError("engine package not found (ENGINE_ROOT)")
    import importlib.util
    path = os.path.join(eng, "dqengine", "runtime", "engine_rpc.py")
    spec = importlib.util.spec_from_file_location("_strategy_lab_engine_rpc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _RPC_MOD = mod
    return mod


def session_start(session_id: str, code: str, run_cfg: dict, data_dir: str,
                  image: str | None = None, ready_timeout_s: float = 60.0) -> dict:
    """Launch (or replace) the engine container for `session_id`. Returns
    the heartbeat state once the engine has built, or raises."""
    session_stop(session_id)
    # A container from a previous svc process carries no registry entry but
    # is still running (registries die with the process; --rm fires only on
    # exit). Find it by label and kill it, or two engines serve one
    # deployment until DQENGINE_SERVE_IDLE_S.
    kill_labelled(session_id)
    rpc = _rpc()
    name = f"pyrun-engine-{uuid.uuid4().hex[:12]}"
    workdir, _ = _prepare_workdir(code, run_cfg)
    cmd = _launch_cmd(name, workdir, data_dir, image or PYRUN_IMAGE)
    cmd.insert(2, "--label")
    cmd.insert(3, f"{ENGINE_LABEL}={session_id}")
    cmd.insert(2, "-d")
    cmd.insert(3, "-e")
    cmd.insert(4, f"DQENGINE_SERVE_IDLE_S={os.environ.get('DQENGINE_SERVE_IDLE_S', str(4 * 3600))}")
    cmd.append("--serve")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"engine container failed to start: {r.stderr[-400:]}")
    entry = {"name": name, "workdir": workdir, "data_dir": data_dir, "seq": 0,
             "sock": None, "transport": "file", "lock": threading.Lock()}
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = entry
    deadline = time.time() + ready_timeout_s
    while time.time() < deadline:
        st = rpc.read_state(workdir)
        if st is not None:
            if st.get("alive"):
                _connect_transport(session_id, entry, st)
                # the transport is fixed for the session's life: callers
                # cache THIS rather than asking per tick (a remote ask is an
                # HTTP round trip + docker inspect, more than the socket saves)
                return {**st, "transport": entry["transport"]}
            session_stop(session_id)
            raise RuntimeError(f"engine failed to build: {st.get('dead')}")
        if not _alive(name):
            session_stop(session_id)
            raise RuntimeError("engine container exited before announcing itself")
        time.sleep(0.05)
    session_stop(session_id)
    raise TimeoutError("engine did not become ready in time")


def _connect_transport(session_id: str, entry: dict, state: dict) -> None:
    """Pick the transport for a session that just announced alive. The
    socket is bound before alive is written (engine_server.py), so a
    connect that fails now is a real failure, not a race."""
    if ENGINE_TRANSPORT == "file":
        return
    rpc = _rpc()
    path = rpc.sock_path(entry["workdir"])
    try:
        if not state.get("socket", True):
            raise OSError(f"server bound no socket: {state.get('socket_error')}")
        # The path lives in a directory USER CODE can write (it runs in the
        # server process as the sandbox uid, which owns the socket file).
        # Before the driver connects, that code could replace engine.sock
        # with a symlink to any host socket (docker.sock, say) -- the host
        # resolves the link in the host namespace. So: the file must be a
        # socket, not a link, owned by the sandbox uid; and after connect
        # the PEER must be the sandbox uid, not root (docker.sock's peer).
        # (A link to ANOTHER engine's socket would pass -- same uid -- but
        # needs that engine's host path, PYRUN_WORK_BASE/pyrun-<random>,
        # which is not visible inside a container.)
        st_ = os.lstat(path)
        if not stat.S_ISSOCK(st_.st_mode) or stat.S_ISLNK(st_.st_mode):
            raise OSError(f"{path} is not a plain socket file")
        if st_.st_uid != SANDBOX_UID:
            raise OSError(f"{path} owned by uid {st_.st_uid}, expected {SANDBOX_UID}")
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.settimeout(ENGINE_CONNECT_S)
        sk.connect(path)
        uid = _peer_uid(sk)
        if uid is not None and uid != SANDBOX_UID:
            sk.close()
            raise OSError(f"socket peer is uid {uid}, expected the sandbox uid "
                          f"{SANDBOX_UID}")
        entry["sock"] = sk
        entry["transport"] = "socket"
        TRANSPORT_FALLBACKS.pop(session_id, None)
    except OSError as e:
        # Fall to the file transport EITHER way -- a warm engine on the
        # 2.75 ms path beats no warm engine (a session failure per tick
        # would leave the sleeve on the 2 s replay all day). What differs
        # is how loud: in `socket` mode this is an ERROR and a counter the
        # health endpoint and /engines/{id} expose, so prod can never sit on
        # the fallback unnoticed; in `auto` (macOS) it is expected.
        TRANSPORT_FALLBACKS[session_id] = repr(e)
        level = "ERROR" if ENGINE_TRANSPORT == "socket" else "note"
        print(f"[pyrunner] {level}: engine {session_id}: socket unavailable "
              f"({e!r}); using the file transport "
              f"(PYRUN_ENGINE_TRANSPORT={ENGINE_TRANSPORT})", flush=True)


def _peer_uid(sk) -> int | None:
    """uid of the process on the other end, or None where the platform
    offers no way to ask (then the lstat checks above are the guard)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):                       # Linux
            pid, uid, gid = struct.unpack("3i", sk.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return uid
        if sys.platform == "darwin":                              # LOCAL_PEERCRED
            raw = sk.getsockopt(0, 1, 8)                          # struct xucred head
            return struct.unpack("Ii", raw)[1]
    except OSError:
        return None
    return None


def session_transport(session_id: str) -> str:
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(session_id)
    return entry["transport"] if entry else "none"


def transport_health() -> dict:
    """What the svc is running its engines on: prod expects every session
    on the socket and this dict empty of fallbacks."""
    with _SESSIONS_LOCK:
        by = {}
        for sid, e in _SESSIONS.items():
            by[e.get("transport", "?")] = by.get(e.get("transport", "?"), 0) + 1
    return {"configured": ENGINE_TRANSPORT, "sessions": by,
            "fallbacks": dict(TRANSPORT_FALLBACKS)}


def _kill_socket(entry: dict) -> None:
    sk = entry.get("sock")
    entry["sock"] = None
    entry["transport"] = "dead"
    if sk is not None:
        # shutdown first: a thread blocked in recv on this socket wakes
        # with EOF now instead of waiting out its deadline
        for fn in (lambda: sk.shutdown(socket.SHUT_RDWR), sk.close):
            try:
                fn()
            except OSError:
                pass


def session_call(session_id: str, op: str, args: dict | None = None,
                 timeout_s: float = 20.0) -> dict:
    """One request/response. Raises RuntimeError on a session that is gone,
    an op error, or a timeout -- the caller treats every one of those as
    "discard this engine and fall back to replay". Never retries, never
    switches transport mid-session."""
    rpc = _rpc()
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(session_id)
    if entry is None:
        raise RuntimeError("no such engine session")
    # Two callers on one session (the worker/poll hand-back in live.py)
    # serialize here, on BOTH transports, and the seq is allocated INSIDE
    # the lock: allocated outside it, a later-allocated seq could reach the
    # server first and the earlier one would then be refused as a "retry"
    # -- a healthy engine killed by a false duplicate. The wait counts
    # against timeout_s so a queued caller cannot block past its budget.
    t0 = time.monotonic()
    if not entry["lock"].acquire(timeout=timeout_s):
        raise RuntimeError(f"engine session busy: {op} waited {timeout_s}s for the lock")
    try:
        remaining = timeout_s - (time.monotonic() - t0)
        if remaining < min(ENGINE_MIN_CALL_S, timeout_s / 2):
            # a request once sent cannot be abandoned safely (the engine
            # would step it and the driver would kill a healthy container
            # on the timeout): refuse BEFORE sending
            raise RuntimeError(f"engine session busy: only {remaining:.2f}s of "
                               f"{timeout_s}s left for {op} after waiting")
        entry["seq"] += 1
        seq = entry["seq"]
        if entry["transport"] == "socket":
            return _socket_call(entry, seq, op, args or {}, remaining)
        if entry["transport"] == "dead":
            raise RuntimeError("engine session transport is dead")
        return _file_call(entry, seq, op, args or {}, remaining)
    finally:
        entry["lock"].release()


def _file_call(entry: dict, seq: int, op: str, args: dict, timeout_s: float) -> dict:
    rpc = _rpc()
    wd = entry["workdir"]
    rpc.write_atomic(rpc.req_path(wd, seq), {"op": op, "args": args})
    deadline = time.time() + timeout_s
    # `docker inspect` is a ~20 ms subprocess; run it at most once a second
    # while waiting. Between checks the server's state.json (written after
    # every request and once a second while idle) is the liveness signal.
    next_docker_check = time.time() + ENGINE_ALIVE_CHECK_S
    while time.time() < deadline:
        res = rpc.read_json(rpc.res_path(wd, seq))
        if res is not None:
            try:
                os.remove(rpc.res_path(wd, seq))
            except OSError:
                pass
            if not res.get("ok"):
                raise rpc.envelope_error(res)
            return res.get("result")
        st = rpc.read_state(wd)
        if st is not None and st.get("alive") is False:
            raise RuntimeError(f"engine session ended: {st.get('dead')}")
        if time.time() >= next_docker_check:
            if not _alive(entry["name"]):
                raise RuntimeError("engine container is gone")
            next_docker_check = time.time() + ENGINE_ALIVE_CHECK_S
        time.sleep(ENGINE_POLL_S)
    raise RuntimeError(f"engine did not answer {op} within {timeout_s}s")


def _socket_call(entry: dict, seq: int, op: str, args: dict,
                 timeout_s: float) -> dict:
    """One framed request on the session's socket. ANY failure -- timeout,
    EOF, a frame that does not validate, a parser blowing up on a hostile
    body -- closes the socket, marks the transport dead (sticky) and
    raises. The caller kills the container; the socket is never reused."""
    rpc = _rpc()
    sk = entry.get("sock")
    if sk is None:
        raise RuntimeError("engine session transport is dead")
    deadline = time.monotonic() + timeout_s
    try:
        rpc.send_frame(sk, {"seq": seq, "op": op, "args": args}, deadline)
        res = rpc.recv_frame(sk, deadline)
        if res.get("seq") != seq:
            raise ValueError(f"response seq {res.get('seq')!r} for request {seq}")
        if not isinstance(res.get("ok"), bool):
            raise ValueError("response has no boolean ok")
        if res["ok"] and not isinstance(res.get("result"), dict):
            raise ValueError("ok response without a result object")
        if not res["ok"] and not isinstance(res.get("error", {}), dict):
            raise ValueError("error response without an error object")
    except Exception as e:                              # noqa: BLE001
        _kill_socket(entry)
        raise RuntimeError(f"engine socket failed on {op}: {e!r}") from e
    if not res["ok"]:
        raise rpc.envelope_error(res)
    return res["result"]


def session_alive(session_id: str) -> bool:
    with _SESSIONS_LOCK:
        entry = _SESSIONS.get(session_id)
    if entry is None or entry.get("transport") == "dead":
        return False
    st = _rpc().read_state(entry["workdir"])
    if not (st and st.get("alive")):
        return False
    if entry.get("transport") == "socket":
        # the stream is the liveness check: a dead container is EOF on the
        # next call, which fails loud. No ~20 ms docker inspect per bar.
        return True
    now = time.monotonic()
    if now - entry.get("_inspected_at", -1e9) >= ENGINE_ALIVE_CHECK_S:
        entry["_inspect_ok"] = _alive(entry["name"])
        entry["_inspected_at"] = now
    return bool(entry.get("_inspect_ok", True))


def session_stop(session_id: str) -> None:
    """Idempotent. Asks politely, then kills; always frees the workdir."""
    with _SESSIONS_LOCK:
        entry = _SESSIONS.pop(session_id, None)
    if entry is None:
        return
    _kill_socket(entry)
    try:
        rpc = _rpc()
        entry["seq"] += 1
        rpc.write_atomic(rpc.req_path(entry["workdir"], entry["seq"]),
                         {"op": rpc.STOP_OP, "args": {}})
        time.sleep(0.2)
    except Exception:                                   # noqa: BLE001
        pass
    subprocess.run(["docker", "kill", entry["name"]],
                   capture_output=True, text=True)
    shutil.rmtree(entry["workdir"], ignore_errors=True)


# ---- local/remote dispatch, same shape as run() ----------------------------

def _remote(path: str, body: dict | None, method: str = "POST",
            timeout_s: float = 30.0) -> dict:
    url = os.environ["PYRUNNER_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('PYRUN_SVC_TOKEN', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s + 10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # the svc puts the REASON in the body (HTTPException detail); a bare
        # "502 Bad Gateway" in the api log left engine-build failures blind
        try:
            body = e.read().decode(errors="replace")[:600]
        except Exception:                                # noqa: BLE001
            body = ""
        raise urllib.error.HTTPError(e.url, e.code, f"{e.reason}: {body}",
                                     e.headers, None) from None


# ---- in-process sessions (self-hosted only; see _check_engine_mode) --------

def _inproc_start(session_id: str, code: str, run_cfg: dict) -> dict:
    _inproc_stop(session_id)
    def _rt():
        # dqengine.runtime is an installed package (spec 2026-09-18 R5/R6); the
        # in-process session imports it like any other
        from dqengine.runtime.inproc import InProcSession
        return InProcSession
    sess = _rt()(session_id, code, run_cfg, data_root=_pydata_root())
    with _INPROC_LOCK:
        _INPROC[session_id] = sess
    return {"alive": True, "dead": None, "seq_done": -1, "transport": "inproc"}


def _inproc_call(session_id: str, op: str, args: dict | None, timeout_s: float) -> dict:
    with _INPROC_LOCK:
        sess = _INPROC.get(session_id)
    if sess is None:
        raise RuntimeError("no such engine session")
    return sess.call(op, args, timeout_s)


def _inproc_alive(session_id: str) -> bool:
    with _INPROC_LOCK:
        sess = _INPROC.get(session_id)
    return bool(sess and sess.alive())


def _inproc_stop(session_id: str) -> None:
    with _INPROC_LOCK:
        sess = _INPROC.pop(session_id, None)
    if sess is not None:
        sess.stop()


# ---- dispatch: remote first, then the mode. In the hosted topology the
# mode branch is unreachable (PYRUNNER_URL is set) by construction. -----

def engine_start(session_id: str, code: str, run_cfg: dict) -> dict:
    if os.environ.get("PYRUNNER_URL"):
        return _remote(f"/engines/{session_id}",
                       {"code": code, "run_cfg": run_cfg}, timeout_s=90)
    if ENGINE_MODE == "inproc":
        return _inproc_start(session_id, code, run_cfg)
    return session_start(session_id, code, run_cfg, data_dir=_pydata_root())


def engine_call(session_id: str, op: str, args: dict | None = None,
                timeout_s: float = 20.0) -> dict:
    if os.environ.get("PYRUNNER_URL"):
        out = _remote(f"/engines/{session_id}/call",
                      {"op": op, "args": args or {}, "timeout_s": timeout_s},
                      timeout_s=timeout_s)
        if out.get("error"):
            raise RuntimeError(out["error"])
        return out.get("result")
    if ENGINE_MODE == "inproc":
        return _inproc_call(session_id, op, args, timeout_s)
    return session_call(session_id, op, args, timeout_s)


def engine_alive(session_id: str) -> bool:
    if os.environ.get("PYRUNNER_URL"):
        try:
            return bool(_remote(f"/engines/{session_id}", None, method="GET",
                                timeout_s=5).get("alive"))
        except Exception:                               # noqa: BLE001
            return False
    if ENGINE_MODE == "inproc":
        return _inproc_alive(session_id)
    return session_alive(session_id)


def engine_transport(session_id: str) -> str:
    """Which transport this session trades over: socket / file / dead /
    none -- surfaced into the payload so the audit trail names the path."""
    if os.environ.get("PYRUNNER_URL"):
        try:
            return str(_remote(f"/engines/{session_id}", None, method="GET",
                               timeout_s=5).get("transport") or "unknown")
        except Exception:                               # noqa: BLE001
            return "unknown"
    if ENGINE_MODE == "inproc":
        return "inproc" if _inproc_alive(session_id) else "dead"
    return session_transport(session_id)


def engine_stop(session_id: str) -> None:
    if os.environ.get("PYRUNNER_URL"):
        try:
            _remote(f"/engines/{session_id}", None, method="DELETE", timeout_s=10)
        except Exception:                               # noqa: BLE001
            pass
        return
    if ENGINE_MODE == "inproc":
        _inproc_stop(session_id)
        return
    session_stop(session_id)
