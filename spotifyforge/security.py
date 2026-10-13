"""OAuth ``state`` helpers: the only secret-handling this package still does itself.

Tokens live in the OS keyring (:class:`spotifyforge.auth.oauth.KeyringTokenStore`).
"""

from __future__ import annotations

import hmac
import secrets


def generate_csrf_state() -> str:
    """Generate a cryptographically random CSRF state token."""
    return secrets.token_urlsafe(32)


def verify_csrf_state(expected: str, actual: str | None) -> bool:
    """Constant-time comparison of CSRF state tokens."""
    if actual is None:
        return False
    return hmac.compare_digest(expected, actual)
