"""Broker credential encryption: Fernet over a secret the host supplies.

Lifted out of the platform's own auth module unchanged (Phase 3 spec §4.5)
so the executor can decrypt a connection without importing the platform's
passwords and session tokens. The key derivation is byte-for-byte what
that module has used since the first stored connection: `sha256(secret)`,
urlsafe-base64'd into a Fernet key. A single byte's difference here and
every stored connection stops decrypting, which fails outside
`sync_broker_account`'s try block: no account trades, loudly.

The host chooses where the secret comes from. The platform installs its own
(`PLATFORM_SECRET`, the same secret that signs its auth tokens) from a
module every one of its processes imports -- the api, a worker, a script --
so no process can reach the executor with the wrong key.

With no provider installed this reads `DQENGINE_SECRET`, then
`~/.dqengine_secret` if that file already exists, and otherwise refuses.
It never invents a secret: on a machine that already holds encrypted rows a
generated key is indistinguishable from a correct one until every
connection fails to decrypt, and the generated file then looks like the
real secret to whoever finds it next.
"""
import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet

SECRET_ENV = "DQENGINE_SECRET"
SECRET_PATH = os.path.expanduser("~/.dqengine_secret")

_PROVIDER = None


def set_secret_provider(fn) -> None:
    """Install the callable that returns the host's secret (bytes or str).
    `None` restores the default."""
    global _PROVIDER
    _PROVIDER = fn


def _default_secret() -> bytes:
    env = os.environ.get(SECRET_ENV)
    if env:
        return env.strip().encode()
    if os.path.exists(SECRET_PATH):
        return open(SECRET_PATH).read().strip().encode()
    raise RuntimeError(
        f"no credential secret: set {SECRET_ENV}, or write one to "
        f"{SECRET_PATH} (chmod 600). Refusing to generate one -- a fresh "
        f"key silently fails to decrypt credentials stored under the old "
        f"one.")


def _secret() -> bytes:
    s = (_PROVIDER or _default_secret)()
    return s if isinstance(s, bytes) else str(s).encode()


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(_secret()).digest())
    return Fernet(key)


def encrypt_creds(creds: dict) -> str:
    return _fernet().encrypt(json.dumps(creds).encode()).decode()


def decrypt_creds(blob: str) -> dict:
    return json.loads(_fernet().decrypt(blob.encode()))
