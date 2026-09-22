"""Four small behaviours of the sync cycle that had no test.

Each is a line or two in `sync_broker_account` that is easy to lose while the
function is taken apart.
"""
import time

import pytest

from dqengine.live import executor, vault

from rig import blind_rig
from dqengine.live import persistence


# ---- H24: a broker rate limit pauses the fast path

def test_the_fast_path_stands_down_during_a_rate_limit_cooldown(pg, owner_id, monkeypatch):
    fb = blind_rig(pg, owner_id, monkeypatch, "c24", "d24")
    monkeypatch.setitem(executor._SYNC_GATE, "c24", {"cooldown_until": time.time() + 30})
    assert executor.sync_broker_account("c24", fast=True) == "fallback"
    assert fb.log == []                                          # nothing reached the broker


def test_the_fast_path_runs_again_once_the_cooldown_has_passed(pg, owner_id, monkeypatch):
    fb = blind_rig(pg, owner_id, monkeypatch, "c24b", "d24b")
    monkeypatch.setitem(executor._SYNC_GATE, "c24b", {"cooldown_until": time.time() - 1})
    assert executor.sync_broker_account("c24b", fast=True) == "submitted"
    assert len([1 for tag, _ in fb.log if tag == "submit"]) == 1


def test_a_full_cycle_skips_during_the_cooldown_except_in_the_close_window(pg, owner_id, monkeypatch):
    """Skipping is safe mid-session, because another cycle follows in seconds.
    In the last minutes before the close it is not: the next cycle lands after
    the gateway shuts and the day's orders are lost."""
    blind_rig(pg, owner_id, monkeypatch, "c24c", "d24c")
    monkeypatch.setitem(executor._SYNC_GATE, "c24c", {"cooldown_until": time.time() + 30})
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: False)
    assert executor.sync_broker_account("c24c", fast=False) == "skipped"
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: True)
    assert executor.sync_broker_account("c24c", fast=False) == "audited"


# ---- H25: the buying power the order launcher is told about

def _captured_buying_power(pg, owner_id, monkeypatch, conn, dep, balance):
    blind_rig(pg, owner_id, monkeypatch, conn, dep)
    with pg() as s:
        s.get(persistence.BrokerConnection, conn).balance = balance
        s.commit()
    executor._GATHER_CACHE.pop(conn, None)
    seen = {}
    real = executor.reconcile

    def wrapped(*a, **k):
        seen["bp"] = k.get("buying_power")
        return real(*a, **k)
    monkeypatch.setattr(executor, "reconcile", wrapped)
    executor.sync_broker_account(conn, fast=True)
    return seen["bp"]


@pytest.mark.parametrize("n,balance,expected", [
    (1, {"cash": 1000.0, "equity": 5000.0}, 4750.0),     # 95% of equity beats cash
    (2, {"cash": 6000.0, "equity": 5000.0}, 6000.0),     # cash beats 95% of equity
    (3, {"cash": None, "equity": None}, None),           # unknown is None, never 0
    (4, {}, None),
    (5, {"cash": "n/a", "equity": 5000.0}, None),        # unreadable is unknown too
])
def test_buying_power_is_the_larger_of_cash_and_95_percent_of_equity(pg, owner_id, monkeypatch, n, balance, expected):
    assert _captured_buying_power(pg, owner_id, monkeypatch, f"c25-{n}", f"d25-{n}", balance) == expected


# ---- H27: a refreshed broker login is stored, and the fast path does not re-prove it

def test_refreshed_credentials_are_encrypted_and_stored(pg, owner_id, monkeypatch):
    fb = blind_rig(pg, owner_id, monkeypatch, "c27", "d27")
    from dqengine.live.book import book_for
    book_for("c27").creds = None
    calls = []

    def ensure_session(creds):
        calls.append(dict(creds))
        return {**creds, "access_token": "fresh-token"}
    monkeypatch.setattr(fb, "ensure_session", ensure_session, raising=False)

    executor.sync_broker_account("c27", fast=True)

    assert calls == [{"k": "v"}]
    with pg() as s:
        blob = s.get(persistence.BrokerConnection, "c27").creds_encrypted
    assert vault.decrypt_creds(blob) == {"k": "v",
                                        "access_token": "fresh-token"}
    assert book_for("c27").creds == {"k": "v", "access_token": "fresh-token"}


def test_the_fast_path_reuses_a_login_under_a_minute_old_and_a_full_cycle_never_does(pg, owner_id, monkeypatch):
    fb = blind_rig(pg, owner_id, monkeypatch, "c27b", "d27b")
    from dqengine.live.book import book_for
    calls = []
    monkeypatch.setattr(fb, "ensure_session", lambda creds: calls.append(1) or None, raising=False)
    book = book_for("c27b")

    book.creds, book.creds_at = {"k": "v"}, time.time() - 10
    executor.sync_broker_account("c27b", fast=True)
    assert calls == []                                           # fresh enough: no network call

    book.creds_at = time.time() - 61
    executor._GATHER_CACHE.pop("c27b", None)
    executor.sync_broker_account("c27b", fast=True)
    assert calls == [1]                                          # older than a minute: refreshed

    book.creds_at = time.time()
    monkeypatch.setattr(executor, "_in_close_window", lambda *a, **k: True)
    executor.sync_broker_account("c27b", fast=False)
    assert calls == [1, 1]                                       # the full cycle always refreshes


# ---- H28: credentials stored before the move still decrypt after it

# Not a credential: a Fernet blob over the three literals k-123 / s-456 / A1,
# written under the labelled throwaway secret on the line above. It is here
# because the derivation has to keep reading blobs written before a move.
FIXED_SECRET = "pin-h28-not-a-real-secret"
BLOB_FROM_2026_09_20 = (
    "gAAAAABqsEonsOVDDUM5UL4NgoOXoWCCMltRvS2G9QFAEwOUt2JELRWvlgbdCSql_ao98IxLRrMhKwLJXqPPuFMhx_RKE-"
    "TgILAZ21UC9-BzYRFtE3WYf9mDwzrGV_5jaQfOU0tTLQp6OpA9tHYZEHMoM8G4gmmKfQ==")


def test_a_credential_blob_written_today_still_decrypts(monkeypatch):
    """Every broker connection's credentials are stored as a Fernet blob
    keyed off the host's secret. If the key derivation changes by one byte,
    every stored connection fails to decrypt and no account can trade. This
    blob was written by today's code under a throwaway secret. Which
    variable supplied that secret does not matter: the key is
    sha256(secret), and the host chooses where the bytes come from.

    The values inside look like a credential and are not one: they are the
    literals k-123 / s-456 / A1 under the labelled throwaway secret above."""
    monkeypatch.setenv(vault.SECRET_ENV, FIXED_SECRET)
    expect = {"app_key": "k-123", "app_secret": "s-456", "account_id": "A1"}
    assert vault.decrypt_creds(BLOB_FROM_2026_09_20) == expect


def test_credentials_round_trip_and_a_different_secret_cannot_read_them(monkeypatch):
    from cryptography.fernet import InvalidToken
    monkeypatch.setenv(vault.SECRET_ENV, FIXED_SECRET)
    blob = vault.encrypt_creds({"a": 1})
    assert vault.decrypt_creds(blob) == {"a": 1}
    monkeypatch.setenv(vault.SECRET_ENV, "another-secret")
    with pytest.raises(InvalidToken):
        vault.decrypt_creds(blob)
