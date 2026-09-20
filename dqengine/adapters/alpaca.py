"""Alpaca adapter. Direct port of the original brokers.py functions — the
executor's Alpaca behavior must be byte-identical to before.

Paper vs live is decided by the CREDENTIALS, not by the adapter: Alpaca issues
separate key pairs for its paper and live environments, served by different
hosts. The host is therefore chosen per connection from creds["paper"], which
add_broker sets from the mode the user picked at connect time. Hardcoding the
paper host (as this adapter originally did) meant live keys were silently sent
to the paper environment — where they don't authenticate — and, worse, that a
real-money Alpaca account would have been recorded as mode "paper" and so
skipped the live-trading confirmation gate entirely."""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import base
from .base import (BrokerAdapter, BrokerAuthExpired, BrokerRejected,
                   BrokerUnavailable, Caps, ExecutionBatch,
                   normalize_execution)

logger = logging.getLogger(__name__)

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
BASE = PAPER_BASE                      # back-compat for any stale import


def _base(creds) -> str:
    """Default to paper when the flag is absent: an older connection stored
    before this setting existed was, by construction, a paper one."""
    return LIVE_BASE if creds.get("paper") is False else PAPER_BASE


def _req(creds, method, path, body=None, params=None):
    url = f"{_base(creds)}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"APCA-API-KEY-ID": creds.get("key_id", ""),
               "APCA-API-SECRET-KEY": creds.get("secret_key", "")}
    if body is not None:
        # only with a body — the original brokers.py GET helper sent no
        # Content-Type, and Schwab actively rejects it on GETs
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise BrokerAuthExpired(
                "Alpaca rejected these keys — check them and reconnect")
        if e.code in (400, 422):
            raise BrokerRejected(f"Alpaca {e.code}: {detail}")
        raise BrokerUnavailable(f"Alpaca {e.code}: {detail}")
    except BrokerUnavailable:
        raise
    except Exception as e:
        raise BrokerUnavailable(f"could not reach Alpaca: {e}")


def _norm_order(o: dict) -> dict:
    return {"id": o.get("id", ""), "symbol": (o.get("symbol") or "").upper(),
            "qty": float(o.get("qty") or 0), "side": o.get("side", ""),
            "type": o.get("type", ""),
            "limit_price": (float(o["limit_price"])
                            if o.get("limit_price") is not None else None),
            "status": o.get("status", ""),
            "client_order_id": o.get("client_order_id") or "",
            # Alpaca returns both fields directly on the order object for
            # stop/stop_limit/trailing_stop orders; trail_percent is already
            # a percent string (e.g. "5"), not a fraction.
            "stop_price": (float(o["stop_price"])
                           if o.get("stop_price") is not None else None),
            "trail_percent": (float(o["trail_percent"])
                              if o.get("trail_percent") is not None else None)}


