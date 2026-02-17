"""Cryptographic utilities for SpotifyForge."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import warnings

from cryptography.fernet import Fernet

from spotifyforge.config import settings


def _get_fernet() -> Fernet:
    """Get a Fernet instance using the app's encryption key.

    Derives a Fernet key from SPOTIFYFORGE_SECRET_KEY.
    If no key is set, falls back to a deterministic key (dev only).
    """
    secret = getattr(settings, "secret_key", "") or os.environ.get(
        "SPOTIFYFORGE_SECRET_KEY", ""
    )
    if not secret:
        if getattr(settings, "environment", "development") == "production":
            raise RuntimeError("SPOTIFYFORGE_SECRET_KEY must be set in production")
        warnings.warn(
            "SPOTIFYFORGE_SECRET_KEY not set — using insecure default. "
            "DO NOT use in production.",
            stacklevel=2,
        )
        secret = "insecure-dev-default-key-do-not-use-in-production"
    # Derive a 32-byte key from the secret
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning a base64 Fernet ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext back to the original token string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def hash_token(token: str) -> str:
    """Create a SHA-256 hash of a token for indexed lookup.

    This is a one-way hash used for fast DB lookups on Bearer tokens
    without storing the raw token.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def generate_csrf_state() -> str:
    """Generate a cryptographically random CSRF state token."""
    return secrets.token_urlsafe(32)


def verify_csrf_state(expected: str, actual: str | None) -> bool:
    """Constant-time comparison of CSRF state tokens."""
    if actual is None:
        return False
    return hmac.compare_digest(expected, actual)
