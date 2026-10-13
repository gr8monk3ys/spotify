"""Token loading shared by the CLI and the MCP server."""

from __future__ import annotations

import os
import time

import pytest
import tekore as tk

from spotifyforge.auth import session
from spotifyforge.auth.oauth import AuthenticationError, SpotifyAuth


def _token(fake, user_id: str = "user1", expires_in: int = 3600) -> tk.Token:
    return tk.Token({**fake.issue_token(user_id), "expires_in": expires_in}, uses_pkce=False)


@pytest.fixture()
def auth(app_env, memory_token_store):
    app_env.add_user("user1")
    return SpotifyAuth(token_store=memory_token_store)


def test_a_token_with_headroom_is_used_as_is(app_env, auth, memory_token_store):
    stored = _token(app_env)
    memory_token_store.save_token("user1", stored)

    assert session.load_token(auth, "user1") is stored
    assert ("POST", "/api/token") not in app_env.requests


def test_a_short_lived_token_is_refreshed_and_saved(app_env, auth, memory_token_store, tmp_path):
    memory_token_store.save_token("user1", _token(app_env, expires_in=60))

    token = session.load_token(auth, "user1")

    assert token.expires_in > session.TOKEN_HEADROOM_SECONDS
    assert memory_token_store.load_token("user1") is token
    assert not (tmp_path / "token-refresh.lock").exists()  # released


def test_a_refresh_another_process_already_did_is_not_repeated(
    app_env, auth, memory_token_store, monkeypatch
):
    """The lock was held; by the time it is ours, the stored token is fresh."""
    stale = _token(app_env, expires_in=60)
    fresh = _token(app_env)
    loads = iter([stale, fresh])
    monkeypatch.setattr(memory_token_store, "load_token", lambda user_id: next(loads))

    assert session.load_token(auth, "user1") is fresh
    assert ("POST", "/api/token") not in app_env.requests


def test_a_lock_left_by_a_dead_process_is_taken_over(app_env, auth, memory_token_store, tmp_path):
    lock = tmp_path / "token-refresh.lock"
    lock.mkdir()
    long_ago = time.time() - 3600
    os.utime(lock, (long_ago, long_ago))
    memory_token_store.save_token("user1", _token(app_env, expires_in=60))

    token = session.load_token(auth, "user1")

    assert token.expires_in > session.TOKEN_HEADROOM_SECONDS


def test_a_held_lock_times_out_with_a_clear_error(
    app_env, auth, memory_token_store, tmp_path, monkeypatch
):
    (tmp_path / "token-refresh.lock").mkdir()
    monkeypatch.setattr(session, "_LOCK_WAIT_SECONDS", 0.3)
    memory_token_store.save_token("user1", _token(app_env, expires_in=60))

    with pytest.raises(AuthenticationError, match="another spotifyforge process"):
        session.load_token(auth, "user1")


def test_not_logged_in(app_env):
    with pytest.raises(session.NotLoggedInError):
        session.current_user_id()
