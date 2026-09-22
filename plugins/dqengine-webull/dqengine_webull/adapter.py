"""Webull OpenAPI adapter (live/production). Uses the official
webull-python-sdk-trade; the SDK is imported lazily inside _api() so the rest
of the platform never needs it installed. Webull has no order-replace — the
executor's cancel+resubmit fallback covers TP updates (caps.supports_replace
False).

SDK surface verified offline 2026-08-14 against the installed SDK (pip
install webull-python-sdk-trade==0.1.18, which also required manually
installing webull-python-sdk-mdata — an undeclared transitive dependency of
webullsdktrade.api that its module-level `from webullsdkmdata.quotes...
import` needs — plus a Python-3.12-compatible grpcio since the pinned
grpcio==1.51.1 has no py3.12 wheel and fails to build without pkg_resources)
via `dir()` on a real `API` instance with dummy creds. Findings vs. the
original brief, which assumed `order_v2.place_order` /
`order_v2.cancel_order` / `order_v2.list_open_orders`:

  * `order_v2.place_order`/`cancel_order_v2`/`replace_order`/`get_order_detail`
    are documented in the SDK source (webullsdktrade/trade/v2/
    order_operation_v2.py) as "currently available only to individual
    brokerage customers in Webull Japan and institutional brokerage clients
    in Webull Hong Kong. It is not yet available to Webull US brokerage
    customers" — unusable for this adapter's `region="us"` creds.
  * `order_v2` has no `list_open_orders` at all; the nearest v2 equivalent
    (`get_order_history_request`) is a 7-day *history* query, not an
    open-orders list, and takes different params.
  * The real, region-unlocked equivalents live on `api.order` (v1 group),
    hitting generic endpoints (no HK/JP caveat in the source):
      - `order.place_order_v2(account_id, stock_order_dict)` ->
        POST /trade/order/place (NOT `order.place_order`, which is itself
        HK/A-shares-only and takes positional qty/instrument_id/... args,
        not a dict)
      - `order.cancel_order(account_id, client_order_id)` ->
        POST /trade/order/cancel
      - `order.list_open_orders(account_id, page_size=10,
        last_client_order_id=None)` -> GET /trade/orders/list-open
  * `account_v2.get_account_balance(account_id)` and
    `account_v2.get_account_position(account_id)` matched the brief exactly.
  * `order.cancel_order` takes `client_order_id`, not Webull's own numeric
    order id — but `open_orders()` normalizes the broker-native order_id
    into the Order dict's `id` field (per the base contract), which is what
    the executor's normal `cancel(creds, order["id"])` call pattern would
    pass. `cancel()` below resolves this itself (re-lists open orders,
    matches on either id, cancels with the resolved client_order_id) rather
    than pushing the mismatch onto callers.
  * The exact `stock_order` field names/values for `place_order_v2` and the
    response body shapes for open orders could not be confirmed without a
    live API key (SDK request objects blind-copy whatever dict keys are
    given, so they don't self-document the wire schema) — kept as the
    brief's best-effort payload; verify against a real account before first
    live use (deferred smoke task).
  * The installed SDK vendors an ancient requests/urllib3 whose bundled
    `six.py` registers a PEP-302-only (find_module/load_module, no
    find_spec) meta path finder for `six.moves`. Python 3.12 dropped the
    fallback that let the import system call such finders, so a plain
    `import webullsdkcore...` raises `ModuleNotFoundError: ...six.moves` on
    3.12. _patch_legacy_six_finder() below installs a small bridge finder
    (find_spec that delegates to any meta_path entry exposing find_module)
    before importing the SDK, so this adapter works on this repo's Python
    3.12 venv; it's a no-op on interpreters where the SDK's own meta path
    trick already works.
"""
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from dqengine.adapters import base
from dqengine.adapters.base import (BrokerAdapter, BrokerRejected, BrokerUnavailable, Caps,
                   ExecutionBatch, normalize_execution)

SANDBOX_HOST = "api.sandbox.webull.com"     # paper/simulated trading

logger = logging.getLogger(__name__)

_six_shim_installed = False


