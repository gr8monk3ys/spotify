"""API route definitions for SpotifyForge.

Organizes all endpoints into four ``APIRouter`` instances:

* **auth_router** -- authentication and session management
* **playlist_router** -- CRUD and operations on Spotify playlists
* **discovery_router** -- music discovery and recommendation features
* **schedule_router** -- scheduled automation job management

Each router is included by :func:`spotifyforge.web.app.create_app`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from spotifyforge.models.models import (
    CurationEvalLog,
    CurationRule,
    CurationRuleCreate,
    CurationRuleResponse,
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
from spotifyforge.security import encrypt_token, hash_token

logger = logging.getLogger("spotifyforge.web.routes")


# ---------------------------------------------------------------------------
# Dependency injection helpers (imported from dedicated module to avoid
# circular imports with app.py)
# ---------------------------------------------------------------------------
from spotifyforge.web.deps import get_current_user, get_db_session

# =========================================================================
# Auth Router
# =========================================================================
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.get("/login", summary="Get Spotify login URL")
async def auth_login() -> dict[str, str]:
    """Return the Spotify authorization URL.

    The front-end should redirect the user's browser to the returned URL
    so they can grant SpotifyForge access to their Spotify account.
    """
    from spotifyforge.auth.oauth import build_auth_url

    auth_url = build_auth_url()
    return {"auth_url": auth_url}


@auth_router.get(
    "/callback",
    response_class=RedirectResponse,
    summary="Handle OAuth callback",
)
async def auth_callback(
    request: Request,
    code: str = Query(..., description="Authorization code from Spotify"),
    state: str | None = Query(default=None, description="Anti-CSRF state parameter"),
) -> RedirectResponse:
    """Handle the Spotify OAuth callback.

    Exchanges the authorization code for tokens, upserts the user record
    in the database, sets a session cookie, and redirects to the
    front-end dashboard.
    """
    from spotifyforge.auth.oauth import exchange_code, get_spotify_user
    from spotifyforge.web.app import get_db_session

    try:
        token_info = await exchange_code(code, state=state)
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

    # Obtain a database session manually (not via Depends in redirect handler)
    session_gen = get_db_session()
    db: AsyncSession = await session_gen.__anext__()
    try:
        result = await db.execute(select(User).where(User.spotify_id == spotify_user["id"]))
        user = result.scalars().first()

        if user is None:
            user = User(
                spotify_id=spotify_user["id"],
                display_name=spotify_user.get("display_name"),
                email=spotify_user.get("email"),
                access_token_enc=encrypt_token(token_info["access_token"]),
                refresh_token_enc=encrypt_token(token_info["refresh_token"])
                if token_info.get("refresh_token")
                else None,
                token_expiry=datetime.fromtimestamp(token_info["expires_at"], tz=UTC)
                if token_info.get("expires_at")
                else None,
                token_hash=hash_token(token_info["access_token"]),
                is_premium=spotify_user.get("product") == "premium",
            )
            db.add(user)
        else:
            user.access_token_enc = encrypt_token(token_info["access_token"])
            user.refresh_token_enc = (
                encrypt_token(token_info["refresh_token"])
                if token_info.get("refresh_token")
                else user.refresh_token_enc
            )
            user.token_expiry = (
                datetime.fromtimestamp(token_info["expires_at"], tz=UTC)
                if token_info.get("expires_at")
                else None
            )
            user.token_hash = hash_token(token_info["access_token"])
            user.display_name = spotify_user.get("display_name", user.display_name)
            user.email = spotify_user.get("email", user.email)
            user.is_premium = spotify_user.get("product") == "premium"
            user.updated_at = datetime.now(UTC)
            db.add(user)

        await db.commit()
        await db.refresh(user)

        response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key="spotifyforge_user_id",
            value=str(user.id),
            httponly=True,
            secure=request.url.scheme == "https",  # Secure in production
            samesite="lax",
            max_age=60 * 60 * 24 * 7,  # 7 days (was 30)
        )
        return response
    finally:
        await db.close()


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
async def auth_logout() -> dict[str, str]:
    """Clear the session cookie and log the user out.

    The front-end should discard any cached auth state after calling
    this endpoint.
    """
    response_data = {"detail": "Logged out successfully."}
    from fastapi.responses import JSONResponse

    response = JSONResponse(content=response_data)
    response.delete_cookie(
        key="spotifyforge_user_id",
        httponly=True,
        samesite="lax",
    )
    return response  # type: ignore[return-value]


# =========================================================================
# Playlist Router
# =========================================================================
playlist_router = APIRouter(prefix="/api/playlists", tags=["playlists"])


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
    """Return the authenticated user's playlists with pagination.

    Results are ordered by most recently updated first.
    """
    stmt = (
        select(Playlist)
        .where(Playlist.owner_id == current_user.id)
        .order_by(Playlist.updated_at.desc())  # type: ignore[union-attr]
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
    db: AsyncSession = Depends(get_db_session),
) -> Playlist:
    """Create a new Spotify playlist and register it in SpotifyForge.

    The playlist is created on Spotify first, then stored locally for
    tracking and automation purposes.
    """
    from spotifyforge.core.playlists import create_spotify_playlist

    try:
        spotify_playlist = await create_spotify_playlist(
            user=current_user,
            name=body.name,
            description=body.description,
            public=body.public,
            collaborative=body.collaborative,
        )
    except Exception as exc:
        logger.error("Spotify playlist creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create playlist on Spotify.",
        ) from exc

    playlist = Playlist(
        spotify_id=spotify_playlist["id"],
        owner_id=current_user.id,  # type: ignore[arg-type]
        name=body.name,
        description=body.description,
        public=body.public,
        collaborative=body.collaborative,
        snapshot_id=spotify_playlist.get("snapshot_id"),
    )
    db.add(playlist)
    await db.commit()
    await db.refresh(playlist)
    return playlist


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
    """Return full details for a specific playlist, including track list.

    Only the playlist owner can access this endpoint.
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )
    return playlist


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
) -> Playlist:
    """Update the name, description, or visibility of a playlist.

    Only non-``None`` fields in the request body are applied.
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(playlist, field, value)

    playlist.updated_at = datetime.now(UTC)
    db.add(playlist)

    # Sync changes to Spotify
    from spotifyforge.core.playlists import update_spotify_playlist

    try:
        await update_spotify_playlist(
            user=current_user,
            spotify_id=playlist.spotify_id,
            **update_data,
        )
    except Exception as exc:
        logger.warning("Failed to sync playlist update to Spotify: %s", exc)

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
) -> dict[str, Any]:
    """Trigger a full sync of the playlist from Spotify.

    Pulls the latest track listing, metadata, and snapshot ID from
    Spotify and updates the local database.
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )

    from spotifyforge.core.playlists import sync_playlist_from_spotify

    try:
        sync_result = await sync_playlist_from_spotify(
            user=current_user,
            playlist=playlist,
            db=db,
        )
    except Exception as exc:
        logger.error("Playlist sync failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to sync playlist from Spotify.",
        ) from exc

    playlist.last_synced_at = datetime.now(UTC)
    playlist.updated_at = datetime.now(UTC)
    db.add(playlist)
    await db.commit()

    return {
        "detail": "Playlist synced successfully.",
        "playlist_id": playlist_id,
        "tracks_synced": sync_result.get("tracks_synced", 0),
    }


