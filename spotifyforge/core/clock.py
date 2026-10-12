"""One definition of "now", so every timestamp this package writes is UTC-aware."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)
