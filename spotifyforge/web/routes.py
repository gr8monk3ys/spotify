"""API route definitions for SpotifyForge.

Organizes all endpoints into four ``APIRouter`` instances:

* **auth_router** -- authentication and session management
* **playlist_router** -- CRUD and operations on Spotify playlists
* **discovery_router** -- music discovery features
* **schedule_router** -- scheduled automation job management

Each router is included by :func:`spotifyforge.web.app.create_app`.
All Spotify-backed routes obtain their client through the
``get_spotify`` dependency, which decrypts (and refreshes) the user's
stored tokens — routes never touch token material directly.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import tekore as tk
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from spotifyforge.auth.oauth import build_auth_url, exchange_code, get_spotify_user
from spotifyforge.core.clients import apply_user_profile, apply_user_tokens
from spotifyforge.core.discovery import DiscoveryEngine, artist_to_dict, track_to_dict
from spotifyforge.core.playlist_manager import PlaylistManager
from spotifyforge.core.scheduler import register_job, unregister_job, validate_cron
from spotifyforge.models.models import (
    JobType,
    Playlist,
    PlaylistCreate,
    PlaylistResponse,
    PlaylistUpdate,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobResponse,
    TrackResponse,
    User,
)
from spotifyforge.security import (
    SESSION_TTL_SECONDS,
    generate_csrf_state,
    sign_session,
    verify_csrf_state,
)
from spotifyforge.web.deps import SESSION_COOKIE, get_current_user, get_db_session, get_spotify

logger = logging.getLogger("spotifyforge.web.routes")

STATE_COOKIE = "spotifyforge_oauth_state"

# Shared attributes for every cookie we set or delete; mismatched
# attributes between set_cookie and delete_cookie silently break deletion.
_COOKIE_ATTRS: dict[str, Any] = {"httponly": True, "samesite": "lax"}

_TRACK_URI_RE = re.compile(r"^spotify:track:[A-Za-z0-9]{10,64}$")
_MAX_TRACK_URIS = 1000

# =========================================================================
# Auth Router
# =========================================================================
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.get("/login", summary="Get Spotify login URL")
async def auth_login(request: Request) -> JSONResponse:
    """Return the Spotify authorization URL and set the CSRF state cookie.

    The front-end should redirect the user's browser to the returned URL.
    The random ``state`` embedded in it is also stored in a short-lived,
    HttpOnly cookie; the callback requires the two to match.
    """
    state = generate_csrf_state()
    auth_url = build_auth_url(state=state)
    response = JSONResponse(content={"auth_url": auth_url})
    response.set_cookie(
        key=STATE_COOKIE,
        value=state,
        secure=request.url.scheme == "https",
        max_age=600,  # the OAuth dance should take minutes, not hours
        **_COOKIE_ATTRS,
    )
    return response


@auth_router.get(
    "/callback",
    response_class=RedirectResponse,
    summary="Handle OAuth callback",
)
async def auth_callback(
    request: Request,
    code: str = Query(..., description="Authorization code from Spotify"),
    state: str | None = Query(default=None, description="Anti-CSRF state parameter"),
    db: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    """Handle the Spotify OAuth callback.

    Validates the CSRF state against the login cookie, exchanges the
    authorization code for tokens, upserts the user record, and sets the
    signed session cookie.
    """
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not verify_csrf_state(expected_state, state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state mismatch. Please restart the login flow.",
        )

    try:
        token_info = await exchange_code(code)
    except Exception as exc:
        logger.error("Token exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to exchange authorization code with Spotify.",
        ) from exc

    try:
        spotify_user = await get_spotify_user(token_info["access_token"])
    except Exception as exc:
        logger.error("Failed to fetch Spotify profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve Spotify user profile.",
        ) from exc

    result = await db.execute(select(User).where(User.spotify_id == spotify_user["id"]))
    user = result.scalars().first()
    if user is None:
        user = User(spotify_id=spotify_user["id"])

    apply_user_profile(user, spotify_user)
    apply_user_tokens(
        user,
        token_info["access_token"],
        token_info.get("refresh_token"),
        token_info.get("expires_at"),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    assert user.id is not None  # persisted above

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(user.id),
        secure=request.url.scheme == "https",
        max_age=SESSION_TTL_SECONDS,
        **_COOKIE_ATTRS,
    )
    response.delete_cookie(key=STATE_COOKIE, **_COOKIE_ATTRS)
    return response


@auth_router.get("/me", summary="Get current user info")
async def auth_me(
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return profile information for the currently authenticated user."""
    return {
        "id": current_user.id,
        "spotify_id": current_user.spotify_id,
        "display_name": current_user.display_name,
        "email": current_user.email,
        "is_premium": current_user.is_premium,
        "created_at": current_user.created_at.isoformat(),
    }