@playlist_router.post(
    "/{playlist_id}/deduplicate",
    summary="Remove duplicate tracks",
)
async def deduplicate_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Scan the playlist for duplicate tracks and remove them.

    Duplicates are identified by Spotify track URI and, where available,
    by ISRC code to catch re-releases.
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )

    from spotifyforge.core.playlists import deduplicate_playlist_tracks

    try:
        dedup_result = await deduplicate_playlist_tracks(
            user=current_user,
            playlist=playlist,
            db=db,
        )
    except Exception as exc:
        logger.error("Deduplication failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to deduplicate playlist.",
        ) from exc

    return {
        "detail": "Deduplication complete.",
        "playlist_id": playlist_id,
        "duplicates_removed": dedup_result.get("duplicates_removed", 0),
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
) -> dict[str, Any]:
    """Add one or more tracks to a playlist by Spotify URI.

    Accepts a JSON array of Spotify track URIs (e.g.
    ``["spotify:track:abc123"]``).
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )

    if not uris:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one track URI is required.",
        )

    from spotifyforge.core.playlists import add_tracks_to_playlist

    try:
        add_result = await add_tracks_to_playlist(
            user=current_user,
            playlist=playlist,
            track_uris=uris,
            db=db,
        )
    except Exception as exc:
        logger.error("Failed to add tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to add tracks to playlist on Spotify.",
        ) from exc

    return {
        "detail": "Tracks added successfully.",
        "playlist_id": playlist_id,
        "tracks_added": add_result.get("tracks_added", len(uris)),
        "snapshot_id": add_result.get("snapshot_id"),
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
) -> dict[str, Any]:
    """Remove one or more tracks from a playlist by Spotify URI.

    Accepts a JSON array of Spotify track URIs to remove.
    """
    result = await db.execute(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.owner_id == current_user.id,
        )
    )
    playlist = result.scalars().first()
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Playlist {playlist_id} not found.",
        )

    if not uris:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one track URI is required.",
        )

    from spotifyforge.core.playlists import remove_tracks_from_playlist

    try:
        remove_result = await remove_tracks_from_playlist(
            user=current_user,
            playlist=playlist,
            track_uris=uris,
            db=db,
        )
    except Exception as exc:
        logger.error("Failed to remove tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to remove tracks from playlist on Spotify.",
        ) from exc

    return {
        "detail": "Tracks removed successfully.",
        "playlist_id": playlist_id,
        "tracks_removed": remove_result.get("tracks_removed", len(uris)),
        "snapshot_id": remove_result.get("snapshot_id"),
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
    current_user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return the user's top tracks from Spotify.

    Powered by the Spotify ``/me/top/tracks`` endpoint.  Results can be
    scoped to short-term (~4 weeks), medium-term (~6 months), or
    long-term (all time).
    """
    from spotifyforge.core.discovery import get_top_tracks

    try:
        tracks = await get_top_tracks(
            user=current_user,
            time_range=time_range,
            limit=limit,
        )
    except Exception as exc:
        logger.error("Failed to fetch top tracks: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve top tracks from Spotify.",
        ) from exc

    return tracks


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
    current_user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Return the user's top artists from Spotify.

    Powered by the Spotify ``/me/top/artists`` endpoint.
    """
    from spotifyforge.core.discovery import get_top_artists

    try:
        artists = await get_top_artists(
            user=current_user,
            time_range=time_range,
            limit=limit,
        )
    except Exception as exc:
        logger.error("Failed to fetch top artists: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve top artists from Spotify.",
        ) from exc

    return artists


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
        description="Maximum popularity score to qualify as a deep cut",
    ),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Discover lesser-known tracks by a given artist.

    Returns tracks whose popularity score falls at or below the provided
    *threshold*.  Lower thresholds yield deeper cuts.
    """
    from spotifyforge.core.discovery import get_deep_cuts

    try:
        tracks = await get_deep_cuts(
            user=current_user,
            artist_id=artist_id,
            threshold=threshold,
        )
    except Exception as exc:
        logger.error("Failed to fetch deep cuts for artist %s: %s", artist_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to retrieve deep cuts from Spotify.",
        ) from exc

    return tracks


