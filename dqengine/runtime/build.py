"""Construct a WarmPyEngine from a `serve` run.json -- the ONE place.

The sandbox server (engine_server.EngineServer.build, data_root=/data
inside the container) and the in-process mode (inproc.InProcSession,
data_root = the host's bar store) both build here, so an engine on either
path is constructed from the same fields with the same ledger semantics:
`ledger` rows become the SAME ExecutionLedger the IR engine uses, capped
to LiveCappedLedger when `live_from` is present; `bar_ms`, `generated`
and the start/end/cash overrides are read identically.

No store selection lives here: WarmPyEngine builds DataStore(data_root).
Second resolution stays "not routable" (spec §8) until both this and the
replay carry the HybridStore config.
"""
from __future__ import annotations

from datetime import date


def ledger_from_payload(led):
    if led is None:
        return None
    from dqengine.runtime.core.ledger import ExecutionLedger, LedgerFill, LiveCappedLedger
    inner = ExecutionLedger(
        [LedgerFill(day=date.fromisoformat(f["day"]), time_ms=int(f["ms"]),
                    symbol=f["sym"], qty=int(f["qty"]), price=float(f["px"]),
                    fees=float(f.get("fees") or 0.0),
                    rule_tag=f.get("rule_tag"),
                    broker_order_id=f.get("broker_order_id"))
         for f in led.get("fills") or []],
        unknown=set(led.get("unknown") or []),
        reconciled_from=(date.fromisoformat(led["reconciled_from"])
                         if led.get("reconciled_from") else None),
        unknown_from=(date.fromisoformat(led["unknown_from"])
                      if led.get("unknown_from") else None))
    if led.get("live_from"):
        return LiveCappedLedger(inner,
                                live_from=date.fromisoformat(led["live_from"]))
    return inner


def build_engine(cfg: dict, code: str, data_root: str, engine_cls=None):
    """`cfg` is the run.json the driver wrote (dqengine.live.driver.engine's
    _engine_cfg);
    `data_root` is where THIS process reads bars from (/data in the
    sandbox, the host store in-process) -- never taken from cfg, so a
    run.json cannot point an engine at a path of its choosing."""
    if engine_cls is None:
        from .warm import WarmPyEngine as engine_cls
    overrides = {k: cfg[k] for k in ("start", "end", "cash", "project_calendar", "cash_events")
                 if cfg.get(k) is not None}
    return engine_cls(
        code, data_root=data_root,
        overrides=overrides,
        ledger=ledger_from_payload(cfg.get("ledger")),
        bar_ms=int(cfg.get("bar_ms") or 60_000),
        generated=bool(cfg.get("generated")))
