"""pyrunner HTTP service — the prod home of run_sandboxed().

In prod, ONLY this service's container mounts /var/run/docker.sock; the
public API never holds it (an API compromise must not equal host root).
It listens on the internal network exclusively and requires a bearer token
(defense in depth, PYRUN_SVC_TOKEN).

Run:  uvicorn dqengine.sandbox.service:app --host 0.0.0.0 --port 8470
Env:  PYRUN_SVC_TOKEN (required in prod), plus pyrunner's PYRUN_* knobs.
"""
import os
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import pyrunner

app = FastAPI(title="pyrunner", docs_url=None, redoc_url=None)

DATA_DIR = os.environ.get("PYRUN_DATA_DIR", "/pydata")

# ---- sandbox-image self-provisioning -------------------------------------
# This service is the only thing holding docker.sock, so it also owns making
# sure the sandbox image exists on the host: on startup (PYRUN_ENSURE_IMAGE=1)
# it builds PYRUN_IMAGE from the context baked into this image at
# pyrunner.SANDBOX_CONTEXT -- /srv/sandbox-context unless PYRUN_SANDBOX_CONTEXT
# says otherwise (dqengine/sandbox/Dockerfile.pyrunner at its root, the
# whole open distribution under platform/engine; Dockerfile.pyrunner_svc). The
# first build pulls the ~14GB foundation base — that runs in a background
# thread, and /health reports progress; /run returns 503 until ready.

_image_state = {"status": "unknown", "detail": ""}


def _image_exists() -> bool:
    r = subprocess.run(["docker", "image", "inspect", pyrunner.PYRUN_IMAGE],
                       capture_output=True)
    return r.returncode == 0


def _ensure_image():
    """ALWAYS build at startup — the context baked into this image carries
    the current dqengine.runtime, and skipping the build when a tag merely exists
    served a stale sandbox after deploys (2026-08-28: live-progress shipped
    but the old image kept running). With the layer cache an unchanged
    build is seconds; only the first-ever run pulls the ~14GB base."""
    try:
        had = _image_exists()
        _image_state.update(status="building",
                            detail="building sandbox image (cached: seconds; "
                                   "first ever run pulls the ~14GB base)")
        r = subprocess.run(
            ["docker", "build", "-t", pyrunner.PYRUN_IMAGE,
             pyrunner.SANDBOX_CONTEXT],
            capture_output=True, text=True, timeout=7200)
        if r.returncode == 0 and _image_exists():
            _image_state.update(status="ready", detail="built")
            pyrunner.ensure_pool(DATA_DIR)     # pre-warm sandboxes (env-gated)
        elif had:
            # a failed rebuild must not take the feature down — serve the
            # previous image and say so
            _image_state.update(
                status="ready",
                detail="REBUILD FAILED — serving the previous image: "
                       + (r.stderr or r.stdout or "")[-800:])
        else:
            _image_state.update(status="error",
                                detail=(r.stderr or r.stdout or "")[-1500:])
    except Exception as e:  # noqa: BLE001 — surfaced via /health
        _image_state.update(status="error", detail=str(e)[:1500])


_rebuild_lock = threading.Lock()


def _kick_rebuild():
    """One background rebuild at a time; callers gate on _image_state."""
    def _go():
        if not _rebuild_lock.acquire(blocking=False):
            return
        try:
            _ensure_image()
        finally:
            _rebuild_lock.release()
    threading.Thread(target=_go, daemon=True).start()


def _pool_heartbeat():
    """Keep the warm pool topped up around the clock (env-gated inside
    ensure_pool). A running warm container also REFERENCES the sandbox
    image, which shields it from the host's nightly forced docker
    cleanup — the 2026-08-30 outage was the image being pruned while the
    pool sat empty."""
    every = int(os.environ.get("PYRUN_POOL_HEARTBEAT_S", "60"))
    while True:
        time.sleep(every)
        try:
            if _image_state["status"] == "ready" and _image_exists():
                pyrunner.ensure_pool(DATA_DIR)
        except Exception:  # noqa: BLE001 — the heartbeat must survive anything
            pass


