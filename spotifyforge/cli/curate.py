"""`spotifyforge curate` commands."""

from __future__ import annotations

import typer
from rich import box
from rich.panel import Panel
from rich.table import Table

from spotifyforge.cli._shared import (
    _db_user_id,
    _error_panel,
    _run,
    _run_spotify,
    console,
)
from spotifyforge.config import settings

# ╔═════════════════════════════════════════════════════════════════════════╗
# ║  CURATE                                                                ║
# ╚═════════════════════════════════════════════════════════════════════════╝
curate_app = typer.Typer(
    name="curate",
    help="Forge a catalogue of niche playlists from your liked songs.",
    no_args_is_help=True,
)

# Shared option definitions so `plan`, `forge` and `reflow` cannot drift
# apart — a plan that previewed different clusters than the forge creates
# would make the preview worthless.
_MIN_SIZE = typer.Option(
    12, "--min-size", min=2, help="Smallest genre cluster that becomes a playlist."
)
_MAX_SIZE = typer.Option(
    60, "--max-size", min=10, help="Clusters larger than this are split by decade."
)
_MAX_TRACKS = typer.Option(
    None, "--max-tracks", min=1, help="Only scan the first N liked songs (quick preview)."
)
_EXCLUSIVE = typer.Option(
    False,
    "--exclusive",
    help="Put each track in only its rarest genre (fewer, sharper playlists).",
)
_HARMONIC = typer.Option(
    False,
    "--harmonic",
    help="Sequence by musical key and BPM (needs 'curate features' first).",
)


def _features_or_warn(harmonic: bool):
    """Load cached tempo/key data, explaining if there is none yet."""
    if not harmonic:
        return None
    from spotifyforge.core.audio_features import load_cached_features

    features = load_cached_features()
    if not features:
        _error_panel(
            "No tempo/key data cached yet.\n"
            "Run [bold]spotifyforge curate features --deep[/bold] first.",
            title="Nothing to sequence by",
        )
    keyed = sum(1 for f in features.values() if f.has_key)
    console.print(
        f"[dim]Harmonic ordering: {len(features)} tracks analysed, {keyed} with a key.[/dim]"
    )
    return features


async def _plan(sp, opts, features=None):
    """Plan the catalogue with the pinned expansions folded in.

    Every curate command plans through here: a command that forgot the
    pins would hand reflow a plan that strips pinned tracks off live
    playlists, so the folding is not optional at the call sites.
    """
    from spotifyforge.core.curation import plan_catalogue
    from spotifyforge.core.expansion import load_expansions

    return await plan_catalogue(sp, opts, features, load_expansions())


def _curation_options(min_size, max_size, max_tracks, exclusive):
    from spotifyforge.core.curation import CurationOptions

    return CurationOptions(
        min_size=min_size, max_size=max_size, max_tracks=max_tracks, exclusive=exclusive
    )


def _specs_table(specs) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
    table.add_column("Genre", style="green", no_wrap=True, max_width=30)
    table.add_column("Era", style="magenta")
    table.add_column("Tracks", justify="right")
    for idx, spec in enumerate(specs, start=1):
        table.add_row(
            str(idx),
            spec.title,
            spec.genre_label,
            f"{spec.decade}s" if spec.decade else "\u2014",
            str(len(spec.tracks)),
        )
    return table


