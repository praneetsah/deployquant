"""Where the market data lives (spec 2026-09-18 §6).

One owner for DATA_ROOT. Before this module the private api's main.py
defined it and the sandbox runner, the data exporter, the job queue and
the AI port each did `from main import DATA_ROOT` -- the engine side
depending on the FastAPI app for a path.

Resolution order: the DQENGINE_DATA_ROOT environment variable; else, when
this package is imported from the authors' upstream checkout, that
checkout's curated data tree (three levels above this distribution); else
./data relative to the current directory -- what an installed wheel gets,
and what `dqengine backtest --data ./data` documents.
"""
from __future__ import annotations

import os


def _checkout_data_root():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.abspath(os.path.join(here, "..", "..", "..", "qc", "data"))
    return cand if os.path.isdir(cand) else None


def data_root() -> str:
    env = os.environ.get("DQENGINE_DATA_ROOT", "").strip()
    if env:
        return os.path.abspath(env)
    found = _checkout_data_root()
    if found:
        return found
    return os.path.abspath("data")


DATA_ROOT = data_root()
