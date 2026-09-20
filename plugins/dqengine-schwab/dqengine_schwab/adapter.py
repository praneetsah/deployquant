"""Charles Schwab Trader API adapter. Live-money only (Schwab has no paper
environment) — the executor's live gate applies. No client order ids on this
API: attribution happens in the BrokerOrder audit table at record time."""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dqengine.adapters import base
from dqengine.adapters.base import (BrokerAdapter, BrokerAuthExpired, BrokerRejected,
                   BrokerUnavailable, Caps, ExecutionBatch,
                   normalize_execution)

from . import oauth

BASE = "https://api.schwabapi.com/trader/v1"
logger = logging.getLogger(__name__)

# Schwab reports rejection ASYNCHRONOUSLY: a bad order still returns 201 with
# a Location header, then lands in the order list as REJECTED. Anything not in
# this set is still live and must be visible to the executor.
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}


def _req(creds, method, path, body=None, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {creds.get('access_token', '')}"}
    if body is not None:
        # Schwab 400s ("Internal Server Error") on any GET that carries
        # Content-Type — send it only when there is actually a body.
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            headers = dict(r.headers) if hasattr(r, "headers") else {}
            return (json.loads(raw) if raw else None), headers
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code == 401:
            raise BrokerAuthExpired("Schwab access token rejected")
        if e.code in (400, 422):
            raise BrokerRejected(f"Schwab {e.code}: {detail}")
        raise BrokerUnavailable(f"Schwab {e.code}: {detail}")
    except (BrokerAuthExpired, BrokerRejected, BrokerUnavailable):
        raise
    except Exception as e:
        raise BrokerUnavailable(f"could not reach Schwab: {e}")


def _hash(creds):
    if not creds.get("account_hash"):
        raise BrokerUnavailable("Schwab account hash missing — reconnect")
    return creds["account_hash"]


def _norm(o: dict) -> dict:
    legs = o.get("orderLegCollection") or [{}]
    leg = legs[0]
    instruction = (leg.get("instruction") or "").upper()
    # Schwab carries the trailing offset in stopPriceOffset, but only when
    # stopPriceLinkType is "PERCENT" — it can also be "VALUE" (a dollar
    # offset) or "TICK", which are not a trail_percent and must not be
    # mapped as one.
    trail_percent = None
    if (o.get("stopPriceLinkType") or "").upper() == "PERCENT" \
            and o.get("stopPriceOffset") is not None:
        trail_percent = float(o["stopPriceOffset"])
    return {"id": str(o.get("orderId", "")),
            "symbol": ((leg.get("instrument") or {}).get("symbol") or "").upper(),
            "qty": float(leg.get("quantity") or 0),
            "side": "buy" if "BUY" in instruction else "sell",
            "type": (o.get("orderType") or "").lower(),
            "limit_price": (float(o["price"]) if o.get("price") is not None
                            else None),
            "status": o.get("status", ""), "client_order_id": "",
            "stop_price": (float(o["stopPrice"])
                           if o.get("stopPrice") is not None else None),
            "trail_percent": trail_percent}


# normalized order type -> Schwab orderType. Every one of these was verified
# accepted by the live API on 2026-08-15 (see the hosted platform's
# api/scripts/probe_schwab_ordertypes.py).
_TYPE = {
    base.MARKET: "MARKET",
    base.LIMIT: "LIMIT",
    base.STOP: "STOP",
    base.STOP_LIMIT: "STOP_LIMIT",
    base.TRAILING_STOP: "TRAILING_STOP",
    base.MARKET_ON_CLOSE: "MARKET_ON_CLOSE",
    base.LIMIT_ON_CLOSE: "LIMIT_ON_CLOSE",
}
# IMMEDIATE_OR_CANCEL / END_OF_WEEK / END_OF_MONTH are rejected as
# "Invalid value" by Schwab, so they are absent from Caps.tifs.
_DURATION = {"day": "DAY", "gtc": "GOOD_TILL_CANCEL", "fok": "FILL_OR_KILL"}


def _order_body(symbol, qty, side, order_type, tif, limit_price,
                stop_price=None, trail_percent=None, extended_hours=False):
    body = {
        "orderType": _TYPE[order_type],
        # SEAMLESS routes pre/post-market as well as regular hours
        "session": "SEAMLESS" if extended_hours else "NORMAL",
        "duration": _DURATION[tif],
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "BUY" if side == "buy" else "SELL",
            "quantity": abs(int(qty)) if abs(qty) >= 1 else abs(qty),
            "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
        }],
    }
    if order_type in base.NEEDS_LIMIT_PRICE:
        body["price"] = str(round(limit_price, 2))     # Schwab wants a string
    if order_type in base.NEEDS_STOP_PRICE:
        body["stopPrice"] = str(round(stop_price, 2))
    if order_type == base.TRAILING_STOP:
        # UNITS: stopPriceOffset with stopPriceLinkType "PERCENT" is a
        # PERCENT (5 == 5%) — the same unit adapters take, so NO CONVERSION.
        # Stated because it is not self-evident and the neighbouring venue
        # disagrees: Webull's trailing_stop_step is a fraction (0.01 == 1%),
        # and getting it wrong is a protective exit 100x from where the
        # strategy asked for it.
        body["stopPriceLinkBasis"] = "BID" if side == "sell" else "ASK"
        body["stopPriceLinkType"] = "PERCENT"
        body["stopPriceOffset"] = trail_percent
    return body


