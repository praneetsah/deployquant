"""Alpaca PAPER adapter: the platform's Alpaca adapter, pinned to the
paper environment.

The platform adapter picks its host from creds["paper"] so one class serves
both environments. The open distribution ships the full Alpaca adapter as
`alpaca`; this subclass is the paper-pinned `alpaca-paper` — the safe
default for the CLI (spec 2026-09-18 §6). It forces the flag on every
call: a creds dict carrying live keys, or no flag at all, is still sent to
the paper host, where live keys simply do not authenticate. There is no
code path from here to real money.
"""
from __future__ import annotations

import os

from .alpaca import AlpacaAdapter

ENV_KEY_ID = ("ALPACA_KEY_ID", "APCA_API_KEY_ID")
ENV_SECRET = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

_PINNED = ("ensure_session", "fetch_balance", "positions", "positions_detail",
           "open_orders", "executions", "submit", "replace", "cancel")


def _paper(creds: dict | None) -> dict:
    return {**(creds or {}), "paper": True}


class AlpacaPaperAdapter(AlpacaAdapter):
    id = "alpaca-paper"
    name = "Alpaca (paper)"

    @staticmethod
    def creds_from_env(env: dict | None = None) -> dict:
        """{key_id, secret_key, paper: True} from the environment; raises
        with the variable names when either is missing."""
        env = os.environ if env is None else env
        kid = next((env[k] for k in ENV_KEY_ID if env.get(k)), None)
        sec = next((env[k] for k in ENV_SECRET if env.get(k)), None)
        if not kid or not sec:
            raise KeyError(f"set {ENV_KEY_ID[0]} and {ENV_SECRET[0]} "
                           f"(Alpaca paper keys)")
        return {"key_id": kid, "secret_key": sec, "paper": True}


def _pin(name):
    inherited = getattr(AlpacaAdapter, name)

    def method(self, creds, *args, **kwargs):
        return inherited(self, _paper(creds), *args, **kwargs)
    method.__name__ = name
    method.__doc__ = inherited.__doc__
    return method


for _name in _PINNED:
    setattr(AlpacaPaperAdapter, _name, _pin(_name))