@auth_router.post("/logout", summary="Log out current user")
async def auth_logout() -> JSONResponse:
    """Clear the session cookie and log the user out."""
    response = JSONResponse(content={"detail": "Logged out successfully."})
    response.delete_cookie(key=SESSION_COOKIE, **_COOKIE_ATTRS)
    return response


# =========================================================================
# Playlist Router
# =========================================================================
playlist_router = APIRouter(prefix="/api/playlists", tags=["playlists"])


async def _owned_playlist(playlist_id: int, user: User, db: AsyncSession) -> Playlist:
    """Load a playlist owned by *user* or raise 404."""
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )
    return playlist


def _validated_uris(uris: list[str]) -> list[str]:
    """Validate a track-URI payload: non-empty, bounded, well-formed."""
    if not uris:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one track URI is required.",
        )
    if len(uris) > _MAX_TRACK_URIS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"At most {_MAX_TRACK_URIS} track URIs per request.",
        )
    bad = [u for u in uris if not _TRACK_URI_RE.match(u)]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid track URI(s): {bad[:5]}",
        )
    return uris


@playlist_router.get(
    "",
    response_model=list[PlaylistResponse],
    summary="List user playlists",
)
async def list_playlists(
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=20, ge=1, le=100, description="Page size"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[Playlist]:
    """Return the authenticated user's playlists with pagination."""
    stmt = (
        select(Playlist)
        .where(Playlist.owner_id == current_user.id)
        .order_by(col(Playlist.updated_at).desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@playlist_router.post(
    "",
    response_model=PlaylistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new playlist",
)
async def create_playlist(
    body: PlaylistCreate,
    current_user: User = Depends(get_current_user),
    spotify: tk.Spotify = Depends(get_spotify),
) -> Playlist:
    """Create a new Spotify playlist and register it in SpotifyForge."""
    manager = PlaylistManager(spotify)
    try:
        return await manager.create_playlist(
            name=body.name,
            owner_id=current_user.id,  # type: ignore[arg-type]
            description=body.description or "",
            public=body.public,
            collaborative=body.collaborative,
        )
    except tk.HTTPError as exc:
        logger.error("Spotify playlist creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create playlist on Spotify.",
        ) from exc


@playlist_router.get(
    "/{playlist_id}",
    response_model=PlaylistResponse,
    summary="Get playlist details",
)
async def get_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Playlist:
    """Return details for a specific playlist. Owner only."""
    return await _owned_playlist(playlist_id, current_user, db)


@playlist_router.put(
    "/{playlist_id}",
    response_model=PlaylistResponse,
    summary="Update playlist details",
)
async def update_playlist(
    playlist_id: int,
    body: PlaylistUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    spotify: tk.Spotify = Depends(get_spotify),
) -> Playlist:
    """Update the name, description, or visibility of a playlist.

    Spotify is updated first; the local row only changes if Spotify
    accepted the update, so the two never silently diverge.
    """
    playlist = await _owned_playlist(playlist_id, current_user, db)

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        return playlist

    try:
        await spotify.playlist_change_details(
            playlist.spotify_id,
            name=update_data.get("name"),
            description=update_data.get("description"),
            public=update_data.get("public"),
            collaborative=update_data.get("collaborative"),
        )
    except tk.HTTPError as exc:
        logger.error("Failed to update playlist %s on Spotify: %s", playlist.spotify_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to update playlist on Spotify.",
        ) from exc

    for field, value in update_data.items():
        setattr(playlist, field, value)
    playlist.updated_at = datetime.now(UTC)
    db.add(playlist)
    await db.commit()
    await db.refresh(playlist)
    return playlist


@playlist_router.post(
    "/{playlist_id}/sync",
    summary="Sync playlist with Spotify",
)
async def sync_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    spotify: tk.Spotify = Depends(get_spotify),
) -> dict[str, Any]:
    """Trigger a full sync of the playlist from Spotify.

    Pulls the latest track listing, metadata, and snapshot ID from
    Spotify and rebuilds the local track associations.
    """
    playlist = await _owned_playlist(playlist_id, current_user, db)

    manager = PlaylistManager(spotify)
    try:
        synced = await manager.sync_playlist(
            playlist.spotify_id,
            owner_id=current_user.id,  # type: ignore[arg-type]
        )
    except tk.HTTPError as exc:
        logger.error("Playlist sync failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to sync playlist from Spotify.",
        ) from exc

    return {
        "detail": "Playlist synced successfully.",
        "playlist_id": playlist_id,
        "tracks_synced": synced.track_count,
    }


@playlist_router.post(
    "/{playlist_id}/deduplicate",
    summary="Remove duplicate tracks",
)
async def deduplicate_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    spotify: tk.Spotify = Depends(get_spotify),
) -> dict[str, Any]:
    """Remove duplicate tracks from the playlist, keeping one copy of each."""
    playlist = await _owned_playlist(playlist_id, current_user, db)

    manager = PlaylistManager(spotify)
    try:
        removed = await manager.deduplicate(playlist.spotify_id)
    except tk.HTTPError as exc:
        logger.error("Deduplication failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to deduplicate playlist.",
        ) from exc

    return {
        "detail": "Deduplication complete.",
        "playlist_id": playlist_id,
        "duplicates_removed": removed,
    }


@playlist_router.post(
    "/{playlist_id}/tracks",
    status_code=status.HTTP_201_CREATED,
    summary="Add tracks to playlist",
)
async def add_tracks(
    playlist_id: int,
    uris: list[str],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    spotify: tk.Spotify = Depends(get_spotify),
) -> dict[str, Any]:
    """Add tracks to a playlist by Spotify URI (``spotify:track:...``)."""
    playlist = await _owned_playlist(playlist_id, current_user, db)
    uris = _validated_uris(uris)

    manager = PlaylistManager(spotify)
    try:
        snapshot_id = await manager.add_tracks(playlist.spotify_id, uris)
    except tk.HTTPError as exc:
        logger.error("Failed to add tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to add tracks to playlist on Spotify.",
        ) from exc

    return {
        "detail": "Tracks added successfully.",
        "playlist_id": playlist_id,
        "tracks_added": len(uris),
        "snapshot_id": snapshot_id,
    }


@playlist_router.delete(
    "/{playlist_id}/tracks",
    summary="Remove tracks from playlist",
)
async def remove_tracks(
    playlist_id: int,
    uris: list[str],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    spotify: tk.Spotify = Depends(get_spotify),
) -> dict[str, Any]:
    """Remove tracks from a playlist by Spotify URI.

    Removes every occurrence of each given URI (Spotify semantics).
    """
    playlist = await _owned_playlist(playlist_id, current_user, db)
    uris = _validated_uris(uris)

    manager = PlaylistManager(spotify)
    try:
        snapshot_id = await manager.remove_tracks(playlist.spotify_id, uris)
    except tk.HTTPError as exc:
        logger.error("Failed to remove tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to remove tracks from playlist on Spotify.",
        ) from exc

    return {
        "detail": "Tracks removed successfully.",
        "playlist_id": playlist_id,
        "tracks_removed": len(uris),
        "snapshot_id": snapshot_id,
    }


# =========================================================================
# Discovery Router
# =========================================================================
discovery_router = APIRouter(prefix="/api/discover", tags=["discovery"])


@discovery_router.get(
    "/top-tracks",
    response_model=list[TrackResponse],
    summary="Get user's top tracks",
)
async def top_tracks(
    time_range: str = Query(
        default="medium_term",
        pattern="^(short_term|medium_term|long_term)$",
        description="Spotify time range: short_term, medium_term, or long_term",
    ),
    limit: int = Query(default=50, ge=1, le=50, description="Number of results"),
    spotify: tk.Spotify = Depends(get_spotify),
) -> list[dict[str, Any]]:
    """Return the user's top tracks from Spotify (``/me/top/tracks``)."""
    engine = DiscoveryEngine(spotify)
    try:
        tracks = await engine.get_user_top_tracks(time_range=time_range, limit=limit)
    except tk.HTTPError as exc:
        logger.error("Failed to fetch top tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve top tracks from Spotify.",
        ) from exc
    return [track_to_dict(t) for t in tracks]


@discovery_router.get(
    "/top-artists",
    summary="Get user's top artists",
)
async def top_artists(
    time_range: str = Query(
        default="medium_term",
        pattern="^(short_term|medium_term|long_term)$",
        description="Spotify time range: short_term, medium_term, or long_term",
    ),
    limit: int = Query(default=50, ge=1, le=50, description="Number of results"),
    spotify: tk.Spotify = Depends(get_spotify),
) -> list[dict[str, Any]]:
    """Return the user's top artists from Spotify (``/me/top/artists``)."""
    engine = DiscoveryEngine(spotify)
    try:
        artists = await engine.get_user_top_artists(time_range=time_range, limit=limit)
    except tk.HTTPError as exc:
        logger.error("Failed to fetch top artists: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve top artists from Spotify.",
        ) from exc
    return [artist_to_dict(a) for a in artists]


@discovery_router.get(
    "/deep-cuts/{artist_id}",
    response_model=list[TrackResponse],
    summary="Find deep cuts for an artist",
)
async def deep_cuts(
    artist_id: str,
    threshold: int = Query(
        default=30,
        ge=0,
        le=100,
        description="Tracks with popularity strictly below this value qualify",
    ),
    spotify: tk.Spotify = Depends(get_spotify),
) -> list[dict[str, Any]]:
    """Discover lesser-known tracks by a given artist.

    Returns tracks whose popularity is strictly below *threshold*.
    Walks the artist's full discography, so large catalogues take a
    while and count against Spotify rate limits.
    """
    engine = DiscoveryEngine(spotify)
    try:
        tracks = await engine.find_deep_cuts(artist_id=artist_id, popularity_threshold=threshold)
    except tk.HTTPError as exc:
        logger.error("Failed to fetch deep cuts for artist %s: %s", artist_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve deep cuts from Spotify.",
        ) from exc
    return [track_to_dict(t) for t in tracks]


@discovery_router.post(
    "/genre-playlist",
    response_model=PlaylistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a genre-based playlist",
)
async def create_genre_playlist(
    genre: str = Query(..., min_length=1, description="Genre seed (e.g. 'indie-rock')"),
    limit: int = Query(default=30, ge=1, le=50, description="Number of tracks (max 50)"),
    playlist_name: str | None = Query(
        default=None, description="Custom playlist name (auto-generated if omitted)"
    ),
    current_user: User = Depends(get_current_user),
    spotify: tk.Spotify = Depends(get_spotify),
) -> Playlist:
    """Create a new playlist populated with tracks from a genre search."""
    engine = DiscoveryEngine(spotify)
    manager = PlaylistManager(spotify)
    try:
        tracks = await engine.build_genre_playlist(genre=genre, limit=limit)
        playlist = await manager.create_playlist_with_tracks(
            name=playlist_name or f"SpotifyForge: {genre.title()}",
            owner_id=current_user.id,  # type: ignore[arg-type]
            tracks=tracks,
            description=f"Genre playlist: {genre}",
        )
    except tk.HTTPError as exc:
        logger.error("Genre playlist creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create genre-based playlist.",
        ) from exc
    return playlist


@discovery_router.post(
    "/time-capsule",
    response_model=PlaylistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a time capsule playlist",
)
async def create_time_capsule(
    time_range: str = Query(
        default="long_term",
        pattern="^(short_term|medium_term|long_term)$",
        description="Listening-history window to snapshot",
    ),
    playlist_name: str | None = Query(
        default=None, description="Custom playlist name (auto-generated if omitted)"
    ),
    current_user: User = Depends(get_current_user),
    spotify: tk.Spotify = Depends(get_spotify),
) -> Playlist:
    """Create a snapshot playlist of the user's top tracks for a time range."""
    engine = DiscoveryEngine(spotify)
    manager = PlaylistManager(spotify)
    try:
        tracks = await engine.build_time_capsule(time_range=time_range)
        playlist = await manager.create_playlist_with_tracks(
            name=playlist_name or f"SpotifyForge: Time Capsule ({time_range})",
            owner_id=current_user.id,  # type: ignore[arg-type]
            tracks=tracks,
            description=f"Time capsule playlist ({time_range})",
            public=False,
        )
    except tk.HTTPError as exc:
        logger.error("Time capsule creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create time capsule playlist.",
        ) from exc
    return playlist


# =========================================================================
# Schedule Router
# =========================================================================
schedule_router = APIRouter(prefix="/api/schedules", tags=["schedules"])

# Job types that operate on an existing playlist.
_PLAYLIST_JOB_TYPES = {JobType.sync, JobType.archive, JobType.deduplicate, JobType.genre_refresh}


def _validate_job_spec(body: ScheduledJobCreate) -> None:
    """Reject job specs that could never execute."""
    if validate_cron(body.cron_expression) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid cron expression {body.cron_expression!r}. "
                "Expected 5 fields: 'minute hour day month day_of_week'."
            ),
        )
    if body.job_type in _PLAYLIST_JOB_TYPES and body.playlist_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Job type '{body.job_type}' requires a playlist_id.",
        )
    config = body.config or {}
    if body.job_type is JobType.archive and not config.get("source_playlist_id"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Archive jobs require 'source_playlist_id' in config.",
        )
    if body.job_type is JobType.genre_refresh and not config.get("genre"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Genre-refresh jobs require 'genre' in config.",
        )