def _patch_legacy_six_finder():
    """See module docstring. Idempotent; safe to call every _api(). The
    bridge finder is inserted into sys.meta_path once and left there for
    the life of the process — it's a permanent, process-wide import-system
    patch, not scoped to this module or undone afterward."""
    global _six_shim_installed
    if _six_shim_installed:
        return
    import importlib.util

    class _LegacyFinderBridge:
        def find_spec(self, fullname, path, target=None):
            for finder in list(sys.meta_path):
                if finder is self or hasattr(finder, "find_spec"):
                    continue
                fm = getattr(finder, "find_module", None)
                if fm is None:
                    continue
                loader = fm(fullname, path)
                if loader is not None:
                    return importlib.util.spec_from_loader(fullname, loader)
            return None

    sys.meta_path.insert(0, _LegacyFinderBridge())
    _six_shim_installed = True


def _patch_legacy_ssl():
    """The SDK's vendored urllib3 does
    `from ssl import wrap_socket, CERT_NONE, PROTOCOL_SSLv23` in one try/except
    ImportError. ssl.wrap_socket was removed in 3.12, so the whole line fails
    and leaves CERT_NONE/PROTOCOL_SSLv23 unbound too — every HTTPS request then
    dies with `NameError: PROTOCOL_SSLv23`. Restoring the name is enough: the
    only bare wrap_socket call sits in urllib3's fallback SSLContext class,
    which is unreachable when the stdlib provides a real SSLContext.

    Idempotent; permanent and process-wide, like the six bridge above. Also
    repairs the vendored module in place when it was imported before us."""
    import ssl

    if not hasattr(ssl, "wrap_socket"):
        def wrap_socket(sock, keyfile=None, certfile=None, server_side=False,
                        cert_reqs=ssl.CERT_NONE, ssl_version=ssl.PROTOCOL_TLS,
                        ca_certs=None, do_handshake_on_connect=True,
                        suppress_ragged_eofs=True, ciphers=None):
            ctx = ssl.SSLContext(ssl_version)
            ctx.check_hostname = False
            ctx.verify_mode = cert_reqs
            if certfile:
                ctx.load_cert_chain(certfile, keyfile)
            if ca_certs:
                ctx.load_verify_locations(ca_certs)
            if ciphers:
                ctx.set_ciphers(ciphers)
            return ctx.wrap_socket(
                sock, server_side=server_side,
                do_handshake_on_connect=do_handshake_on_connect,
                suppress_ragged_eofs=suppress_ragged_eofs)

        ssl.wrap_socket = wrap_socket

    mod = sys.modules.get(
        "webullsdkcore.vendored.requests.packages.urllib3.util.ssl_")
    if mod is not None:
        for name in ("wrap_socket", "CERT_NONE", "PROTOCOL_SSLv23", "HAS_SNI"):
            if not hasattr(mod, name):
                setattr(mod, name, getattr(ssl, name))


def _payload(resp):
    """SDK responses expose .json(); non-2xx carry error_code/msg."""
    data = resp.json() if hasattr(resp, "json") else resp
    code = getattr(resp, "status_code", 200)
    if code >= 400 or (isinstance(data, dict) and data.get("error_code")):
        msg = (data or {}).get("msg", "") if isinstance(data, dict) else ""
        if code in (401, 403):
            from dqengine.adapters.base import BrokerAuthExpired
            raise BrokerAuthExpired(f"Webull auth failed: {msg}")
        if code in (400, 422) or (isinstance(data, dict)
                                  and data.get("error_code")):
            raise BrokerRejected(f"Webull rejected: {msg}")
        raise BrokerUnavailable(f"Webull error {code}: {msg}")
    return data


