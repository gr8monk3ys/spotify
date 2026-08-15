"""Cryptographic utilities for SpotifyForge."""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets
import time
import warnings

from cryptography.fernet import Fernet

from spotifyforge.config import settings

_DEV_FALLBACK_SECRET = "insecure-dev-default-key-do-not-use-in-production"

# Session cookies are valid for 7 days.
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7


# The secret is immutable per process; cache the derivations so the
# per-request hot paths (session verification, token decryption) don't
# redo SHA-256/base64/Fernet construction on every call.
@functools.lru_cache(maxsize=1)
def _get_secret() -> str:
    """Return the application secret, failing hard outside development.

    Only the literal ``development`` environment may run without an
    explicit ``SPOTIFYFORGE_SECRET_KEY``; every other environment name
    (``production``, ``prod``, ``staging``, ...) refuses to fall back to
    the publicly known default.
    """
    secret = settings.secret_key
    if secret:
        return secret
    if settings.environment != "development":
        raise RuntimeError(
            "SPOTIFYFORGE_SECRET_KEY must be set when SPOTIFYFORGE_ENVIRONMENT "
            f"is {settings.environment!r} (anything other than 'development')."
        )
    warnings.warn(
        "SPOTIFYFORGE_SECRET_KEY not set — using insecure default. DO NOT use in production.",
        stacklevel=3,
    )
    return _DEV_FALLBACK_SECRET


@functools.lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """Get a Fernet instance keyed from the application secret."""
    key = base64.urlsafe_b64encode(hashlib.sha256(_get_secret().encode()).digest())
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning a base64 Fernet ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext back to the original token string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def generate_csrf_state() -> str:
    """Generate a cryptographically random CSRF state token."""
    return secrets.token_urlsafe(32)


def verify_csrf_state(expected: str, actual: str | None) -> bool:
    """Constant-time comparison of CSRF state tokens."""
    if actual is None:
        return False
    return hmac.compare_digest(expected, actual)


# ---------------------------------------------------------------------------
# Signed session cookies
# ---------------------------------------------------------------------------
# Format: "<user_id>.<expiry_ts>.<hmac>" where the HMAC covers
# "<user_id>.<expiry_ts>" under the application secret. The cookie is
# self-authenticating: a client cannot mint or alter one without the secret.


def _session_signature(payload: str) -> str:
    mac = hmac.new(_get_secret().encode(), payload.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def sign_session(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Create a signed session cookie value for *user_id*."""
    expiry = int(time.time()) + ttl_seconds
    payload = f"{user_id}.{expiry}"
    return f"{payload}.{_session_signature(payload)}"


def verify_session(cookie_value: str | None) -> int | None:
    """Validate a signed session cookie and return its user id.

    Returns ``None`` for missing, malformed, tampered, or expired cookies.
    """
    if not cookie_value:
        return None
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return None
    user_id_str, expiry_str, signature = parts
    payload = f"{user_id_str}.{expiry_str}"
    if not hmac.compare_digest(_session_signature(payload), signature):
        return None
    try:
        user_id = int(user_id_str)
        expiry = int(expiry_str)
    except ValueError:
        return None
    if expiry < time.time():
        return None
    return user_id
