"""What an Alpaca HTTP error means.

Alpaca answers bad credentials with 401, and a REJECTED ORDER with 403 and a
JSON body that says why -- measured on the paper API, 2026-10-01:

    403 {"code":40310000,"message":"insufficient buying power", ...}
    403 {"code":40310000,"message":"potential wash trade detected. use
         complex orders","reject_reason":"opposite side market/stop order
         exists", ...}
    401 {"message": "unauthorized."}

Reading the 403 as bad credentials marks the connection `reconnect_needed`
and every later sweep skips it: one refused order stopped the account.
"""
import io
import urllib.error

import pytest

from dqengine.adapters import alpaca
from dqengine.adapters.base import (BrokerAuthExpired, BrokerRejected,
                                    BrokerUnavailable)

CREDS = {"key_id": "k", "secret_key": "s", "paper": True}


def _raises(monkeypatch, code, body):
    def boom(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, code, "x", {},
                                     io.BytesIO(body.encode()))
    monkeypatch.setattr(alpaca.urllib.request, "urlopen", boom)


def test_a_refused_order_is_a_rejection_not_bad_credentials(monkeypatch):
    _raises(monkeypatch, 403,
            '{"buying_power":"381229.38","code":40310000,'
            '"message":"insufficient buying power"}')
    with pytest.raises(BrokerRejected) as ei:
        alpaca._req(CREDS, "POST", "/v2/orders", body={"symbol": "SPY"})
    assert not isinstance(ei.value, BrokerAuthExpired)
    assert "insufficient buying power" in str(ei.value)


def test_a_wash_trade_refusal_carries_its_reason(monkeypatch):
    _raises(monkeypatch, 403,
            '{"code":40310000,"message":"potential wash trade detected. use '
            'complex orders","reject_reason":"opposite side market/stop '
            'order exists"}')
    with pytest.raises(BrokerRejected) as ei:
        alpaca._req(CREDS, "POST", "/v2/orders", body={"symbol": "SPY"})
    assert "opposite side market/stop order exists" in str(ei.value)


def test_401_is_bad_credentials(monkeypatch):
    _raises(monkeypatch, 401, '{"message": "unauthorized."}')
    with pytest.raises(BrokerAuthExpired):
        alpaca._req(CREDS, "GET", "/v2/account")


def test_a_bare_403_is_still_bad_credentials(monkeypatch):
    """No message to report: the account itself is forbidden."""
    for body in ("", "forbidden", '{"message": "forbidden."}'):
        _raises(monkeypatch, 403, body)
        with pytest.raises(BrokerAuthExpired):
            alpaca._req(CREDS, "GET", "/v2/account")


def test_other_statuses_keep_their_meaning(monkeypatch):
    _raises(monkeypatch, 422, '{"message":"qty must be > 0"}')
    with pytest.raises(BrokerRejected):
        alpaca._req(CREDS, "POST", "/v2/orders", body={})
    _raises(monkeypatch, 500, "oops")
    with pytest.raises(BrokerUnavailable):
        alpaca._req(CREDS, "GET", "/v2/account")


def test_a_403_on_a_cancel_or_a_read_is_still_bad_credentials(monkeypatch):
    """Review F7: cancel() treats BrokerRejected as "already gone", so a
    403 there must not become one; and a 403 on a read is about the
    account."""
    body = ('{"code":40310000,"message":"insufficient buying power"}')
    _raises(monkeypatch, 403, body)
    with pytest.raises(BrokerAuthExpired):
        alpaca._req(CREDS, "DELETE", "/v2/orders/abc")
    with pytest.raises(BrokerAuthExpired):
        alpaca._req(CREDS, "GET", "/v2/positions")
    with pytest.raises(BrokerRejected):
        alpaca._req(CREDS, "PATCH", "/v2/orders/abc", body={"qty": "1"})
