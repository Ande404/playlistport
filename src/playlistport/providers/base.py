"""The provider contract.

Every platform implements this and nothing else. Adding Apple Music later means
writing one file here, not touching the matcher, the job engine, or the UI.

The interface is deliberately **synchronous**: both spotipy and ytmusicapi are
blocking clients, so an async facade would be theatre. FastAPI runs sync
dependencies in a threadpool, and the one place parallelism genuinely pays —
search fan-out — uses an explicit ThreadPoolExecutor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import Candidate, CanonicalTrack, PlaylistRef


class ProviderError(RuntimeError):
    """Anything the caller cannot fix by retrying."""


class AuthRequired(ProviderError):
    """Credentials are missing or expired; the user must re-authorize."""


class QuotaExceeded(ProviderError):
    """A hard daily ceiling was hit. Resume tomorrow rather than retrying."""


class MusicProvider(ABC):
    #: Stable short name, stored in the database alongside every mapped ID.
    name: str

    #: How many tracks to send per write request. Spotify accepts 100 per call;
    #: YouTube has no batch insert and each track costs 50 quota units, so it
    #: writes one at a time and the engine checkpoints after each.
    write_batch_size: int = 50

    # -- read ---------------------------------------------------------------

    @abstractmethod
    def list_playlists(self) -> list[PlaylistRef]:
        """Playlists the user owns or follows."""

    @abstractmethod
    def get_tracks(self, playlist_id: str) -> list[CanonicalTrack]:
        """Every track in a playlist, in order, fully paginated."""

    @abstractmethod
    def get_saved_tracks(self) -> list[CanonicalTrack]:
        """The user's Liked Songs / saved library."""

    # -- match --------------------------------------------------------------

    #: Whether this platform can resolve a recording by ISRC. Apple Music,
    #: Tidal and Deezer can; YouTube Music exposes no ISRC at all. Declaring it
    #: lets the engine skip fuzzy matching entirely for an exact identity.
    supports_isrc_lookup: bool = False

    @abstractmethod
    def search(self, track: CanonicalTrack, limit: int = 8) -> list[Candidate]:
        """Candidate matches for a track originating on another platform."""

    def lookup_by_isrc(self, isrc: str) -> Candidate | None:
        """Resolve a recording by exact identifier, if the platform supports it.

        Returning None means "not found" *or* "unsupported" — both cases fall
        through to fuzzy search, so a provider that cannot do this needs no
        implementation.
        """
        return None

    # -- write --------------------------------------------------------------

    @abstractmethod
    def create_playlist(self, name: str, description: str = "") -> str:
        """Create an empty playlist and return its ID."""

    @abstractmethod
    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Append tracks, batching as the platform requires."""

    # -- helpers ------------------------------------------------------------

    def resolve_playlist(self, needle: str) -> PlaylistRef:
        """Find a playlist by exact ID, exact name, or unique case-insensitive
        substring. Raises rather than guessing when a substring is ambiguous."""
        playlists = self.list_playlists()
        for playlist in playlists:
            if playlist.id == needle:
                return playlist
        exact = [p for p in playlists if p.name.casefold() == needle.casefold()]
        if len(exact) == 1:
            return exact[0]
        partial = [p for p in playlists if needle.casefold() in p.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = ", ".join(repr(p.name) for p in partial[:8])
            raise ProviderError(f"{needle!r} is ambiguous — matches {names}")
        raise ProviderError(f"No playlist matching {needle!r} on {self.name}")
