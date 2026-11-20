"""Encryption at rest for OAuth tokens (Fernet = AES-128-CBC + HMAC).

Key source, in order: env TOKEN_ENCRYPTION_KEY (a Fernet key), else a key file created once in DATA_DIR
(on Railway that is the /data volume, so a database dump or backup alone cannot reveal the tokens).
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR

PREFIX = "enc:v1:"


class TokenError(Exception):
    """Stored login cannot be read; the channel must be reconnected."""


def _key() -> bytes:
    env = (os.getenv("TOKEN_ENCRYPTION_KEY") or "").strip()
    if env:
        return env.encode()
    path = Path(DATA_DIR) / ".token_key"
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def encrypt(plain: str) -> str:
    return PREFIX + _f().encrypt(plain.encode()).decode()


def decrypt(value: str | None) -> str:
    if not value:
        raise TokenError("No stored login for this channel. Reconnect it.")
    if not value.startswith(PREFIX):  # legacy plaintext, upgraded on next startup
        return value
    try:
        return _f().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise TokenError("Stored login can't be decrypted (encryption key changed). Reconnect your YouTube channel.") from e


def encrypt_existing() -> int:
    """Encrypt any token still stored as plaintext. Returns how many were upgraded."""
    from .db import Destination, SessionLocal

    n = 0
    with SessionLocal() as s:
        for d in s.query(Destination).all():
            if d.token and not d.token.startswith(PREFIX):
                d.token = encrypt(d.token)
                n += 1
        s.commit()
    return n