@curate_app.command("plan")
def curate_plan(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Preview the playlist catalogue your liked songs would produce (no writes)."""

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)
    plan = _run_spotify(
        "Scanning your liked songs...",
        "Failed to plan curation",
        lambda sp: _plan(sp, opts, features),
    )

    console.print(
        Panel(
            f"Liked songs scanned:   [bold]{plan.liked_count}[/bold]\n"
            f"Unique songs:          [bold]{plan.unique_count}[/bold] "
            f"({plan.collapsed_count} duplicate versions collapsed)\n"
            f"Playlists planned:     [bold]{len(plan.specs)}[/bold]\n"
            f"Songs placed:          [bold]{plan.placed_liked_count}[/bold] of "
            f"{plan.unique_count} "
            f"({plan.unplaced_count} in genres too small to fill a playlist)\n"
            f"Playlist entries:      [bold]{plan.entry_count}[/bold] across "
            f"{plan.placed_count} distinct songs (pinned discoveries included)\n"
            f"Sequenced by key+BPM:  [bold]{plan.harmonic_count}[/bold] of {len(plan.specs)} "
            "(the rest had too little key data)",
            title="Curation Plan",
            border_style="cyan",
            expand=False,
        )
    )
    if plan.specs:
        console.print(_specs_table(plan.specs))
        console.print(
            "\nRun [bold]spotifyforge curate forge --limit N[/bold] to create them "
            "(already-created titles are skipped, so repeated runs continue the catalogue)."
        )


@curate_app.command("forge")
def curate_forge(
    limit: int = typer.Option(
        5, "--limit", "-l", min=1, help="Maximum playlists to create this run."
    ),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
    private: bool = typer.Option(
        False, "--private", help="Create the playlists as private instead of public."
    ),
) -> None:
    """Create the next batch of planned playlists on Spotify (resumable)."""
    from spotifyforge.core.curation import forge_next, writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager

    owner_id = _db_user_id()
    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _forge(sp):
        plan = await _plan(sp, opts, features)
        specs = writable_specs(plan.specs, min_size)
        created, pending = await forge_next(
            PlaylistManager(sp), owner_id, specs, limit, public=not private
        )
        return created, pending, len(specs)

    created, pending, total = _run_spotify(
        "Forging playlists from your liked songs...",
        "Failed to forge playlists",
        _forge,
    )

    if not created:
        console.print(
            Panel(
                f"All {total} planned playlists already exist \u2014 nothing to create.",
                title="Catalogue complete",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Created {len(created)} playlist(s).[/green]\n"
            f"Remaining in plan: [bold]{pending - len(created)}[/bold] of {total} \u2014 "
            "run the same command again to continue.",
            title="Forge",
            border_style="green",
            expand=False,
        )
    )
    console.print(_specs_table([spec for spec, _ in created]))


@curate_app.command("follow-artists")
def curate_follow_artists(
    limit: int = typer.Option(
        50, "--limit", "-l", min=1, help="Maximum artists to follow this run."
    ),
    min_liked: int = typer.Option(
        2, "--min-liked", min=1, help="Only artists with at least this many liked songs."
    ),
    max_tracks: int | None = _MAX_TRACKS,
    apply: bool = typer.Option(
        False, "--apply", help="Actually follow them (default is a dry run)."
    ),
) -> None:
    """Follow the artists behind your liked songs.

    Ranks every artist credited on your saved tracks by how many of them
    are theirs and follows the top ones, skipping any you already
    follow. Dry run by default: it prints exactly who the next
    [bold]--apply[/bold] would follow.

    This follows artists only. Bulk-following listeners to collect
    follow-backs is what Spotify's rules prohibit — use
    [bold]curate curators[/bold] for a shortlist of people worth
    following, and choose among them yourself.
    """
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.following import follow_artists, rank_candidates, unfollowed

    async def _follow(sp):
        tracks = await CurationEngine(sp).fetch_liked(max_tracks=max_tracks)
        ranked = rank_candidates(tracks, min_liked=min_liked)
        pending_ids = set(await unfollowed(sp, [c.id for c in ranked]))
        pending = [c for c in ranked if c.id in pending_ids][:limit]
        if not apply:
            return pending, [], len(ranked), len(tracks)
        followed, failed = await follow_artists(sp, pending)
        return pending, failed, len(ranked), len(tracks)

    pending, failed, ranked_count, scanned = _run_spotify(
        "Ranking the artists behind your liked songs...",
        "Failed to follow artists",
        _follow,
    )

    if not pending:
        console.print(
            Panel(
                f"You already follow every artist with {min_liked}+ liked songs "
                f"({ranked_count} of them, from {scanned} saved tracks).",
                title="Nothing to follow",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Artist")
    table.add_column("Liked songs", justify="right")
    for candidate in pending:
        table.add_row(candidate.name, str(candidate.liked_tracks))
    console.print(table)

    verb = "Followed" if apply else "Would follow"
    body = (
        f"[bold]{verb} {len(pending)}[/bold] artist(s), from {ranked_count} with "
        f"{min_liked}+ liked songs across {scanned} saved tracks."
    )
    if failed:
        body += f"\n[yellow]{len(failed)} could not be followed.[/yellow]"
    if not apply:
        body += "\n\nRe-run with [bold]--apply[/bold] to follow them."
    console.print(Panel(body, title="Follow artists", border_style="green", expand=False))


@curate_app.command("curators")
def curate_curators(
    limit: int = typer.Option(25, "--limit", "-l", min=1, help="How many curators to list."),
    genres: int = typer.Option(12, "--genres", min=1, help="How many of your genres to search."),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Find curators whose playlists overlap your liked songs.

    Read-only: it lists people worth following, it does not follow
    anyone. Mass-following strangers to collect follow-backs is the
    artificial-engagement pattern Spotify's rules prohibit, and it risks
    the account it is meant to grow.
    """
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.curators import find_curators, top_genres

    async def _find(sp):
        engine = CurationEngine(sp)
        tracks = await engine.enrich_genres(await engine.fetch_liked(max_tracks=max_tracks))
        me = await sp.current_user()
        seeds = top_genres(tracks, count=genres)
        liked_ids = {t.id for t in tracks}
        return await find_curators(sp, seeds, liked_ids, me.id, limit=limit), seeds, len(tracks)

    curators, seeds, scanned = _run_spotify(
        "Searching for curators who share your taste...",
        "Failed to find curators",
        _find,
    )

    console.print(
        Panel(
            f"Liked songs scanned: [bold]{scanned}[/bold]\n"
            f"Genres searched:     {', '.join(seeds[:6])}"
            + (f" (+{len(seeds) - 6} more)" if len(seeds) > 6 else "")
            + f"\nCurators found:      [bold]{len(curators)}[/bold]",
            title="Curator search",
            border_style="cyan",
            expand=False,
        )
    )
    if not curators:
        console.print("No curators with overlapping taste turned up. Try [bold]--genres 20[/bold].")
        return

    # The profile URL rides on the curator's name as a terminal hyperlink
    # rather than taking a column of its own — five columns do not fit an
    # 80-character terminal, and the overlap count is the part to read.
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Curator", style="white", overflow="ellipsis", max_width=30)
    table.add_column("Shared", justify="right", style="bold green")
    table.add_column("Their playlist", style="magenta", overflow="ellipsis", max_width=34)
    for idx, c in enumerate(curators, start=1):
        table.add_row(
            str(idx),
            f"[link={c.url}]{c.display_name}[/link]",
            str(c.shared_tracks),
            c.example_playlist,
        )
    console.print(table)
    console.print("\n[dim]Profiles:[/dim]")
    for idx, c in enumerate(curators, start=1):
        console.print(f"  [dim]{idx:>2}.[/dim] {c.url}")
    console.print(
        "\n[dim]'Shared' counts your own liked songs found in that curator's playlist. "
        "Open a profile and follow the ones you actually like — that is the kind of "
        "follow Spotify rewards.[/dim]"
    )


@curate_app.command("covers")
def curate_covers(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace covers that are already set."
    ),
    photos: bool = typer.Option(
        False,
        "--photos",
        help="Use licensed photos (Pexels) matched to each playlist's vibe, "
        "covering personal playlists too. Needs SPOTIFYFORGE_PEXELS_API_KEY.",
    ),
    repick_people: bool = typer.Option(
        False,
        "--repick-people",
        help="With --photos: re-roll only the covers whose photo shows a person.",
    ),
) -> None:
    """Give your playlists cover art.

    By default each forged playlist gets generated art whose colour is
    derived from its genre — stable across runs, one collection. With
    [bold]--photos[/bold], every owned playlist instead gets a licensed
    photograph matched to its vibe; picks are pinned locally so re-runs
    are stable, and a Pexels rate limit pauses the run resumably rather
    than failing it.
    """
    if photos:
        _photo_covers(min_size, max_size, max_tracks, exclusive, overwrite, repick_people)
        return

    from spotifyforge.core.curation import apply_covers, writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)

    async def _covers(sp):
        plan = await _plan(sp, opts)
        specs = writable_specs(plan.specs, min_size)
        uploaded, failed = await apply_covers(PlaylistManager(sp), sp, specs, overwrite=overwrite)
        return uploaded, failed, len(specs)

    uploaded, failed, total = _run_spotify(
        "Painting playlist covers...", "Failed to set covers", _covers
    )

    if not uploaded:
        console.print(
            Panel(
                f"All {total} playlists already have artwork "
                "(use [bold]--overwrite[/bold] to replace it).",
                title="Nothing to paint",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Set artwork on {len(uploaded)} playlist(s)[/green] of {total}."
            + (f"\n[yellow]{len(failed)} failed[/yellow]" if failed else ""),
            title="Covers",
            border_style="green",
            expand=False,
        )
    )