@schedule_router.get(
    "",
    response_model=list[ScheduledJobResponse],
    summary="List scheduled jobs",
)
async def list_schedules(
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=50, ge=1, le=100, description="Page size"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ScheduledJob]:
    """Return the authenticated user's scheduled jobs, newest first."""
    stmt = (
        select(ScheduledJob)
        .where(ScheduledJob.user_id == current_user.id)
        .order_by(col(ScheduledJob.created_at).desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@schedule_router.post(
    "",
    response_model=ScheduledJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a scheduled job",
)
async def create_schedule(
    body: ScheduledJobCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledJob:
    """Register a new recurring automation job.

    The spec is validated up front (cron syntax, required playlist and
    config for the job type), so a 201 means a job that can really run.
    """
    _validate_job_spec(body)

    if body.playlist_id is not None:
        result = await db.execute(
            select(Playlist).where(
                Playlist.id == body.playlist_id,
                Playlist.owner_id == current_user.id,
            )
        )
        if result.scalars().first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Playlist {body.playlist_id} not found.",
            )

    job = ScheduledJob(
        user_id=current_user.id,
        name=body.name,
        job_type=body.job_type,
        playlist_id=body.playlist_id,
        config=body.config,
        cron_expression=body.cron_expression,
        enabled=body.enabled,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if job.enabled:
        register_job(job)

    return job


@schedule_router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scheduled job",
)
async def delete_schedule(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a scheduled job from the database and the live scheduler."""
    result = await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.id == job_id,
            ScheduledJob.user_id == current_user.id,
        )
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled job {job_id} not found.",
        )

    unregister_job(job_id)
    await db.delete(job)
    await db.commit()


@schedule_router.put(
    "/{job_id}/toggle",
    response_model=ScheduledJobResponse,
    summary="Toggle a scheduled job",
)
async def toggle_schedule(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledJob:
    """Enable or disable a scheduled job, syncing the live scheduler."""
    result = await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.id == job_id,
            ScheduledJob.user_id == current_user.id,
        )
    )
    job = result.scalars().first()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scheduled job {job_id} not found.",
        )

    job.enabled = not job.enabled
    job.updated_at = datetime.now(UTC)
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if job.enabled:
        register_job(job)
    else:
        unregister_job(job_id)

    return job
