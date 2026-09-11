"""Finding duplicate occurrences in a target playlist.

Duplicates arise legitimately from cross-platform mapping: several distinct
Spotify tracks — a single, an album cut, a "(feat. X)" variant — can resolve to
one YouTube upload. The write path prevents new ones, but playlists written
before that guard existed still carry them, and a user can create them by hand.

Kept separate from the job engine on purpose: this operates on what is *actually
in the playlist right now*, not on what the database believes was written. After
manual edits those two disagree, and for a destructive operation the playlist
itself has to be the source of truth.
"""

from __future__ import annotations

from .models import PlaylistEntry


def find_duplicates(entries: list[PlaylistEntry]) -> list[PlaylistEntry]:
    """Return the occurrences to remove, keeping the earliest of each track.

    Keeping the *first* preserves playlist ordering as the user originally saw
    it: removing the earlier copy and keeping a later one would silently move
    the track down the playlist.
    """
    seen: set[str] = set()
    extras: list[PlaylistEntry] = []
    for entry in sorted(entries, key=lambda e: e.position):
        if entry.track_id in seen:
            extras.append(entry)
        else:
            seen.add(entry.track_id)
    return extras


def group_duplicates(
    entries: list[PlaylistEntry],
) -> dict[str, list[PlaylistEntry]]:
    """Duplicated track ids mapped to all of their occurrences, for display."""
    by_track: dict[str, list[PlaylistEntry]] = {}
    for entry in sorted(entries, key=lambda e: e.position):
        by_track.setdefault(entry.track_id, []).append(entry)
    return {k: v for k, v in by_track.items() if len(v) > 1}