def _norm(o: dict) -> dict:
    """Webull nests the leg detail one level down (verified live 2026-08-16):

      {"client_order_id": ..., "order_id": ..., "tif": "GTC",
       "order_type": "LMT", "extended_hours_trading": false,
       "items": [{"symbol": "TQQQ", "qty": "100", "side": "SELL",
                  "order_type": "LIMIT", "limit_price": "55.00",
                  "order_status": "SUBMITTED", "filled_qty": "0"}]}

    so symbol/qty/side/price live in items[0] while the ids and tif live on
    the outer object. Flat lookups are kept as a fallback for the sandbox
    shape."""
    item = (o.get("items") or [{}])[0]

    def pick(key, default=None):
        v = item.get(key)
        return o.get(key, default) if v in (None, "") else v

    qty = pick("qty") or pick("quantity") or 0
    px = pick("limit_price")
    return {"id": str(o.get("order_id", "")),
            "symbol": (pick("symbol") or "").upper(),
            "qty": float(qty or 0),
            "side": (pick("side") or "").lower(),
            # items[] carries the readable type (LIMIT/MARKET); the outer
            # object's is the abbreviated one (LMT/MKT)
            "type": (item.get("order_type") or o.get("order_type") or "").lower(),
            "limit_price": float(px) if px not in (None, "") else None,
            "status": pick("order_status") or o.get("status", "") or "",
            "client_order_id": o.get("client_order_id") or "",
            # The live shape captured in this module's docstring (2026-08-16)
            # is a LIMIT order and carries no stop-price/trailing-offset
            # field anywhere in the outer object or items[] — only
            # limit_price. That only proves it for LIMIT; a resting
            # STOP_LOSS/STOP_LOSS_LIMIT order's payload has not actually
            # been observed and may carry a trigger-price field nobody has
            # captured yet (see the docstring's "could not be confirmed
            # without a live API key" caveat). TRAILING_STOP isn't even in
            # this adapter's Caps yet. Rather than guess a field name for
            # the unobserved stop shape, report these as unknown either
            # way — None stays the correct conservative default whether
            # the field truly doesn't exist or we simply haven't seen it.
            # The executor treats an unknown resting reference as
            # "unchanged" (falling back to a level token embedded in the
            # client_order_id, not coercing to 0) rather than churning a
            # live protective order every sweep.
            "stop_price": None,
            "trail_percent": None}


# Normalized order type -> Webull's own name, established by probing the live
# production API (2026-08-16): "STOP"/"STOP_LIMIT"/"LMT"/"STP" are all refused
# with "Please check and enter the correct order type"; these four are accepted.
_NATIVE_TRAILING = os.environ.get("WEBULL_NATIVE_TRAILING") == "1"

_TYPE = {
    base.MARKET: "MARKET",
    base.LIMIT: "LIMIT",
    base.STOP: "STOP_LOSS",
    base.STOP_LIMIT: "STOP_LOSS_LIMIT",
    base.TRAILING_STOP: "TRAILING_STOP_LOSS",
}
# TRAILING_STOP_LOSS: the 2026-08-16 probe could get no combination of
# trailing_type/trailing_stop_step accepted, and guessed "exit-only". The
# docs (2026-09-05) give a likelier explanation and it was neither of those:
# TRAILING_STOP_LOSS "only supports DAY time in force", and every resting
# order the executor sent was GTC. Two things were also missing outright —
# this type was absent from _TYPE, and submit() never mapped trail_percent
# into the payload at all, so the trailing fields were never sent.
#
# Both are fixed, and the tif is now negotiated per type (Caps.tif_by_type
# -> broker_exec.pick_tif) rather than hard-coded. Still unobserved against
# the live venue; if it is refused again, the next suspect is the
# order.place_order_v2 endpoint rather than these values.

_instrument_ids = {}          # symbol -> Webull instrument_id (process cache)
# Webull resolves instruments per category and 417s on a miss, so an ETF is
# invisible to a US_STOCK lookup. Ordered by how often this platform trades
# each kind; leveraged ETFs (TQQQ) are the common case.
_CATEGORIES = ("US_STOCK", "US_ETF")


