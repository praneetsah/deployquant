"""The credential vault and the provider that keys it.

Every stored broker connection is a Fernet blob over a secret the host
supplies. If a process reaches the executor with a different secret,
`decrypt_creds` raises outside the sweep's try block and no account trades.
These pin what happens when nobody configured one, and where the default
provider looks.
"""
import os

import pytest

from dqengine.live import vault


@pytest.fixture(autouse=True)
def _restore_provider():
    saved = vault._PROVIDER
    yield
    vault.set_secret_provider(saved)


def test_an_unconfigured_vault_refuses_instead_of_inventing_a_key(
        monkeypatch, tmp_path):
    """A generated secret is indistinguishable from the right one until
    every stored connection fails to decrypt. Refuse, and name the variable."""
    missing = str(tmp_path / "nothing-here")
    vault.set_secret_provider(None)
    monkeypatch.delenv("DQENGINE_SECRET", raising=False)
    monkeypatch.setattr(vault, "SECRET_PATH", missing)
    with pytest.raises(RuntimeError) as e:
        vault.encrypt_creds({"a": 1})
    assert "DQENGINE_SECRET" in str(e.value)
    assert not os.path.exists(missing)


def test_the_default_provider_reads_the_env_then_the_file(
        monkeypatch, tmp_path):
    path = tmp_path / "dqengine_secret"
    path.write_text("from-the-file\n")
    vault.set_secret_provider(None)
    monkeypatch.setattr(vault, "SECRET_PATH", str(path))
    monkeypatch.setenv("DQENGINE_SECRET", "from-the-env")
    assert vault._secret() == b"from-the-env"
    monkeypatch.delenv("DQENGINE_SECRET")
    assert vault._secret() == b"from-the-file"


def test_a_provider_returning_text_is_encoded_the_same_as_bytes():
    vault.set_secret_provider(lambda: "pin-text-secret")
    blob = vault.encrypt_creds({"a": 1})
    vault.set_secret_provider(lambda: b"pin-text-secret")
    assert vault.decrypt_creds(blob) == {"a": 1}
