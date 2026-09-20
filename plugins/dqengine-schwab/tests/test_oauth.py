"""The token refresh is on the live-money path (SchwabAdapter.ensure_session
calls it whenever the access token is stale). Pin the request it builds,
character for character, so a refactor cannot move it by accident."""
import io
import json
import urllib.error

import pytest

from dqengine.adapters.base import BrokerAuthExpired, BrokerUnavailable
from dqengine_schwab import oauth


def _capture(monkeypatch, body=b'{"access_token": "A", "refresh_token": "R", "expires_in": 1800}'):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["data"] = req.data
        seen["timeout"] = timeout
        return io.BytesIO(body)

    # the module the code under test looks urlopen up on, at call time
    monkeypatch.setattr("dqengine_schwab.oauth.urllib.request.urlopen", fake_urlopen)
    return seen


def test_refresh_sends_exactly_this_request(monkeypatch):
    seen = _capture(monkeypatch)
    tok = oauth.schwab_refresh("k", "s", "r")
    assert tok == {"access_token": "A", "refresh_token": "R", "expires_in": 1800}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.schwabapi.com/v1/oauth/token"
    # base64("k:s") == "azpz"; urllib title-cases header names on the way out
    assert seen["headers"] == {"Authorization": "Basic azpz",
                               "Content-type": "application/x-www-form-urlencoded"}
    assert seen["data"] == b"grant_type=refresh_token&refresh_token=r"
    assert seen["timeout"] == 30


def test_refresh_dead_token_is_auth_expired(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "u", 400, "bad", {},
            io.BytesIO(b'{"error":"unsupported_token_type"}'))

    monkeypatch.setattr("dqengine_schwab.oauth.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(BrokerAuthExpired) as ei:
        oauth.schwab_refresh("K", "S", "DEAD")
    assert "unsupported_token_type" in str(ei.value)


def test_other_http_errors_and_transport_failures_are_unavailable(monkeypatch):
    def five_hundred(req, timeout=None):
        raise urllib.error.HTTPError("u", 503, "down", {}, io.BytesIO(b"x"))

    monkeypatch.setattr("dqengine_schwab.oauth.urllib.request.urlopen", five_hundred)
    with pytest.raises(BrokerUnavailable):
        oauth.schwab_refresh("K", "S", "RT")

    def unreachable(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("dqengine_schwab.oauth.urllib.request.urlopen", unreachable)
    with pytest.raises(BrokerUnavailable):
        oauth.schwab_refresh("K", "S", "RT")


def test_token_post_returns_the_decoded_json_body(monkeypatch):
    seen = _capture(monkeypatch, body=json.dumps({"ok": 1}).encode())
    assert oauth._token_post("k", "s", {"grant_type": "x"}) == {"ok": 1}
    assert seen["data"] == b"grant_type=x"