@app.on_event("startup")
def _startup():
    print(f"[pyrunner] engine transport: {pyrunner.ENGINE_TRANSPORT} "
          f"(PYRUN_ENGINE_TRANSPORT; prod must be 'socket')", flush=True)
    # engine containers from a previous svc process: no registry entry, but
    # running and holding reservations until their idle timeout
    try:
        n = pyrunner.kill_labelled()
        if n:
            print(f"[pyrunner] swept {n} orphaned engine container(s)", flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[pyrunner] orphan sweep failed: {e}", flush=True)
    if os.environ.get("PYRUN_ENSURE_IMAGE") == "1":
        _kick_rebuild()
    else:
        _image_state.update(status="ready" if _image_exists() else "missing")
    threading.Thread(target=_pool_heartbeat, daemon=True).start()


class RunIn(BaseModel):
    code: str
    run_cfg: dict
    timeout_s: int | None = None


def _check_auth(request: Request):
    token = os.environ.get("PYRUN_SVC_TOKEN")
    if not token:
        return  # local dev
    if request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/health")
def health():
    return {"ok": True, "image": pyrunner.PYRUN_IMAGE,
            "image_status": _image_state["status"],
            "detail": _image_state["detail"],
            "transport": pyrunner.transport_health()}


def _gate_image():
    # self-heal: the host's nightly forced docker cleanup can prune the
    # locally-built image while state still says "ready" (2026-08-30
    # incident: every run died with exit 125 / "unable to find image").
    # One `docker image inspect` (~30ms) per run start buys the recheck.
    if _image_state["status"] == "ready" and not _image_exists():
        _image_state.update(status="building",
                            detail="sandbox image was pruned — rebuilding")
        _kick_rebuild()
    if _image_state["status"] == "building":
        raise HTTPException(status_code=503,
                            detail="python runner is still preparing its "
                                   "sandbox image — try again in a few minutes")
    if _image_state["status"] in ("error", "missing"):
        raise HTTPException(
            status_code=503,
            detail=f"python runner's sandbox image is unavailable "
                   f"({_image_state['status']}): "
                   f"{_image_state['detail'][:300]}")


@app.post("/runs")
def start(inp: RunIn, request: Request):
    """Live-progress protocol: start a run, poll GET /runs/{id} for
    snapshots and the final result."""
    _check_auth(request)
    _gate_image()
    if len(inp.code) > 200_000:
        raise HTTPException(status_code=413, detail="code too large (200KB max)")
    return {"run_id": pyrunner.start_run(inp.code, inp.run_cfg,
                                         data_dir=DATA_DIR,
                                         timeout_s=inp.timeout_s)}


@app.get("/runs/{run_id}")
def poll(run_id: str, request: Request):
    _check_auth(request)
    return pyrunner.poll_run(run_id)


@app.post("/run")
def run(inp: RunIn, request: Request):
    """Blocking variant — the manifest pass uses this; backtests stream
    via POST /runs."""
    _check_auth(request)
    _gate_image()
    if len(inp.code) > 200_000:
        raise HTTPException(status_code=413, detail="code too large (200KB max)")
    return pyrunner.run_sandboxed(inp.code, inp.run_cfg, data_dir=DATA_DIR,
                                  timeout_s=inp.timeout_s)


# ---- long-lived engine sessions (the sub-second live path) -----------------

class EngineStartIn(BaseModel):
    code: str
    run_cfg: dict


class EngineCallIn(BaseModel):
    op: str
    args: dict = {}
    timeout_s: float = 20.0


@app.post("/engines/{session_id}")
def engine_start(session_id: str, inp: EngineStartIn, request: Request):
    _check_auth(request)
    _gate_image()
    if len(inp.code) > 200_000:
        raise HTTPException(status_code=413, detail="code too large (200KB max)")
    try:
        return pyrunner.session_start(session_id, inp.code, inp.run_cfg,
                                      data_dir=DATA_DIR)
    except Exception as e:                              # noqa: BLE001
        print(f"[pyrunner] engine start FAILED session={session_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/engines/{session_id}/call")
def engine_call(session_id: str, inp: EngineCallIn, request: Request):
    _check_auth(request)
    try:
        return {"result": pyrunner.session_call(session_id, inp.op, inp.args,
                                                inp.timeout_s)}
    except Exception as e:                              # noqa: BLE001
        return {"error": str(e)}


@app.get("/engines/{session_id}")
def engine_status(session_id: str, request: Request):
    _check_auth(request)
    return {"alive": pyrunner.session_alive(session_id),
            "transport": pyrunner.session_transport(session_id),
            "fallback": pyrunner.TRANSPORT_FALLBACKS.get(session_id)}


@app.delete("/engines/{session_id}")
def engine_stop(session_id: str, request: Request):
    _check_auth(request)
    pyrunner.session_stop(session_id)
    return {"ok": True}