class AlpacaAdapter(BrokerAdapter):
    id = "alpaca"
    name = "Alpaca"
    # Alpaca's own order-type names match the normalized ones exactly. It has
    # no MARKET_ON_CLOSE/LIMIT_ON_CLOSE type — the equivalent is tif "cls".
    # Declared from Alpaca's API docs; not yet re-verified live post-refactor.
    caps = Caps(auth_kind="keys", paper=True, supports_replace=True,
                supports_client_order_id=True, supports_short=True,
                qty_step=1.0,
                order_types=frozenset({base.MARKET, base.LIMIT, base.STOP,
                                       base.STOP_LIMIT, base.TRAILING_STOP}),
                tifs=frozenset({"day", "gtc", "ioc", "fok", "opg", "cls"}),
                extended_hours=True)

    def fetch_balance(self, creds):
        acct = _req(creds, "GET", "/v2/account")
        hist = _req(creds, "GET", "/v2/account/portfolio/history",
                    params={"period": "3M", "timeframe": "1D"})
        days, values = [], []
        for ts, eq in zip(hist.get("timestamp", []), hist.get("equity", [])):
            if eq is None:
                continue
            days.append(datetime.fromtimestamp(ts, tz=timezone.utc)
                        .date().isoformat())
            values.append(round(float(eq), 2))
        return {
            "equity": round(float(acct.get("equity", 0)), 2),
            "cash": round(float(acct.get("cash", 0)), 2),
            "buying_power": round(float(acct.get("buying_power", 0)), 2),
            "history": {"days": days, "values": values},
            "account_label":
                ("Alpaca paper · " if creds.get("paper") is not False
                 else "Alpaca · ") + str(acct.get("account_number", "")),
            "currency": "USD",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def positions(self, creds):
        rows = _req(creds, "GET", "/v2/positions") or []
        return {(p.get("symbol") or "").upper(): float(p.get("qty", 0))
                for p in rows}

    def open_orders(self, creds):
        rows = _req(creds, "GET", "/v2/orders",
                    params={"status": "open", "limit": 200}) or []
        return [_norm_order(o) for o in rows]

    # Alpaca's account-activities feed pages via a cursor: pass page_token =
    # the "id" of the last activity in the previous page to fetch the next
    # one; a page shorter than page_size marks the end. MAX_PAGES bounds a
    # pathological account (e.g. a corrupted cursor looping forever) rather
    # than looping unbounded.
    MAX_EXEC_PAGES = 50

    def executions(self, creds, since=None):
        # direction="asc" must be explicit: Alpaca defaults to "desc"
        # (newest first), under which page_token walks backward from now.
        # If MAX_EXEC_PAGES ever trips under desc, it truncates the OLDEST
        # rows — exactly the ones a since=None backfill exists to recover,
        # and unrecoverably so. Under asc, a tripped cap instead drops the
        # newest rows, which the next incremental poll re-fetches via
        # `since` — a recoverable failure instead of a silent, permanent one.
        params = {"page_size": 100, "direction": "asc"}
        if since is not None:
            # Alpaca's accepted formats are YYYY-MM-DD or
            # YYYY-MM-DDTHH:MM:SSZ; isoformat()'s "+00:00" suffix (and any
            # microseconds) is not documented as accepted, so format
            # explicitly.
            params["after"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        out = []
        skipped = 0
        page_token = None
        hit_cap = True
        for _ in range(self.MAX_EXEC_PAGES):
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            rows = _req(creds, "GET", "/v2/account/activities/FILL",
                        params=page_params) or []
            if not rows:
                hit_cap = False
                break
            for a in rows:
                ts = str(a.get("transaction_time") or "")
                if not ts:
                    skipped += 1               # unusable without a fill time
                    continue
                try:
                    # Alpaca stamps UTC with a trailing "Z"; fromisoformat
                    # wants "+00:00".
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    out.append(normalize_execution(
                        broker_order_id=a.get("order_id"),
                        broker_exec_id=a.get("id"), symbol=a.get("symbol"),
                        side=a.get("side"), qty=a.get("qty"),
                        price=a.get("price"), filled_at=when))
                except (ValueError, TypeError) as exc:
                    # A single malformed activity (bad side/qty/price/etc.)
                    # must never discard the rest of a real fill batch.
                    skipped += 1
                    logger.warning(
                        "alpaca executions: skipping malformed FILL "
                        "activity id=%r: %s", a.get("id"), exc)
            page_token = rows[-1].get("id")
            if len(rows) < params["page_size"] or not page_token:
                hit_cap = False
                break
        if hit_cap:
            logger.warning(
                "alpaca executions: hit page cap (%d pages); results may "
                "be truncated", self.MAX_EXEC_PAGES)
        if skipped:
            logger.warning(
                "alpaca executions: skipped %d malformed FILL activity "
                "row(s)", skipped)
        out.sort(key=lambda r: r["filled_at"])
        # I6: the skip count rides back with the rows. A row we could not
        # parse is UNKNOWN downstream, never "the broker did not fill it".
        return ExecutionBatch(out, skipped=skipped)

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        # Alpaca needs no distinct side value for a short — a sell from flat
        # opens one — but opens_short must still reach validate_order, or
        # Caps.supports_short is unenforceable here.
        base.validate_order(self.caps, order_type, tif, limit_price,
                            stop_price, trail_percent, qty, extended_hours,
                            self.name, opens_short=opens_short)
        body = {"symbol": symbol.upper(), "qty": str(abs(int(qty))),
                "side": side, "type": order_type, "time_in_force": tif}
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        if stop_price is not None:
            body["stop_price"] = str(round(stop_price, 2))
        if trail_percent is not None:
            body["trail_percent"] = str(trail_percent)
        if extended_hours:
            body["extended_hours"] = True
        if client_order_id:
            body["client_order_id"] = client_order_id[:128]
        return _norm_order(_req(creds, "POST", "/v2/orders", body=body))

    def replace(self, creds, order_id, qty=None, limit_price=None):
        body = {}
        if qty is not None:
            body["qty"] = str(abs(int(qty)))
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        return _norm_order(
            _req(creds, "PATCH", f"/v2/orders/{order_id}", body=body))

    def cancel(self, creds, order_id):
        try:
            _req(creds, "DELETE", f"/v2/orders/{order_id}")
        except (BrokerUnavailable, BrokerRejected):
            pass
