"""Fixtures for web-route tests: the real app wired to the fake Spotify.

Composes the root ``app_env`` fixture (fresh on-disk DB, fake Spotify via
the tekore sender seam, test OAuth credentials, per-test scheduler reset).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class WebEnv:
    """A booted app, a lifespan-running client, and the fake Spotify backend."""

    app: FastAPI
    client: TestClient
    fake: Any


@pytest.fixture()
def env(app_env):
    """The real FastAPI app running with lifespan (DB init + scheduler)."""
    from spotifyforge.web.app import create_app

    app = create_app()
    with TestClient(app) as client:
        yield WebEnv(app=app, client=client, fake=app_env)