def _photo_covers(
    min_size, max_size, max_tracks, exclusive, overwrite, repick_people=False
) -> None:
    """The --photos path of ``curate covers``: every owned playlist."""
    from spotifyforge.core.curation import writable_specs
    from spotifyforge.core.photo_covers import (
        PexelsSource,
        apply_photo_covers,
        drop_person_picks,
        picks_path,
    )
    from spotifyforge.core.playlist_manager import PlaylistManager

    if not settings.pexels_api_key:
        _error_panel(
            "Photo covers need a Pexels key.\n"
            "Get a free one at pexels.com/api and set "
            "[bold]SPOTIFYFORGE_PEXELS_API_KEY[/bold].",
            title="Pexels key missing",
        )

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)

    # Forgetting the pick is what re-rolls it, so this runs before the
    # plan: the covers run that follows sees those playlists as unpinned
    # and picks again, this time past the photo the filter now rejects.
    reroll = drop_person_picks() if repick_people else None
    if reroll is not None:
        if not reroll:
            console.print(
                Panel(
                    "No pinned cover shows a person.",
                    title="Nothing to re-roll",
                    border_style="green",
                    expand=False,
                )
            )
            return
        console.print(f"[dim]Re-rolling {len(reroll)} cover(s): {', '.join(sorted(reroll))}[/dim]")
        overwrite = True

    async def _photo(sp):
        plan = await _plan(sp, opts)
        vibe_by_title = {s.title: s.genre_label for s in writable_specs(plan.specs, min_size)}
        me = await sp.current_user()
        owned = [
            p for p in await PlaylistManager(sp).get_user_playlists() if p["owner_id"] == me.id
        ]
        # Forged playlists search by genre; personal ones by their name.
        targets = [(p["name"], p["id"], vibe_by_title.get(p["name"], p["name"])) for p in owned]
        if reroll is not None:
            targets = [t for t in targets if t[0] in set(reroll)]
        source = PexelsSource(settings.pexels_api_key)
        try:
            covered, failed, limited = await apply_photo_covers(
                sp, targets, source, overwrite=overwrite
            )
        finally:
            await source.close()
        return covered, failed, limited, len(targets)

    covered, failed, limited, total = _run_spotify(
        "Matching photographs to playlists...", "Failed to set photo covers", _photo
    )

    body = f"[green]Photo-covered {len(covered)} playlist(s)[/green] of {total}."
    if failed:
        body += f"\n[yellow]{len(failed)} had no usable photo[/yellow] (kept their current art)."
    if limited:
        body += (
            "\n[yellow]Pexels' hourly limit reached[/yellow] — progress is saved; "
            "re-run in an hour to continue."
        )
    if not covered and not failed and not limited:
        body = f"All {total} playlists already have pinned photos (use [bold]--overwrite[/bold])."
    console.print(Panel(body, title="Photo covers", border_style="green", expand=False))
    console.print(f"[dim]Picks + attribution: {picks_path()}[/dim]")


