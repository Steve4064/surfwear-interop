"""Authentification : mots de passe hachés (PBKDF2) et jetons de session signés (HMAC).

Le jeton est transmis dans un cookie HttpOnly (navigateur) ou dans l'en-tête
``Authorization: Bearer`` (clients techniques). Les logiciels des boutiques
peuvent aussi utiliser leur clé d'API (en-tête ``X-API-Key``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

ITERATIONS = 120_000
COOKIE_NAME = "sw_session"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(secret: str, claims: dict, ttl_seconds: int) -> str:
    body = _b64(json.dumps({**claims, "exp": int(time.time()) + ttl_seconds}).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(secret: str, token: str) -> dict | None:
    try:
        body, sig = token.split(".")
    except ValueError:
        return None
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    claims = json.loads(_unb64(body))
    if claims.get("exp", 0) < time.time():
        return None
    return claims
