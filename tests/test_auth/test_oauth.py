def test_required_scopes_allow_saving_albums():
    """`library save` writes to the user's saved albums, which no earlier
    scope covered."""
    from spotifyforge.auth.oauth import REQUIRED_SCOPES

    assert "user-library-modify" in str(REQUIRED_SCOPES)