class WebullAdapter(BrokerAdapter):
    id = "webull"
    name = "Webull"
    # Verified against the live production API 2026-08-16 (see the order-type
    # probe under the hosted platform's api/scripts/, which uses this
    # adapter). MARKET and tif "day" were refused only for timing
    # reasons while the market was closed ("MARKET_NOT_READY",
    # "DAY_ORDER_NOT_ALLOWED_AFT_CORE_TIME"), not as unsupported features.
    # TRAILING_STOP_LOSS is mapped below and its DAY-only TIF declared, but
    # it has never been observed against the live venue (the 2026-08-16
    # probe was refused for a different reason). Until a 1-share probe
    # confirms it, trailing stops stay EMULATED -- the engine watches the
    # level and the quote fast-path fires it -- exactly as on main. Flip
    # WEBULL_NATIVE_TRAILING=1 after the probe.
    caps = Caps(auth_kind="keys", paper=False, supports_replace=False,
                # side is BUY | SELL | SHORT (docs confirmed 2026-09-05,
                # developer.webull.com/apis/docs/trade-api/stock). submit()
                # emits:
                #     open long / close short -> BUY
                #     close long              -> SELL
                #     OPEN short              -> SHORT
                # A plain SELL does NOT open a short; the side has to say so.
                #
                # Enabled on the account owner's instruction without a live
                # probe. The residual risk is NOT the side value but the
                # ENDPOINT: those docs describe the unified REST shape
                # (new_orders[], symbol/quantity/time_in_force, plus required
                # market/instrument_type/combo_type/support_trading_session),
                # while this adapter calls the SDK's order.place_order_v2,
                # whose shape is instrument_id/qty/tif (see submit(), field
                # names verified live). SHORT is documented for the former;
                # whether the latter accepts it has not been observed. If an
                # opening short is ever rejected, suspect the endpoint before
                # the side value.
                supports_client_order_id=True, supports_short=True,
                qty_step=1.0,
                order_types=(frozenset(_TYPE) if _NATIVE_TRAILING
                             else frozenset(_TYPE) - {base.TRAILING_STOP}),
                tifs=frozenset({"day", "gtc"}),
                # TRAILING_STOP_LOSS is DAY-only at this venue (docs
                # 2026-09-05). Declared here so the executor negotiates it
                # instead of sending GTC and being refused.
                tif_by_type=({base.TRAILING_STOP: frozenset({"day"})}
                             if _NATIVE_TRAILING else {}),
                # MARKET_ON_CLOSE / LIMIT_ON_CLOSE exist on this API but the
                # docs mark them institutional-only, so a retail account
                # cannot use them. Recorded so the platform says "your
                # account does not have it here" rather than the false
                # "this broker has no market-on-close".
                entitlement_by_type={
                    base.MARKET_ON_CLOSE: "institutional-only at Webull",
                    base.LIMIT_ON_CLOSE: "institutional-only at Webull",
                },
                extended_hours=True)

    def _api(self, creds):
        _patch_legacy_six_finder()
        _patch_legacy_ssl()
        try:
            from webullsdkcore.client import ApiClient
            from webullsdktrade.api import API
        except ImportError as e:
            # The SDK is an optional vendor package (install recipe: this
            # plugin's README; the hosted platform's deploy/Dockerfile.api).
            # Surface it as a normal broker outage rather than letting an
            # ImportError escape as an unhandled 500 — callers only ever catch
            # the Broker* family.
            raise BrokerUnavailable(
                "the Webull SDK isn't installed on this server — Webull "
                f"connections can't be reached until it is ({e})")
        region = creds.get("region", "us")
        client = ApiClient(creds.get("app_key", ""),
                           creds.get("app_secret", ""),
                           region_id=region)
        # Paper/simulated credentials only authenticate against the sandbox
        # host; the SDK's endpoints.json only knows the production one, so
        # Production is the default and the only surface that works for US
        # accounts: it serves the v1 /account/* + /trade/* paths. The sandbox
        # host only serves /openapi/* (v2), which Webull's own SDK documents
        # as Japan/Hong-Kong-only — so US paper trading is unavailable, and
        # `paper: True` (or an explicit `endpoint`) is opt-in, not default.
        endpoint = creds.get("endpoint")
        if not endpoint and creds.get("paper", False):
            endpoint = SANDBOX_HOST
        if endpoint:
            client.add_endpoint(region, endpoint)
        return API(client)

    def _call(self, fn, *args, **kwargs):
        """SDK network/client errors don't all surface as HTTP responses with
        .status_code — wrap unexpected exceptions the way the other adapters
        do so the executor sees BrokerUnavailable, not a raw SDK crash.

        Crucially, a failed call usually RAISES rather than returning a
        response, so _payload()'s status mapping never sees it: the SDK's
        ServerException carries .http_status/.error_code instead. Classify off
        those here, or bad credentials get reported as "could not reach
        Webull" — a transient outage — when they're a definitive rejection the
        user must fix. Duck-typed so the SDK stays a lazy import."""
        from dqengine.adapters.base import BrokerAuthExpired
        try:
            return fn(*args, **kwargs)
        except (BrokerRejected, BrokerUnavailable, BrokerAuthExpired):
            raise
        except Exception as e:
            status = getattr(e, "http_status", None)
            code = str(getattr(e, "error_code", "") or "").upper()
            msg = getattr(e, "error_msg", "") or ""
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
            if status in (401, 403) or code in ("UNAUTHORIZED", "FORBIDDEN"):
                raise BrokerAuthExpired(
                    "Webull rejected these credentials"
                    + (f": {msg}" if msg else " (401 unauthorized)"))
            if status == 429:
                raise BrokerUnavailable(f"Webull rate-limited us: {msg or e}")
            if status is not None and 400 <= status < 500:
                # every other 4xx is Webull refusing THIS request (e.g. 417
                # INVALID_SYMBOL for a symbol looked up in the wrong
                # category) — retrying it verbatim can never succeed, so it
                # must not masquerade as a transient outage
                raise BrokerRejected(f"Webull refused the request: {msg or e}")
            if status is None and hasattr(e, "error_code"):
                # ClientException — the SDK itself refused to send (bad
                # parameters), which retrying verbatim will never fix
                raise BrokerRejected(f"Webull refused the request: {e}")
            raise BrokerUnavailable(f"could not reach Webull: {e}")

    def _account_rows(self, api, creds):
        """[{account_id, account_number}] — the two hosts expose this via
        different endpoints (verified live 2026-08-16):
          production: account.get_app_subscriptions() -> /app/subscriptions/list
          sandbox:    account_v2.get_account_list()   -> /openapi/account/list
        The v2 path 404s on production and the v1 path 404s on sandbox."""
        if creds.get("paper", False):
            rows = _payload(self._call(api.account_v2.get_account_list))
        else:
            rows = _payload(self._call(api.account.get_app_subscriptions))
        rows = rows.get("data") if isinstance(rows, dict) else rows
        return rows or []

    def ensure_session(self, creds):
        """Webull's trade APIs key off an opaque account_id
        (e.g. GBHG4LR6T97A5LLJ0CM9J82TPA); the user only ever sees the account
        number their app displays (e.g. CVV4MMP5). Resolve it here and hand
        the updated creds back for re-encryption — the same contract the
        Schwab adapter uses for its account hash. Passing the number straight
        through earns a 403 ACCOUNT_ACCESS_DENIED."""
        want = str(creds.get("account_id", "")).strip()
        api = self._api(creds)
        rows = self._account_rows(api, creds)
        if any(str(r.get("account_id", "")) == want for r in rows):
            return None                      # already the opaque id
        for r in rows:
            if str(r.get("account_number", "")).upper() == want.upper():
                creds = dict(creds)
                creds["account_id"] = str(r.get("account_id", ""))
                creds["account_number"] = str(r.get("account_number", ""))
                return creds
        # definitively wrong input, not a transient outage: the app
        # authenticated fine and simply has no such account. BrokerRejected so
        # the connect form refuses it outright instead of storing a pending
        # connection that can never work. (sync_broker_account's catch-all
        # handles this class fine for an already-stored connection.)
        raise BrokerRejected(
            f"Webull account {want!r} not visible to this app — accounts: "
            + ", ".join(str(r.get("account_number", "?")) for r in rows))

    def fetch_balance(self, creds):
        """Live shape (verified 2026-08-16):
        {total_market_value, total_cash_balance,
         account_currency_assets: [{currency, net_liquidation_value,
                                    cash_balance, margin_power, cash_power}]}"""
        api = self._api(creds)
        b = _payload(self._call(api.account.get_account_balance,
                                creds["account_id"], "USD")
                     if not creds.get("paper", False) else
                     self._call(api.account_v2.get_account_balance,
                                creds["account_id"]))
        assets = (b.get("account_currency_assets") or [{}])
        usd = next((a for a in assets if a.get("currency") == "USD"), assets[0])
        equity = usd.get("net_liquidation_value")
        if equity is None:                       # sandbox/v2 fallback shape
            equity = b.get("total_asset") or b.get("total_market_value") or 0
        cash = usd.get("cash_balance", b.get("total_cash_balance", 0))
        power = usd.get("margin_power") or usd.get("cash_power") or cash
        return {
            "equity": round(float(equity), 2),
            "cash": round(float(cash), 2),
            "buying_power": round(float(power), 2),
            "history": {"days": [], "values": []},
            "account_label":
                f"Webull · {creds.get('account_number') or creds.get('account_id', '')[:8]}",
            "currency": "USD",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def positions(self, creds):
        """Live shape (verified 2026-08-16):
        {has_next, holdings: [{symbol, qty, unit_cost, market_value, ...}]}
        Production serves this on account (v1); sandbox on account_v2."""
        api = self._api(creds)
        p = _payload(self._call(api.account_v2.get_account_position,
                                creds["account_id"])
                     if creds.get("paper", False) else
                     self._call(api.account.get_account_position,
                                creds["account_id"], 100))
        out = {}
        for h in (p.get("holdings") or p.get("data") or []):
            sym = (h.get("symbol") or "").upper()
            qty = float(h.get("quantity") or h.get("qty") or 0)
            if sym and qty:
                out[sym] = out.get(sym, 0.0) + qty
        return out

    def positions_detail(self, creds):
        """Webull's holdings carry market_value and unit_cost; positions()
        drops both by contract, so surface them here for diagnostics."""
        api = self._api(creds)
        p = _payload(self._call(api.account_v2.get_account_position,
                                creds["account_id"])
                     if creds.get("paper", False) else
                     self._call(api.account.get_account_position,
                                creds["account_id"], 100))
        out = []
        for h in (p.get("holdings") or p.get("data") or []):
            sym = (h.get("symbol") or "").upper()
            qty = float(h.get("quantity") or h.get("qty") or 0)
            if not sym or not qty:
                continue
            mv = h.get("market_value")
            out.append({"symbol": sym, "qty": qty,
                        "market_value": float(mv) if mv not in (None, "") else None,
                        "unit_cost": (float(h["unit_cost"])
                                      if h.get("unit_cost") not in (None, "")
                                      else None),
                        "last_price": h.get("last_price")})
        return out

    def open_orders(self, creds):
        api = self._api(creds)
        r = _payload(self._call(api.order.list_open_orders,
                                creds["account_id"], page_size=100))
        # live key is "orders"; "data" is the sandbox/older shape
        rows = (r.get("orders") or r.get("data")) if isinstance(r, dict) else r
        return [_norm(o) for o in (rows or [])]

    def executions(self, creds, since=None):
        """Webull reports order-level averages only — no per-execution feed.
        A partially-filled leg therefore arrives as ONE row at its average
        price, flagged order_level_avg so the UI can say so rather than
        implying a precision Webull never gave us.

        TODAY-ONLY, BY CONTRACT. api.order.list_today_orders is the only
        endpoint this adapter has: the natural history endpoint,
        api.order_v2.get_order_history_request, returns HTTP 404 on the
        live production account (verified 2026-08-24) — it is simply not
        available for this SDK/API combination, so it is not called here at
        all, and there is no silent fallback masking that gap. Concretely
        this means a Webull connection's `reconciled_from` can only ever
        start from the day its ledger is switched on, and the backfill's
        `coverage_gaps` will correctly report every earlier day as not
        covered. Do not paper over that by pretending a partial (today-only)
        window is a complete history.

        `list_today_orders` always returns today's orders regardless of
        `since`; `since` is therefore applied here as a client-side filter
        on each item's fill time, same as the other adapters.

        Response shape (real payload, verified live 2026-08-24):
        {"orders": [{"order_id": ..., "client_order_id": ...,
                     "items": [{"symbol", "side", "filled_qty",
                                "filled_price", "last_filled_time",
                                "order_status", "commission",
                                "transaction_fee", ...}, ...]}]}
        The order-level object carries order_id/client_order_id; every
        fill-relevant field (symbol, side, qty, price, time, status, fees)
        lives on the item. An order can carry more than one item (multi-leg
        fills), so broker_exec_id is `f"{order_id}:{index}"` — the bare
        order_id would collide across items and the (connection_id,
        broker_exec_id) unique constraint would silently drop every item
        after the first.

        A CANCELLED/REJECTED order can still carry a real partial fill on
        one of its items, so "is this a fill" is decided per item off its
        own filled_qty/filled_price/last_filled_time — never off the
        order-level order_status."""
        api = self._api(creds)
        r = _payload(self._call(api.order.list_today_orders,
                                creds["account_id"], page_size=100))
        rows = (r.get("orders") or r.get("data")) if isinstance(r, dict) else r
        out = []
        skipped = 0
        for o in (rows or []):
            order_id = o.get("order_id")
            client_order_id = o.get("client_order_id")
            for idx, item in enumerate(o.get("items") or []):
                filled = float(item.get("filled_qty") or 0)
                px = item.get("filled_price")
                ts = item.get("last_filled_time")
                if filled <= 0 or px in (None, "") or not ts:
                    continue              # not a fill — no row, no skip
                try:
                    when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if since is not None and when < since:
                        continue
                    commission = float(item.get("commission") or 0)
                    txn_fee = float(item.get("transaction_fee") or 0)
                    out.append(normalize_execution(
                        broker_order_id=order_id,
                        broker_exec_id=f"{order_id}:{idx}",
                        client_order_id=client_order_id,
                        symbol=item.get("symbol"), side=item.get("side"),
                        qty=filled, price=px, filled_at=when,
                        fees=commission + txn_fee,
                        order_level_avg=True))
                except (ValueError, TypeError) as exc:
                    # A single malformed item must never discard the rest
                    # of a real fill batch — including its siblings in the
                    # same order.
                    skipped += 1
                    logger.warning(
                        "webull executions: skipping malformed item "
                        "order_id=%r index=%d: %s", order_id, idx, exc)
        if skipped:
            logger.warning(
                "webull executions: skipped %d malformed item row(s)",
                skipped)
        out.sort(key=lambda row: row["filled_at"])
        # I6: see AlpacaAdapter.executions -- a row we could not parse is
        # UNKNOWN downstream, never a confirmed no-fill.
        return ExecutionBatch(out, skipped=skipped)

    def _instrument_id(self, api, symbol: str) -> str:
        """Webull places orders by instrument_id, not symbol — sending a
        symbol earns "instrument_id value:null is blank". Resolved once per
        symbol per process.

        The lookup is per-CATEGORY and a wrong one is a hard error, not an
        empty result: TQQQ under US_STOCK returns
        `417 INVALID_SYMBOL: The symbols does not exist in the category`.
        Webull's Category enum separates US_STOCK from US_ETF, so every ETF
        this platform trades (TQQQ, QQQ, SPY, TLT, IWM...) fails a
        stock-only lookup. Try each category and take the first that knows
        the symbol."""
        sym = symbol.upper()
        if sym in _instrument_ids:
            return _instrument_ids[sym]
        refusal = None
        for category in _CATEGORIES:
            try:
                rows = _payload(self._call(api.instrument.get_instrument,
                                           sym, category))
            except BrokerRejected as e:
                refusal = e            # wrong category — try the next one
                continue
            rows = rows.get("data") if isinstance(rows, dict) else rows
            for r in (rows or []):
                if str(r.get("symbol", "")).upper() == sym:
                    _instrument_ids[sym] = str(r.get("instrument_id", ""))
                    return _instrument_ids[sym]
        raise BrokerRejected(
            f"Webull does not list a tradable {sym} in "
            + "/".join(_CATEGORIES) + (f" ({refusal})" if refusal else ""))

    def submit(self, creds, symbol, qty, side, order_type="market",
               tif="day", limit_price=None, stop_price=None,
               trail_percent=None, extended_hours=False,
               client_order_id=None, opens_short=False):
        base.validate_order(self.caps, order_type, tif, limit_price,
                            stop_price, trail_percent, qty, extended_hours,
                            self.name, opens_short=opens_short)
        api = self._api(creds)
        cid = (client_order_id or uuid.uuid4().hex)[:40]
        # field names verified live: qty (not quantity), tif (not
        # time_in_force), instrument_id (not symbol)
        order = {
            "client_order_id": cid,
            "instrument_id": self._instrument_id(api, symbol),
            "order_type": _TYPE[order_type],
            "qty": str(abs(int(qty))),
            # SHORT vs SELL: the trade API documents side as
            # BUY | SELL | SHORT. A sell that OPENS exposure is a short sale
            # and must say so; a sell that reduces or closes is a plain SELL.
            # Sending SHORT for a closing sell would be as wrong as the
            # reverse — see base.validate_order, which gates only opening.
            "side": ("BUY" if side == "buy"
                     else "SHORT" if opens_short else "SELL"),
            "tif": "GTC" if tif == "gtc" else "DAY",
            "entrust_type": "QTY",
            "extended_hours_trading": bool(extended_hours),
        }
        if order_type == base.TRAILING_STOP:
            # UNITS: adapters take trail_percent as a PERCENT (5 == 5%);
            # Webull's trailing_stop_step is a FRACTION (0.01 == 1%). A 100x
            # error here is a protective exit at the wrong distance, so the
            # conversion lives next to the field it feeds.
            order["trailing_type"] = "PERCENTAGE"
            order["trailing_stop_step"] = str(round(trail_percent / 100.0, 6))
        if order_type in base.NEEDS_LIMIT_PRICE:
            order["limit_price"] = str(round(limit_price, 2))
        if order_type in base.NEEDS_STOP_PRICE:
            order["stop_price"] = str(round(stop_price, 2))
        _payload(self._call(api.order.place_order_v2,
                            creds["account_id"], order))
        # The place response carries only {"client_order_id": ...} — verified
        # live — so there is no broker-native id to return here. That id
        # appears once the order is listed; cancel() accepts either, so the
        # client_order_id is a sufficient handle in the meantime.
        return {"id": "", "symbol": symbol.upper(),
                "qty": float(abs(int(qty))), "side": side, "type": order_type,
                "limit_price": (round(limit_price, 2)
                                if limit_price is not None else None),
                "status": "submitted", "client_order_id": cid}

    def replace(self, creds, order_id, qty=None, limit_price=None):
        raise NotImplementedError("Webull has no order replace — executor "
                                  "uses cancel+resubmit (caps.supports_replace)")

    def cancel(self, creds, order_id):
        """Webull's cancel endpoint only accepts client_order_id, but
        open_orders() normalizes the broker-native order_id into the
        returned Order dict's "id" field (matching the base contract) — so
        the executor's normal call pattern, `cancel(creds,
        open_orders()[i]["id"])`, would otherwise send the wrong
        identifier and get silently swallowed below as a "successful"
        no-op cancel of nothing.

        To keep that call pattern correct without changing the base
        contract, resolve `order_id` against a fresh open-orders list
        first: match it against either the broker-native order_id or the
        client_order_id (accepting either lets callers who already have
        the client_order_id — e.g. straight from submit()'s return — skip
        the extra round trip too), then cancel using that order's
        client_order_id. No match means the order is already gone
        (filled/cancelled/expired) — a legitimate no-op, not an error, so
        we return without calling the SDK at all. The broker-rejected/
        unavailable swallow applies ONLY to the actual cancel call, after
        resolution — a failure to list open orders is NOT swallowed and
        propagates normally.
        """
        # reuse open_orders() so the response-shape handling lives in exactly
        # one place (Webull nests leg detail under items[] — see _norm)
        cid = None
        for o in self.open_orders(creds):
            if str(o["id"]) == str(order_id) or o["client_order_id"] == order_id:
                cid = o["client_order_id"] or None
                break
        if cid is None:
            return
        api = self._api(creds)
        try:
            _payload(self._call(api.order.cancel_order,
                                creds["account_id"], cid))
        except BrokerRejected:
            # The venue ANSWERED and refused: the order is already gone
            # (filled, cancelled, expired). A legitimate no-op.
            pass
        # BrokerUnavailable is deliberately NOT swallowed. A 429 or a 5xx
        # means we do not know whether the cancel landed, and the order may
        # still be resting. Recording it as cancelled lets the next pass
        # submit a replacement ALONGSIDE it — two protective stops on the
        # same shares. Let it propagate; the caller already treats an
        # unavailable venue as "retry", which is the honest answer.