@curate_app.command("describe")
def curate_describe(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Refresh forged playlists' descriptions from the current templates.

    Descriptions are the only text besides the name that Spotify's
    search indexes. This rewrites each forged playlist's description to
    the current template — leading with the playlist's own artists —
    without touching tracks, titles, followers, or artwork. Playlists
    already carrying the wanted text are skipped.
    """
    from spotifyforge.core.curation import apply_descriptions, writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _push(sp):
        plan = await _plan(sp, opts, features)
        specs = writable_specs(plan.specs, min_size)
        updated, failed = await apply_descriptions(PlaylistManager(sp), sp, specs)
        return updated, failed, len(specs)

    updated, failed, total = _run_spotify(
        "Rewriting playlist descriptions...", "Failed to update descriptions", _push
    )

    if not updated and not failed:
        console.print(
            Panel(
                f"All {total} playlists already carry the current descriptions.",
                title="Nothing to rewrite",
                border_style="green",
                expand=False,
            )
        )
        return

    console.print(
        Panel(
            f"[green]Updated {len(updated)} description(s)[/green] of {total}."
            + (f"\n[yellow]{len(failed)} failed[/yellow]" if failed else ""),
            title="Descriptions",
            border_style="green",
            expand=False,
        )
    )


@curate_app.command("expand")
def curate_expand(
    target: int = typer.Option(
        12, "--target", min=3, help="Grow playlists below this many tracks."
    ),
    limit: int = typer.Option(
        10, "--limit", "-l", min=1, help="Maximum playlists to expand this run."
    ),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Grow thin playlists with unheard tracks from the same niche.

    Searches Spotify for more of a playlist's genre — music you have
    never heard — and pins the picks locally. Nothing is written to
    Spotify here: run [bold]curate reflow[/bold] afterwards to push the
    grown playlists (and [bold]curate describe[/bold] to refresh their
    descriptions). Repeat runs continue where the last one stopped.
    """
    from spotifyforge.core.curation import plan_catalogue
    from spotifyforge.core.expansion import expand_catalogue, expansions_path, load_expansions

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _expand(sp):
        # Loaded once and shared: the plan folds these pins in, and
        # expand_catalogue appends this run's picks to the same dict.
        pins = load_expansions()
        plan = await plan_catalogue(sp, opts, features, pins)
        return await expand_catalogue(sp, plan.specs, target=target, limit=limit, expansions=pins)

    added, thin = _run_spotify(
        "Digging for unheard tracks...", "Failed to expand playlists", _expand
    )

    if not added:
        console.print(
            Panel(
                f"No playlists below {target} tracks had unheard music to pin "
                f"({thin} are below the target).",
                title="Nothing to expand",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
    table.add_column("Pinned", justify="right")
    table.add_column("New artists", style="green", max_width=50)
    for title, tracks in added.items():
        artists = ", ".join(dict.fromkeys(t.artist_names[0] for t in tracks if t.artist_names))
        table.add_row(title, str(len(tracks)), artists)
    console.print(table)
    console.print(
        Panel(
            f"[green]Pinned {sum(len(t) for t in added.values())} track(s) across "
            f"{len(added)} playlist(s)[/green] of {thin} below the target; "
            "re-run to continue.\n"
            "Run [bold]spotifyforge curate reflow[/bold] to push them to Spotify.",
            title="Expanded",
            border_style="green",
            expand=False,
        )
    )
    console.print(f"[dim]Pins: {expansions_path()}[/dim]")


@curate_app.command("stats")
def curate_stats() -> None:
    """Snapshot follower counts and show growth since the last run.

    Spotify keeps no follower history, so growth is only measurable if
    each run records what it saw. Snapshots accumulate locally; run this
    on any cadence and it reports the change since the previous run.
    """
    from spotifyforge.core.stats import record_snapshot

    snapshot, growth, path = _run_spotify(
        "Counting followers...", "Failed to read follower counts", record_snapshot
    )

    lines = [
        f"Account followers:   [bold]{snapshot.account_followers}[/bold]",
        f"Owned playlists:     [bold]{len(snapshot.playlists)}[/bold]",
        f"Playlist followers:  [bold]{snapshot.playlist_followers}[/bold] "
        f"across {snapshot.followed_playlists} playlist(s)",
    ]
    if growth is None:
        lines.append("First snapshot — the next run will show growth.")
    else:
        lines.append(
            f"Since {growth.since.split('T')[0]}:  "
            f"account [bold]{growth.account_delta:+d}[/bold], "
            f"playlist followers [bold]{growth.playlist_delta:+d}[/bold]"
        )
    console.print(Panel("\n".join(lines), title="Growth", border_style="cyan", expand=False))

    if growth is not None and growth.movers:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Playlist", style="white", no_wrap=True, max_width=45)
        table.add_column("Followers", justify="right")
        for name, delta in growth.movers[:10]:
            table.add_row(name, f"{delta:+d}")
        console.print(table)

    console.print(f"[dim]History: {path}[/dim]")


@curate_app.command("features")
def curate_features(
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Also fetch musical key via MusicBrainz/AcousticBrainz (~1 track/sec).",
    ),
    max_tracks: int | None = _MAX_TRACKS,
) -> None:
    """Fetch tempo and key for your liked songs, caching them on disk.

    Spotify's own audio-features endpoint is withdrawn for this app, so
    tempo comes from Deezer and musical key from AcousticBrainz, both
    looked up by ISRC. Results are cached, so this is slow once and
    instant afterwards; re-run it after liking new music.
    """
    from spotifyforge.core.audio_features import feature_cache_path, gather_features
    from spotifyforge.core.curation import CurationEngine
    from spotifyforge.core.expansion import load_expansions

    async def _fetch(sp):
        return await CurationEngine(sp).fetch_liked(max_tracks=max_tracks)

    tracks = _run_spotify("Reading your liked songs...", "Failed to read library", _fetch)
    # Pinned expansion tracks play in the same playlists, so they need
    # tempo/key just as much as the liked songs they sit between.
    pinned = [t for entries in load_expansions().values() for t in entries]
    isrcs = sorted({t.isrc for t in [*tracks, *pinned] if t.isrc})

    if deep:
        console.print(
            f"[yellow]Deep lookup resolves {len(isrcs)} recordings in batches of 25, "
            "pausing between MusicBrainz calls as their rate limit asks.[/yellow]"
        )

    from rich.progress import Progress

    with Progress(transient=True) as progress:
        task = progress.add_task("Looking up tempo/key...", total=len(isrcs))
        features, learned = _run(
            gather_features(isrcs, deep=deep, progress=lambda: progress.advance(task))
        )

    analysed = sum(1 for f in features.values() if f.tempo is not None)
    keyed = sum(1 for f in features.values() if f.has_key)
    console.print(
        Panel(
            f"Recordings with an ISRC: [bold]{len(isrcs)}[/bold] "
            f"from {len(tracks)} liked + {len(pinned)} pinned tracks\n"
            f"Newly resolved:          [bold]{learned}[/bold]\n"
            f"Tempo known:             [bold]{analysed}[/bold]\n"
            f"Key known:               [bold]{keyed}[/bold]"
            + ("" if deep else "  [dim](use --deep to fetch keys)[/dim]")
            + f"\nCache: {feature_cache_path()}",
            title="Audio features",
            border_style="cyan",
            expand=False,
        )
    )
    if keyed:
        console.print(
            "\nRun [bold]spotifyforge curate reflow --harmonic[/bold] to re-sequence "
            "your playlists by key and BPM."
        )


@curate_app.command("reflow")
def curate_reflow(
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Re-sequence playlists you already forged, keeping their URLs and followers."""
    from spotifyforge.core.curation import reflow, writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _reflow(sp):
        plan = await _plan(sp, opts, features)
        specs = writable_specs(plan.specs, min_size)
        rewritten, failed = await reflow(PlaylistManager(sp), sp, specs)
        return rewritten, failed, len(specs)

    rewritten, failed, total = _run_spotify(
        "Re-sequencing your forged playlists...", "Failed to reflow playlists", _reflow
    )

    if not rewritten:
        console.print(
            Panel(
                f"All {total} planned playlists are already in the right order.",
                title="Nothing to reflow",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Playlist", style="white", no_wrap=True, max_width=55)
    table.add_column("Tracks", justify="right")
    for idx, (title, count) in enumerate(rewritten, start=1):
        table.add_row(str(idx), title, str(count))

    console.print(
        Panel(
            f"[green]Re-sequenced {len(rewritten)} playlist(s)[/green] of {total} planned."
            + (f"\n[yellow]{len(failed)} could not be updated[/yellow]" if failed else ""),
            title="Reflow",
            border_style="green",
            expand=False,
        )
    )
    console.print(table)


@curate_app.command("retire")
def curate_retire(
    apply: bool = typer.Option(
        False, "--apply", help="Actually retire them (default: list them and stop)."
    ),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Retire at most this many."),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Retire forged playlists the catalogue no longer plans.

    A better genre assignment leaves playlists behind holding songs that
    now belong elsewhere — the duplication the assignment fix was for.
    This unfollows them; Spotify has no delete, and keeps them
    recoverable from the web client for about ninety days.

    A playlist is only ever a candidate if its description is one this
    tool generated. Anything you wrote yourself is listed as kept and is
    never touched, whatever its name or size.

    Lists them and stops unless [bold]--apply[/bold] is passed.
    """
    from spotifyforge.core.curation import writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.retiring import plan_retirements, retire_playlists

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _retire(sp):
        plan = await _plan(sp, opts, features)
        me = await sp.current_user()
        owned = [
            p for p in await PlaylistManager(sp).get_user_playlists() if p["owner_id"] == me.id
        ]
        live = {p["name"]: p.get("description") or "" for p in owned}
        ids_by_name = {p["name"]: p["id"] for p in owned}

        planned = {s.title for s in writable_specs(plan.specs, min_size)}
        retirable, kept = plan_retirements(live, planned)
        retirable = retirable[: limit or None]
        if not apply:
            return retirable, kept, [], False
        retired, failed = await retire_playlists(sp, ids_by_name, retirable)
        return retired, kept, failed, True

    retirable, kept, failed, applied = _run_spotify(
        "Retiring playlists..." if apply else "Working out what is no longer planned...",
        "Failed to retire playlists",
        _retire,
    )

    if not retirable:
        console.print(
            Panel(
                f"Nothing to retire. {len(kept)} unplanned playlist(s) are yours, not forged.",
                title="Nothing to retire",
                border_style="green",
                expand=False,
            )
        )
        return

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("Retiring" if applied else "Would retire")
    for name in retirable[:40]:
        table.add_row(name)
    console.print(table)
    if len(retirable) > 40:
        console.print(f"[dim]…and {len(retirable) - 40} more[/dim]")

    verb = "Retired" if applied else "Would retire"
    body = f"[bold]{verb} {len(retirable)}[/bold] forged playlist(s) the catalogue no longer plans."
    body += (
        f"\n[bold]{len(kept)}[/bold] unplanned playlist(s) kept — you wrote those, not this tool."
    )
    if failed:
        body += f"\n[yellow]{len(failed)} could not be retired.[/yellow]"
    if applied:
        body += "\nSpotify keeps them recoverable from the web client for about 90 days."
    else:
        body += "\nNothing has changed — re-run with [bold]--apply[/bold] to do it."
    console.print(Panel(body, title="Retire", border_style="green", expand=False))


@curate_app.command("migrate")
def curate_migrate(
    apply: bool = typer.Option(
        False, "--apply", help="Actually rename (default: show the mapping and stop)."
    ),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Move live playlists onto a re-clustered catalogue, by their songs.

    Use this after the clustering itself changes — a different genre
    assignment renames most of the catalogue, because titles are derived
    from the genres. [bold]rename[/bold] cannot follow that: it re-derives
    the old title from the genre, and the genre is what moved.

    This matches each planned playlist to the live one that already
    holds its songs and renames that one in place, so followers,
    artwork, descriptions and search ranking survive. Playlists with no
    live counterpart are reported as new — run [bold]forge[/bold] for
    those, then [bold]reflow[/bold] to fix every tracklist.

    Shows the mapping and stops unless [bold]--apply[/bold] is passed.
    """
    from functools import partial

    from spotifyforge.core.curation import gather_bounded, writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.renaming import apply_renames, match_by_contents

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _migrate(sp):
        plan = await _plan(sp, opts, features)
        manager = PlaylistManager(sp)
        me = await sp.current_user()
        owned = [p for p in await manager.get_user_playlists() if p["owner_id"] == me.id]

        async def _contents(playlist):
            # playlist_items yields PlaylistTrack wrappers, not tracks;
            # reading .id off the wrapper silently gives None for every
            # row, which matches nothing and reports a confident zero.
            items = await manager.get_playlist_tracks(playlist["id"])
            held = {i.track.id for i in items if i.track is not None and i.track.id is not None}
            return playlist["name"], held

        live = dict(await gather_bounded([partial(_contents, p) for p in owned]))
        if owned and not any(live.values()):
            # Every match is decided by these sets. Reading them all as
            # empty is a bug, and it fails as "nothing to rename" —
            # indistinguishable from a clean catalogue.
            raise RuntimeError(
                f"Read no tracks from any of {len(owned)} owned playlists; "
                "refusing to report a migration based on that."
            )
        renames, unmatched = match_by_contents(writable_specs(plan.specs, min_size), live)
        if not apply:
            return renames, unmatched, [], [], False
        renamed, already, failed = await apply_renames(manager, sp, renames)
        return renamed, unmatched, already, failed, True

    renames, unmatched, already, failed, applied = _run_spotify(
        "Renaming playlists..." if apply else "Matching playlists to their songs...",
        "Failed to migrate playlists",
        _migrate,
    )

    if renames:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Was", style="dim", max_width=42)
        table.add_column("Now", style="green", max_width=42)
        for rename in renames[:40]:
            table.add_row(rename.old, rename.new)
        console.print(table)
        if len(renames) > 40:
            console.print(f"[dim]…and {len(renames) - 40} more[/dim]")

    verb = "Renamed" if applied else "Would rename"
    body = f"[bold]{verb} {len(renames)}[/bold] playlist(s) in place."
    if already:
        body += f"\n{len(already)} already carried the new name."
    if failed:
        body += f"\n[yellow]{len(failed)} could not be renamed.[/yellow]"
    body += f"\n[bold]{len(unmatched)}[/bold] planned playlist(s) have no live counterpart."
    if applied:
        body += (
            "\nFollowers, artwork and descriptions are unchanged.\n"
            "Next: [bold]curate forge[/bold] for the new ones, then "
            "[bold]curate reflow[/bold]."
        )
    else:
        body += "\nNothing has changed — re-run with [bold]--apply[/bold] to do it."
    console.print(Panel(body, title="Migrate", border_style="green", expand=False))


@curate_app.command("rename")
def curate_rename(
    apply: bool = typer.Option(
        False, "--apply", help="Actually rename (default: show the mapping and stop)."
    ),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Rename at most this many."),
    min_size: int = _MIN_SIZE,
    max_size: int = _MAX_SIZE,
    max_tracks: int | None = _MAX_TRACKS,
    exclusive: bool = _EXCLUSIVE,
    harmonic: bool = _HARMONIC,
) -> None:
    """Move forged playlists onto the current naming scheme.

    Playlist identity here is the title, so changing the naming scheme
    without renaming the live playlists would make the next
    [bold]forge[/bold] create duplicates and strand the originals —
    followers, artwork and all. This renames them in place instead, and
    carries their cover picks across.

    Shows the mapping and stops unless [bold]--apply[/bold] is passed.
    Playlists already carrying their new name are skipped, so an
    interrupted run can simply be re-run.
    """
    from spotifyforge.core.curation import writable_specs
    from spotifyforge.core.playlist_manager import PlaylistManager
    from spotifyforge.core.renaming import apply_renames, plan_renames

    opts = _curation_options(min_size, max_size, max_tracks, exclusive)
    features = _features_or_warn(harmonic)

    async def _rename(sp):
        plan = await _plan(sp, opts, features)
        renames = plan_renames(writable_specs(plan.specs, min_size))[: limit or None]
        if not apply:
            return renames, [], [], False
        renamed, already, failed = await apply_renames(PlaylistManager(sp), sp, renames)
        return renamed, already, failed, True

    renames, already, failed, applied = _run_spotify(
        "Renaming playlists..." if apply else "Working out the new names...",
        "Failed to rename playlists",
        _rename,
    )

    if not renames and not already:
        console.print(
            Panel(
                "Every playlist already carries its current name.",
                title="Nothing to rename",
                border_style="green",
                expand=False,
            )
        )
        return

    if renames:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("Was", style="dim", max_width=44)
        table.add_column("Now", style="green", max_width=44)
        for rename in renames[:40]:
            table.add_row(rename.old, rename.new)
        console.print(table)
        if len(renames) > 40:
            console.print(f"[dim]…and {len(renames) - 40} more[/dim]")

    if applied:
        body = f"[green]Renamed {len(renames)} playlist(s)[/green] in place."
        if already:
            body += f"\n{len(already)} already carried the new name."
        if failed:
            body += f"\n[yellow]{len(failed)} could not be renamed[/yellow] (not found on Spotify)."
        body += "\nFollowers, artwork and descriptions are unchanged."
    else:
        body = (
            f"[bold]{len(renames)} playlist(s)[/bold] would be renamed.\n"
            "Nothing has changed — re-run with [bold]--apply[/bold] to do it."
        )
    console.print(Panel(body, title="Rename", border_style="green", expand=False))
