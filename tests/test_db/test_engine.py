"""Tests for db/engine.py schema self-healing.

``create_all`` never alters existing tables, so a database created
before a column was removed from the models keeps it — and a leftover
NOT NULL column breaks every INSERT that follows the current model
(exactly how removing ``Playlist.follower_count`` broke ``forge`` on a
pre-existing database).
"""

from sqlalchemy import inspect, text
from sqlmodel import Session

from spotifyforge.models.models import Playlist, User


def test_init_db_drops_columns_models_no_longer_declare(isolated_db):
    from spotifyforge.db import engine as engine_mod

    engine = engine_mod.get_engine()
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE playlists ADD COLUMN follower_count INTEGER NOT NULL DEFAULT 0")
        )
        # An index over the dead column must not block the drop (a
        # leftover indexed users.token_hash column proved it does).
        connection.execute(
            text("CREATE INDEX ix_playlists_follower_count ON playlists (follower_count)")
        )

    engine_mod.init_db()

    columns = {c["name"] for c in inspect(engine).get_columns("playlists")}
    assert "follower_count" not in columns

    # The proof that matters: an INSERT built from the current model works.
    with Session(engine) as session:
        user = User(spotify_id="u1", display_name="u1")
        session.add(user)
        session.commit()
        session.refresh(user)
        session.add(Playlist(spotify_id="pl1", owner_id=user.id, name="strictly coldwave"))
        session.commit()


def test_init_db_leaves_declared_columns_alone(isolated_db):
    from spotifyforge.db import engine as engine_mod

    engine = engine_mod.get_engine()
    before = {c["name"] for c in inspect(engine).get_columns("playlists")}
    engine_mod.init_db()
    assert {c["name"] for c in inspect(engine).get_columns("playlists")} == before
