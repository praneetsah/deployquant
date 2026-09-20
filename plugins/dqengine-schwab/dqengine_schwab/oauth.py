"""Schwab OAuth token refresh — a plain urllib POST to Schwab's token endpoint.
Lives with the adapter so `ensure_session` needs nothing from the hosted api;
the api's OAuth *flow* (authorize URL, code exchange, persistence) stays
private in platform/api/broker_oauth.py and imports the refresh from here
(spec 2026-09-18 R4: the api may import anything open)."""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from dqengine.adapters.base import BrokerAuthExpired, BrokerUnavailable


def _token_post(app_key: str, app_secret: str, form: dict) -> dict:
    auth = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.schwabapi.com/v1/oauth/token",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code in (400, 401):
            raise BrokerAuthExpired(f"Schwab re-authorization needed: {detail}")
        raise BrokerUnavailable(f"Schwab token endpoint {e.code}: {detail}")
    except BrokerAuthExpired:
        raise
    except Exception as e:
        raise BrokerUnavailable(f"could not reach Schwab: {e}")


def schwab_refresh(app_key, app_secret, refresh_token) -> dict:
    return _token_post(app_key, app_secret,
                       {"grant_type": "refresh_token",
                        "refresh_token": refresh_token})
