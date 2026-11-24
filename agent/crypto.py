"""Encryption at rest for platform tokens (Fernet: AES-128-CBC + HMAC-SHA256).

The Fernet key is derived from ENCRYPTION_KEY, so any long random string works. Changing
ENCRYPTION_KEY makes stored tokens unreadable - you would have to connect the accounts again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_cache: dict[str, Fernet] = {}


def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    if key not in _cache:
        derived = hashlib.sha256(b"social-post-agent:v1:" + key.encode()).digest()
        _cache.clear()
        _cache[key] = Fernet(base64.urlsafe_b64encode(derived))
    return _cache[key]


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored token can't be decrypted - was ENCRYPTION_KEY changed? Connect the account again") from exc


def encrypt_json(data: dict) -> str:
    return encrypt(json.dumps(data)) or ""


def decrypt_json(value: str) -> dict:
    return json.loads(decrypt(value) or "{}")


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a.encode(), b.encode())