@discovery_router.post(
    "/genre-playlist",
    response_model=PlaylistResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a genre-based playlist",
)
async def create_genre_playlist(
    genre: str = Query(..., min_length=1, description="Genre seed (e.g. 'indie-rock')"),
    limit: int = Query(default=30, ge=1, le=100, description="Number of tracks"),
    playlist_name: str | None = Query(
        default=None, description="Custom playlist name (auto-generated if omitted)"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Playlist:
    """Generate and create a new playlist populated with tracks from a genre.

    Uses Spotify's recommendation engine seeded with the specified genre.
    """
    from spotifyforge.core.discovery import create_genre_based_playlist

    try:
        playlist = await create_genre_based_playlist(
            user=current_user,
            genre=genre,
            limit=limit,
            playlist_name=playlist_name,
            db=db,
        )
    except Exception as exc:
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
    year: int | None = Query(default=None, ge=1900, le=2100, description="Target year"),
    month: int | None = Query(default=None, ge=1, le=12, description="Target month"),
    playlist_name: str | None = Query(
        default=None, description="Custom playlist name (auto-generated if omitted)"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Playlist:
    """Create a nostalgia playlist based on the user's listening history.

    Optionally filter by a specific year and/or month to revisit music
    from that period.
    """
    from spotifyforge.core.discovery import create_time_capsule_playlist

    try:
        playlist = await create_time_capsule_playlist(
            user=current_user,
            year=year,
            month=month,
            playlist_name=playlist_name,
            db=db,
        )
    except Exception as exc:
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


@schedule_router.get(
    "",
    response_model=list[ScheduledJobResponse],
    summary="List scheduled jobs",
)
async def list_schedules(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ScheduledJob]:
    """Return all scheduled automation jobs for the authenticated user.

    Jobs are sorted by creation date, newest first.
    """
    stmt = (
        select(ScheduledJob)
        .where(ScheduledJob.user_id == current_user.id)
        .order_by(ScheduledJob.created_at.desc())  # type: ignore[union-attr]
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

    The job is persisted in the database and, if enabled, registered with
    the background scheduler immediately.
    """
    # Validate that the referenced playlist exists if provided
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
        user_id=current_user.id,  # type: ignore[arg-type]
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

    # Register with the live scheduler if enabled
    if job.enabled:
        from spotifyforge.core.scheduler import register_job

        try:
            register_job(job)
        except Exception as exc:
            logger.warning("Failed to register job with scheduler: %s", exc)

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
    """Delete a scheduled job.

    The job is removed from both the database and the live scheduler.
    """
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

    # Unregister from the live scheduler
    from spotifyforge.core.scheduler import unregister_job

    try:
        unregister_job(job)
    except Exception as exc:
        logger.warning("Failed to unregister job from scheduler: %s", exc)

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
    """Enable or disable a scheduled job.

    Flips the ``enabled`` flag and registers or unregisters the job with
    the background scheduler accordingly.
    """
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

    # Sync with live scheduler
    from spotifyforge.core.scheduler import register_job, unregister_job

    try:
        if job.enabled:
            register_job(job)
        else:
            unregister_job(job)
    except Exception as exc:
        logger.warning("Failed to sync job toggle with scheduler: %s", exc)

    return job


# =========================================================================
# Curation Router
# =========================================================================
curation_router = APIRouter(prefix="/api/curation", tags=["curation"])


@curation_router.get("/rules", response_model=list[CurationRuleResponse])
async def list_curation_rules(
    playlist_id: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """List curation rules for the current user, optionally filtered by playlist."""
    stmt = select(CurationRule).where(CurationRule.user_id == current_user.id)
    if playlist_id is not None:
        stmt = stmt.where(CurationRule.playlist_id == playlist_id)
    stmt = stmt.order_by(CurationRule.priority)  # type: ignore[arg-type]
    result = await db.execute(stmt)
    return list(result.scalars().all())


@curation_router.post("/rules", response_model=CurationRuleResponse, status_code=201)
async def create_curation_rule(
    body: CurationRuleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Create a new curation rule."""
    rule = CurationRule(
        user_id=current_user.id,
        name=body.name,
        rule_type=body.rule_type,
        playlist_id=body.playlist_id,
        conditions=body.conditions,
        actions=body.actions,
        enabled=body.enabled,
        priority=body.priority,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@curation_router.delete("/rules/{rule_id}", status_code=204)
async def delete_curation_rule(
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a curation rule."""
    result = await db.execute(
        select(CurationRule).where(
            CurationRule.id == rule_id,
            CurationRule.user_id == current_user.id,
        )
    )
    rule = result.scalars().first()
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found.")
    await db.delete(rule)
    await db.commit()


@curation_router.post("/rules/{playlist_id}/dry-run")
async def dry_run_curation(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Dry-run curation rules on a playlist without making changes."""
    from spotifyforge.core.curation_engine import dry_run

    # Fetch rules
    result = await db.execute(
        select(CurationRule).where(
            CurationRule.playlist_id == playlist_id,
            CurationRule.user_id == current_user.id,
            CurationRule.enabled == True,  # noqa: E712
        ).order_by(CurationRule.priority)  # type: ignore[arg-type]
    )
    rules = list(result.scalars().all())

    if not rules:
        return {"message": "No enabled rules for this playlist", "eval_log": []}

    # For dry run, we need the playlist's tracks — return a simplified result
    rule_dicts = [
        {
            "name": r.name,
            "rule_type": r.rule_type,
            "conditions": r.conditions or {},
            "actions": r.actions or {},
            "enabled": r.enabled,
            "priority": r.priority,
        }
        for r in rules
    ]

    # Use empty tracks for now (real implementation would fetch from DB)
    report = dry_run(tracks=[], audio_features_map={}, rules=rule_dicts)
    report["rules_count"] = len(rules)
    return report


@curation_router.get("/logs/{playlist_id}")
async def get_curation_logs(
    playlist_id: int,
    limit: int = Query(default=20, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Get curation evaluation log entries for a playlist."""
    result = await db.execute(
        select(CurationEvalLog)
        .where(
            CurationEvalLog.playlist_id == playlist_id,
            CurationEvalLog.user_id == current_user.id,
        )
        .order_by(CurationEvalLog.executed_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "id": log.id,
            "rules_applied": log.rules_applied,
            "tracks_before": log.tracks_before,
            "tracks_after": log.tracks_after,
            "details": log.details,
            "executed_at": log.executed_at.isoformat() if log.executed_at else None,
        }
        for log in logs
    ]


# =========================================================================
# Recommendation Router
# =========================================================================
recommend_router = APIRouter(prefix="/api/recommend", tags=["recommendations"])


@recommend_router.get("/taste-profile")
async def get_taste_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Compute the user's taste profile from their top tracks."""
    from spotifyforge.core.recommender import compute_taste_profile
    from spotifyforge.models.models import AudioFeatures

    # Get all audio features for user's playlist tracks
    result = await db.execute(select(AudioFeatures).limit(200))
    features = result.scalars().all()

    af_dicts = [
        {
            "energy": af.energy,
            "danceability": af.danceability,
            "valence": af.valence,
            "acousticness": af.acousticness,
            "instrumentalness": af.instrumentalness,
            "speechiness": af.speechiness,
            "liveness": af.liveness,
            "tempo": af.tempo,
        }
        for af in features
    ]

    profile = compute_taste_profile(af_dicts)
    return {"profile": profile, "tracks_analyzed": len(af_dicts)}


@recommend_router.get("/circuit-breakers")
async def get_circuit_breaker_status() -> dict[str, Any]:
    """Get the status of all circuit breakers (ops/debugging endpoint)."""
    from spotifyforge.core.circuit_breaker import get_all_breakers

    breakers = get_all_breakers()
    return {name: cb.stats() for name, cb in breakers.items()}
