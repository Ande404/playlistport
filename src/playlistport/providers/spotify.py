"""Spotify provider (spotipy, Authorization Code with PKCE)."""

from __future__ import annotations

import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from ..config import interactive_session, load_config
from ..core.models import Candidate, CanonicalTrack, PlaylistEntry, PlaylistRef
from ..core.normalize import canonical_isrc, search_terms
from .base import AuthRequired, MusicProvider, ProviderError

SCOPES = " ".join(
    [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
        "user-library-read",
        "user-library-modify",
    ]
)

#: Spotify caps playlist additions at 100 URIs per request.
ADD_BATCH = 100


class SpotifyProvider(MusicProvider):
    name = "spotify"
    write_batch_size = ADD_BATCH
    supports_isrc_lookup = True

    def __init__(self) -> None:
        self._config = load_config()
        self._config.require_spotify()
        self._client: spotipy.Spotify | None = None

    # -- auth ---------------------------------------------------------------

    @property
    def client(self) -> spotipy.Spotify:
        if self._client is None:
            # Unattended, a browser flow hangs rather than fails: spotipy
            # would open a browser nobody sees and then wait on stdin. Refuse
            # to start one unless a human is present to finish it.
            interactive = interactive_session()
            if not interactive and not self._config.spotify_token_cache.exists():
                raise AuthRequired(
                    "No cached Spotify credentials and no terminal to authorize "
                    "in. Run 'playlistport auth spotify' interactively."
                )
            auth = SpotifyOAuth(
                client_id=self._config.spotify_client_id,
                client_secret=self._config.spotify_client_secret,
                redirect_uri=self._config.spotify_redirect_uri,
                scope=SCOPES,
                cache_path=str(self._config.spotify_token_cache),
                open_browser=interactive,
            )
            self._client = spotipy.Spotify(auth_manager=auth, retries=3)
        return self._client

    def authenticate(self) -> str:
        """Force the OAuth flow and return the account display name."""
        try:
            me = self.client.current_user()
        except spotipy.SpotifyException as exc:  # pragma: no cover - network
            raise AuthRequired(f"Spotify authorization failed: {exc}") from exc
        return me.get("display_name") or me.get("id", "unknown")

    # -- read ---------------------------------------------------------------

    def _paginate(self, page: dict) -> list[dict]:
        """Walk every page, and refuse to return a short read.

        Under concurrent load Spotify has been observed ending pagination early
        — a 303-track playlist came back as 207 with no error. Silently
        returning a truncated list is the worst outcome once writes are
        involved: the transfer would create an incomplete playlist and record it
        as fully synced, so the missing tracks would never be retried. Failing
        loudly is recoverable; quietly losing a third of a playlist is not.
        """
        total = page.get("total")
        items = list(page.get("items", []))
        while page.get("next"):
            page = self.client.next(page)
            if not page:
                break
            items.extend(page.get("items", []))

        if total is not None and len(items) < total:
            raise ProviderError(
                f"Spotify returned {len(items)} of {total} items — the response "
                "was truncated, most likely by rate limiting. Retry, and lower "
                "--workers if it persists."
            )
        return items

    def list_playlists(self) -> list[PlaylistRef]:
        me = self.client.current_user()["id"]
        items = self._paginate(self.client.current_user_playlists(limit=50))
        out = []
        for item in items:
            if not item:  # Spotify occasionally returns null entries
                continue
            owner = (item.get("owner") or {}).get("id")
            tracks = item.get("tracks")
            out.append(
                PlaylistRef(
                    id=item["id"],
                    name=item.get("name") or "(untitled)",
                    track_count=tracks.get("total") if isinstance(tracks, dict) else None,
                    owner=owner,
                    description=item.get("description"),
                    is_owned=owner == me,
                )
            )
        return out

    def _to_track(self, item: dict, position: int) -> CanonicalTrack | None:
        """Convert a playlist item, or return None if it is not a usable track.

        Local files carry no ID and cannot be matched by metadata alone;
        episodes are podcasts, not music. Both are dropped here and reported as
        skips rather than being allowed to fail deeper in the pipeline.
        """
        # Spotify returns the payload under "track" on some endpoints and under
        # an undocumented "item" key on others (playlist_items now does the
        # latter). Missing this silently yields an empty playlist rather than an
        # error, so accept both, plus a bare track object.
        track = item.get("track") or item.get("item")
        if track is None and item.get("id"):
            track = item
        if not track:
            return None
        if track.get("type") == "episode":
            return None
        if item.get("is_local") or track.get("is_local"):
            return None
        if not track.get("id"):
            return None
        # A track with no name cannot be matched by any means, and sending an
        # empty query to a search API is an error rather than a miss.
        if not (track.get("name") or "").strip():
            return None

        return CanonicalTrack(
            title=track.get("name") or "",
            artists=[a["name"] for a in track.get("artists", []) if a.get("name")],
            duration_ms=track.get("duration_ms"),
            album=(track.get("album") or {}).get("name"),
            isrc=(track.get("external_ids") or {}).get("isrc"),
            source_id=track["id"],
            source_provider=self.name,
            position=position,
        )

    def get_tracks(self, playlist_id: str) -> list[CanonicalTrack]:
        page = self.client.playlist_items(
            playlist_id,
            limit=100,
            additional_types=("track",),
        )
        out = []
        for index, item in enumerate(self._paginate(page)):
            track = self._to_track(item, index)
            if track:
                out.append(track)
        return out

    def get_saved_tracks(self) -> list[CanonicalTrack]:
        page = self.client.current_user_saved_tracks(limit=50)
        out = []
        for index, item in enumerate(self._paginate(page)):
            track = self._to_track(item, index)
            if track:
                out.append(track)
        return out

    # -- match --------------------------------------------------------------

    def search(self, track: CanonicalTrack, limit: int = 8) -> list[Candidate]:
        """Field-scoped query first, then a loose fallback.

        The scoped form (`track:"x" artist:"y"`) is precise but brittle — it
        returns nothing when the incoming YouTube metadata names the channel
        rather than the artist, which is common. The loose query rescues those.
        """
        title = search_terms(track.title, track.artists) or track.title
        artist = track.primary_artist
        if not (title or artist).strip():
            return []

        # Progressively looser. The scoped form is precise but returns nothing
        # when the incoming YouTube "artist" is really an uploader channel; the
        # bare-title query is the last resort for exactly that case.
        queries = []
        if artist:
            queries.append(f'track:"{title}" artist:"{artist}"')
            queries.append(f"{title} {artist}")
        queries.append(title)

        seen: set[str] = set()
        candidates: list[Candidate] = []
        for query in queries:
            for item in self._search_raw(query, limit):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                candidates.append(
                    Candidate(
                        id=item["id"],
                        title=item.get("name") or "",
                        artists=[
                            a["name"] for a in item.get("artists", []) if a.get("name")
                        ],
                        duration_ms=item.get("duration_ms"),
                        album=(item.get("album") or {}).get("name"),
                        isrc=(item.get("external_ids") or {}).get("isrc"),
                        provider=self.name,
                        raw={"uri": item.get("uri")},
                    )
                )
            if len(candidates) >= limit:
                break
        return candidates[:limit]

    def _search_raw(self, query: str, limit: int) -> list[dict]:
        try:
            result = self.client.search(q=query, type="track", limit=limit)
        except spotipy.SpotifyException as exc:  # pragma: no cover - network
            if exc.http_status == 429:
                time.sleep(int(exc.headers.get("Retry-After", 3)) + 1)
                result = self.client.search(q=query, type="track", limit=limit)
            else:
                raise ProviderError(f"Spotify search failed: {exc}") from exc
        return (result.get("tracks") or {}).get("items", [])

    def lookup_by_isrc(self, isrc: str) -> Candidate | None:
        """Exact recording lookup via Spotify's `isrc:` search filter."""
        folded = canonical_isrc(isrc)
        if not folded:
            return None
        items = self._search_raw(f"isrc:{folded}", limit=1)
        if not items:
            return None
        item = items[0]
        return Candidate(
            id=item["id"],
            title=item.get("name") or "",
            artists=[a["name"] for a in item.get("artists", []) if a.get("name")],
            duration_ms=item.get("duration_ms"),
            album=(item.get("album") or {}).get("name"),
            isrc=(item.get("external_ids") or {}).get("isrc"),
            provider=self.name,
            raw={"uri": item.get("uri")},
        )

    # -- write --------------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        user_id = self.client.current_user()["id"]
        playlist = self.client.user_playlist_create(
            user_id, name, public=False, description=description
        )
        return playlist["id"]

    # -- removal ------------------------------------------------------------

    supports_removal = True

    def list_entries(self, playlist_id: str) -> list[PlaylistEntry]:
        """Raw occurrences. Spotify has no per-entry id, so position is the handle."""
        page = self.client.playlist_items(
            playlist_id, limit=100, additional_types=("track",)
        )
        entries: list[PlaylistEntry] = []
        for position, item in enumerate(self._paginate(page)):
            track = item.get("track") or item.get("item")
            if not track or not track.get("id"):
                continue
            artists = ", ".join(
                a["name"] for a in track.get("artists", []) if a.get("name")
            )
            entries.append(
                PlaylistEntry(
                    entry_id=str(position),
                    track_id=track["id"],
                    position=position,
                    label=f"{track.get('name', '')} — {artists}",
                )
            )
        return entries

    def remove_entries(self, playlist_id: str, entries: list[PlaylistEntry]) -> None:
        """Remove specific occurrences by position.

        Positions shift as items are deleted, so every position is sent in a
        single request evaluated against one snapshot. Passing `snapshot_id`
        makes the server reject the whole call if the playlist changed
        underneath us, rather than silently deleting the wrong rows.
        """
        if not entries:
            return
        snapshot = self.client.playlist(playlist_id, fields="snapshot_id").get(
            "snapshot_id"
        )
        by_track: dict[str, list[int]] = {}
        for entry in entries:
            by_track.setdefault(entry.track_id, []).append(entry.position)

        items = [
            {"uri": f"spotify:track:{track_id}", "positions": sorted(positions)}
            for track_id, positions in by_track.items()
        ]
        for start in range(0, len(items), ADD_BATCH):
            self.client.playlist_remove_specific_occurrences_of_items(
                playlist_id, items[start : start + ADD_BATCH], snapshot_id=snapshot
            )

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        uris = [
            tid if tid.startswith("spotify:") else f"spotify:track:{tid}"
            for tid in track_ids
        ]
        for start in range(0, len(uris), ADD_BATCH):
            self.client.playlist_add_items(playlist_id, uris[start : start + ADD_BATCH])