class SchwabAdapter(BrokerAdapter):
    id = "schwab"
    name = "Charles Schwab"
    # every order type / duration / session below was verified against the
    # live Trader API on 2026-08-15
    caps = Caps(auth_kind="oauth", paper=False, supports_replace=True,
                supports_client_order_id=False, supports_short=False,
                qty_step=1.0,
                order_types=frozenset(_TYPE),
                tifs=frozenset(_DURATION),
                extended_hours=True)

    def ensure_session(self, creds):
        if creds.get("access_token") and \
                time.time() < float(creds.get("access_expires_at", 0)):
            updated = self._ensure_account_hash(creds)
            return updated
        tok = oauth.schwab_refresh(
            creds.get("app_key", ""), creds.get("app_secret", ""),
            creds.get("refresh_token", ""))
        creds = dict(creds)
        creds.update({
            "access_token": tok.get("access_token", ""),
            "refresh_token": tok.get("refresh_token")
            or creds.get("refresh_token", ""),
            "access_expires_at": time.time() + tok.get("expires_in", 1800) - 120,
        })
        self._ensure_account_hash(creds)
        return creds

    def _ensure_account_hash(self, creds):
        """Resolve and cache the account hash on first use. Mutates and
        returns creds when it had to fetch; returns None otherwise."""
        if creds.get("account_hash"):
            return None
        rows, _ = _req(creds, "GET", "/accounts/accountNumbers")
        if not rows:
            raise BrokerUnavailable("no Schwab accounts visible to this app")
        pick = rows[0]
        if creds.get("account_number"):
            for r in rows:
                if r.get("accountNumber") == creds["account_number"]:
                    pick = r
        creds["account_hash"] = pick.get("hashValue", "")
        creds["account_number"] = pick.get("accountNumber", "")
        return creds

    def fetch_balance(self, creds):
        acct, _ = _req(creds, "GET", f"/accounts/{_hash(creds)}")
        sa = acct.get("securitiesAccount", {})
        bal = sa.get("currentBalances", {})
        return {
            "equity": round(float(bal.get("liquidationValue", 0)), 2),
            "cash": round(float(bal.get("cashBalance", 0)), 2),
            "buying_power": round(float(bal.get("buyingPower",
                                                bal.get("cashBalance", 0))), 2),
            "history": {"days": [], "values": []},   # Schwab has no equity-curve API
            "account_label":
                f"Schwab · ...{str(creds.get('account_number', ''))[-4:]}",
            "currency": "USD",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def positions(self, creds):
        acct, _ = _req(creds, "GET", f"/accounts/{_hash(creds)}",
                       params={"fields": "positions"})
        out = {}
        for p in (acct.get("securitiesAccount", {}).get("positions") or []):
            sym = ((p.get("instrument") or {}).get("symbol") or "").upper()
            if not sym:
                continue
            qty = float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
            if qty:
                out[sym] = out.get(sym, 0.0) + qty
        return out

    def open_orders(self, creds):
        """Every not-yet-terminal order, NOT just status=WORKING. An order
        placed outside market hours rests as PENDING_ACTIVATION/QUEUED and a
        WORKING filter misses it — the executor would then not see its own
        in-flight order and would submit a duplicate on the next sweep."""
        now = datetime.now(timezone.utc)
        rows, _ = _req(creds, "GET", f"/accounts/{_hash(creds)}/orders",
                       params={"fromEnteredTime":
                               (now - timedelta(days=364)).strftime(
                                   "%Y-%m-%dT%H:%M:%S.000Z"),
                               "toEnteredTime":
                               now.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
        return [_norm(o) for o in (rows or [])
                if (o.get("status") or "").upper() not in TERMINAL_STATUSES]

    def executions(self, creds, since=None):
        """Schwab exposes real execution legs (not just an order-level
        average): GET .../orders returns each order with
        orderActivityCollection[].executionLegs[] carrying {price, quantity,
        time, legId}. A multi-leg fill (partial fills across venues) yields
        one row per leg here — the granularity Schwab actually reports."""
        frm = (since or (datetime.now(timezone.utc) - timedelta(days=7)))
        rows, _ = _req(creds, "GET",
                       f"/accounts/{_hash(creds)}/orders",
                       params={"fromEnteredTime":
                               frm.astimezone(timezone.utc).strftime(
                                   "%Y-%m-%dT%H:%M:%S.000Z"),
                               "toEnteredTime":
                               datetime.now(timezone.utc).strftime(
                                   "%Y-%m-%dT%H:%M:%S.000Z")})
        rows = rows or []
        out = []
        skipped = 0
        for o in rows:
            legs = o.get("orderLegCollection") or [{}]
            sym = ((legs[0].get("instrument") or {}).get("symbol") or "")
            instr = str(legs[0].get("instruction") or "").upper()
            if not sym or not instr.startswith(("BUY", "SELL")):
                continue
            side = "buy" if instr.startswith("BUY") else "sell"
            for ai, act in enumerate(o.get("orderActivityCollection") or []):
                # I5: `legId` identifies the order LEG, not the execution.
                # A single-leg order that fills across two activity entries
                # reports legId=0 twice, so an id of orderId:legId collided
                # and the (connection_id, broker_exec_id) unique constraint
                # SILENTLY discarded the second execution -- half a real
                # fill lost without a trace, on exactly the partial-fill
                # case per-execution rows exist for. Scope the id by the
                # activity: Schwab's own activityId where it sends one
                # (stable across polls even if the collection is ever
                # reordered), the positional index otherwise.
                act_id = act.get("activityId")
                act_key = act_id if act_id not in (None, "") else f"a{ai}"
                for leg in (act.get("executionLegs") or []):
                    ts = str(leg.get("time") or "")
                    if not ts:
                        skipped += 1           # unusable without a fill time
                        continue
                    try:
                        # Schwab stamps "+0000" (no colon); fromisoformat on
                        # 3.12 accepts it, but normalize "Z" too for older
                        # shapes.
                        when = datetime.fromisoformat(
                            ts.replace("Z", "+00:00"))
                        out.append(normalize_execution(
                            broker_order_id=o.get("orderId"),
                            broker_exec_id=f"{o.get('orderId')}:{act_key}:"
                                           f"{leg.get('legId')}",
                            symbol=sym, side=side, qty=leg.get("quantity"),
                            price=leg.get("price"), filled_at=when))
                    except (ValueError, TypeError) as exc:
                        # A single malformed leg must never discard the
                        # rest of a real fill batch.
                        skipped += 1
                        logger.warning(
                            "schwab executions: skipping malformed "
                            "executionLeg orderId=%r legId=%r: %s",
                            o.get("orderId"), leg.get("legId"), exc)
        if skipped:
            logger.warning(
                "schwab executions: skipped %d malformed execution "
                "leg(s)", skipped)
        out.sort(key=lambda r: r["filled_at"])
        # I6: see AlpacaAdapter.executions -- a leg we could not parse is
        # UNKNOWN downstream, never a confirmed no-fill.
        return ExecutionBatch(out, skipped=skipped)

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        base.validate_order(self.caps, order_type, tif, limit_price,
                            stop_price, trail_percent, qty, extended_hours,
                            self.name, opens_short=opens_short)
        # Schwab's instruction vocabulary distinguishes SELL_SHORT and
        # BUY_TO_COVER from plain SELL/BUY. Nothing here emits them yet, and
        # Caps.supports_short is False, so validate_order refuses an opening
        # short above before this point is reached. Threading opens_short
        # into _order_body is the work required to lift that flag.
        body = _order_body(symbol, qty, side, order_type, tif, limit_price,
                           stop_price, trail_percent, extended_hours)
        _, headers = _req(creds, "POST", f"/accounts/{_hash(creds)}/orders",
                          body=body)
        loc = headers.get("Location", "") or headers.get("location", "")
        oid = loc.rstrip("/").rsplit("/", 1)[-1] if loc else ""
        # 201 means "order created", NOT "order accepted" — Schwab rejects
        # asynchronously (verified live: an unfundable order returned 201 and
        # then showed up REJECTED). Read the real status back so the audit
        # trail and the executor see what actually happened.
        status = "PENDING_ACTIVATION"
        if oid:
            try:
                detail, _ = _req(creds, "GET",
                                 f"/accounts/{_hash(creds)}/orders/{oid}")
                status = (detail or {}).get("status") or status
            except (BrokerUnavailable, BrokerRejected):
                pass                      # keep the optimistic status
        return {"id": oid, "symbol": symbol.upper(), "qty": float(abs(int(qty))),
                "side": side, "type": order_type,
                "limit_price": (round(limit_price, 2)
                                if limit_price is not None else None),
                "status": status, "client_order_id": ""}

    def replace(self, creds, order_id, qty=None, limit_price=None):
        # Schwab replace = PUT a full order body; fetch current, merge, PUT
        rows, _ = _req(creds, "GET",
                       f"/accounts/{_hash(creds)}/orders/{order_id}")
        cur = _norm(rows or {})
        new_qty = qty if qty is not None else cur["qty"]
        new_px = limit_price if limit_price is not None else cur["limit_price"]
        body = _order_body(cur["symbol"], new_qty, cur["side"],
                           cur["type"] or "limit", "gtc", new_px)
        _, headers = _req(creds, "PUT",
                          f"/accounts/{_hash(creds)}/orders/{order_id}",
                          body=body)
        loc = headers.get("Location", "") or headers.get("location", "")
        oid = loc.rstrip("/").rsplit("/", 1)[-1] if loc else str(order_id)
        return {"id": oid, "symbol": cur["symbol"], "qty": float(new_qty),
                "side": cur["side"], "type": cur["type"] or "limit",
                "limit_price": new_px, "status": "PENDING_ACTIVATION",
                "client_order_id": ""}

    def cancel(self, creds, order_id):
        try:
            _req(creds, "DELETE",
                 f"/accounts/{_hash(creds)}/orders/{order_id}")
        except (BrokerUnavailable, BrokerRejected):
            pass
