"""YouTube / YouTube Music provider — the hybrid described in DESIGN.md §2.1.

    read  (playlists, items) -> official Data API   ~1 quota unit, negligible
    search                   -> ytmusicapi, ANONYMOUS, free
    enrich (videoId -> song) -> ytmusicapi, ANONYMOUS, free
    write (create, insert)   -> official Data API   50 units each

Search is the expensive call on the official API (100 units, vs 1 for a read),
and it is also the one that returns the *wrong kind of result* — music videos,
lyric videos and 8-hour loops instead of catalog songs. Routing just that call
through ytmusicapi both removes the quota wall and materially improves match
quality, while no credentials are ever handed to the unofficial client.

Remaining ceiling: 10,000 / 50 = ~198 tracks written per day. Flip
YT_WRITE_MODE=ytmusicapi to bypass it (requires browser auth headers).
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import load_config
from ..core.models import Candidate, CanonicalTrack, PlaylistEntry, PlaylistRef
from ..core.normalize import canonical_title, search_terms, split_artists
from .base import AuthRequired, MusicProvider, ProviderError, QuotaExceeded

SCOPES = ["https://www.googleapis.com/auth/youtube"]

#: Data API quota costs, for pre-flight estimation.
COST_LIST = 1
COST_INSERT = 50
DAILY_QUOTA = 10_000

#: Throttled searches come back as unparseable bodies; retry before failing.
SEARCH_ATTEMPTS = 3
SEARCH_BACKOFF = 1.5

#: 409 is a concurrency conflict ("The operation was aborted"); 5xx is YouTube
#: being briefly unavailable. Both are retryable and were observed in practice.
#: Enrichment fan-out. Kept modest: throttling is triggered by concurrency, and
#: a throttled enrichment degrades metadata rather than failing loudly.
ENRICH_WORKERS = 6
ENRICH_ATTEMPTS = 3

TRANSIENT_STATUSES = frozenset({409, 500, 502, 503, 504})
WRITE_ATTEMPTS = 4
WRITE_BACKOFF = 1.0

#: Channel-name suffixes YouTube appends that are never part of an artist name.
_CHANNEL_NOISE_RE = re.compile(r"\s*-\s*topic\s*$|\bvevo\b", re.IGNORECASE)

#: "Artist - Title" in a raw video title, used only when enrichment fails.
_VIDEO_TITLE_RE = re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+)$")


def _parse_duration(value: str | int | None) -> int | None:
    """Accept seconds, or 'M:SS' / 'H:MM:SS', and return milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) * 1000
    parts = str(value).strip().split(":")
    if not all(p.isdigit() for p in parts) or not parts:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def _clean_artists(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        cleaned = _CHANNEL_NOISE_RE.sub("", name or "").strip()
        if cleaned and cleaned.casefold() not in {"various artists", "youtube"}:
            out.extend(split_artists(cleaned))
    return out


class YouTubeProvider(MusicProvider):
    name = "youtube"
    #: No batch insert exists, and each track costs 50 quota units, so the
    #: engine checkpoints after every single write.
    write_batch_size = 1

    def __init__(self) -> None:
        self._config = load_config()
        self._youtube = None  # official Data API client, lazy
        self._ytmusic = None  # ytmusicapi client, lazy, anonymous
        self.quota_used = 0

    # -- clients ------------------------------------------------------------

    @property
    def ytmusic(self):
        """Anonymous ytmusicapi client. Never receives credentials."""
        if self._ytmusic is None:
            from ytmusicapi import YTMusic

            self._ytmusic = YTMusic()
        return self._ytmusic

    @property
    def youtube(self):
        if self._youtube is None:
            from googleapiclient.discovery import build

            self._youtube = build(
                "youtube", "v3", credentials=self._credentials(), cache_discovery=False
            )
        return self._youtube

    def _credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        self._config.require_google()
        cache = self._config.google_token_cache
        creds = None
        if cache.exists():
            creds = Credentials.from_authorized_user_info(
                json.loads(cache.read_text()), SCOPES
            )

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Unverified apps in "Testing" status expire refresh tokens
                # after 7 days. This is expected, not a bug — re-run the flow.
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self._config.google_client_secrets_file), SCOPES
            )
            creds = flow.run_local_server(port=8080, prompt="consent")
            cache.write_text(creds.to_json())

        return creds

    def authenticate(self) -> str:
        response = self._execute(
            self.youtube.channels().list(part="snippet", mine=True), COST_LIST
        )
        items = response.get("items") or []
        if not items:
            raise AuthRequired("Google account has no YouTube channel.")
        return items[0]["snippet"]["title"]

    def _execute(self, request, cost: int):
        """Run a Data API request, tracking quota and translating failures.

        Transient statuses are retried rather than surfaced. A real 176-track
        write hit a single HTTP 409 "The operation was aborted" — a concurrency
        conflict on YouTube's side, not a problem with the request. Treating
        that as permanent silently drops one track from the playlist.
        """
        from googleapiclient.errors import HttpError

        for attempt in range(WRITE_ATTEMPTS):
            try:
                result = request.execute()
            except HttpError as exc:
                reason = ""
                try:
                    detail = json.loads(exc.content.decode())
                    reason = detail["error"]["errors"][0].get("reason", "")
                except Exception:
                    pass

                if reason in {
                    "quotaExceeded",
                    "dailyLimitExceeded",
                    "rateLimitExceeded",
                }:
                    raise QuotaExceeded(
                        f"YouTube daily quota exhausted after ~{self.quota_used} "
                        "units. Resume tomorrow, or set YT_WRITE_MODE=ytmusicapi."
                    ) from exc
                if exc.resp.status in (401, 403) and "auth" in reason.lower():
                    raise AuthRequired(f"YouTube authorization failed: {exc}") from exc

                if (
                    exc.resp.status in TRANSIENT_STATUSES
                    and attempt < WRITE_ATTEMPTS - 1
                ):
                    time.sleep(WRITE_BACKOFF * (2**attempt))
                    continue
                raise ProviderError(f"YouTube API error: {exc}") from exc

            self.quota_used += cost
            return result

        raise ProviderError("YouTube API error: retries exhausted")

    # -- read ---------------------------------------------------------------

    def list_playlists(self) -> list[PlaylistRef]:
        out: list[PlaylistRef] = []
        page_token = None
        while True:
            response = self._execute(
                self.youtube.playlists().list(
                    part="snippet,contentDetails",
                    mine=True,
                    maxResults=50,
                    pageToken=page_token,
                ),
                COST_LIST,
            )
            for item in response.get("items", []):
                out.append(
                    PlaylistRef(
                        id=item["id"],
                        name=item["snippet"].get("title") or "(untitled)",
                        track_count=item["contentDetails"].get("itemCount", 0),
                        owner=item["snippet"].get("channelTitle"),
                        description=item["snippet"].get("description"),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                return out

    def get_tracks(self, playlist_id: str) -> list[CanonicalTrack]:
        """List items via the cheap official API, then enrich each with clean
        music metadata from ytmusicapi.

        The Data API returns a video title and a channel name — "Artist - Title
        (Official Video)" and "ArtistVEVO" — which matches poorly against
        Spotify. YouTube Music's catalog entry for the same videoId carries
        proper artist, album and duration fields, and costs nothing to fetch.
        """
        raw_items: list[tuple[str, str, str]] = []
        page_token = None
        seen_items = 0
        expected: int | None = None
        while True:
            response = self._execute(
                self.youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ),
                COST_LIST,
            )
            if expected is None:
                expected = (response.get("pageInfo") or {}).get("totalResults")
            seen_items += len(response.get("items", []))
            for item in response.get("items", []):
                snippet = item["snippet"]
                video_id = item["contentDetails"].get("videoId")
                title = snippet.get("title") or ""
                # Deleted and private videos survive in playlists as tombstones.
                if not video_id or title in {"Deleted video", "Private video"}:
                    continue
                raw_items.append(
                    (video_id, title, snippet.get("videoOwnerChannelTitle") or "")
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # Same guard as the Spotify side: a short read must not pass silently.
        # Tombstones (deleted/private videos) are counted as seen, so this
        # measures pagination completeness, not how many were usable.
        if expected is not None and seen_items < expected:
            raise ProviderError(
                f"YouTube returned {seen_items} of {expected} playlist items — "
                "the response was truncated. Retry the command."
            )

        # Enrichment is one independent network call per track and was the
        # dominant cost of reading a playlist: 180 tracks took over four minutes
        # serially. The calls are free in quota terms, so fan them out — but
        # modestly, since hammering the endpoint is what triggers throttling in
        # the first place. `map` preserves playlist order.
        if not raw_items:
            return []

        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
            return list(
                pool.map(
                    lambda entry: self._enrich(
                        entry[1][0], entry[1][1], entry[1][2], entry[0]
                    ),
                    enumerate(raw_items),
                )
            )

    def _enrich(
        self, video_id: str, video_title: str, channel: str, position: int
    ) -> CanonicalTrack:
        """Resolve a videoId to catalog metadata, falling back to title parsing.

        The fallback is much worse metadata — an uploader channel in place of the
        artist — so a throttled call must be retried rather than allowed to
        degrade the track silently. Only a genuine absence of catalog data
        should reach the fallback.
        """
        track = None
        for attempt in range(ENRICH_ATTEMPTS):
            try:
                watch = self.ytmusic.get_watch_playlist(videoId=video_id, limit=1)
                track = (watch.get("tracks") or [None])[0]
                break
            except Exception:
                if attempt == ENRICH_ATTEMPTS - 1:
                    break
                time.sleep(SEARCH_BACKOFF * (2**attempt))

        if track:
            artists = _clean_artists(
                [a.get("name", "") for a in (track.get("artists") or [])]
            )
            album = (track.get("album") or {}).get("name") if track.get("album") else None
            if artists:
                return CanonicalTrack(
                    title=track.get("title") or video_title,
                    artists=artists,
                    duration_ms=_parse_duration(
                        track.get("length") or track.get("duration_seconds")
                    ),
                    album=album,
                    source_id=video_id,
                    source_provider=self.name,
                    position=position,
                )

        # Fallback: split "Artist - Title", else lean on the channel name.
        stripped, _ = canonical_title(video_title)
        match = _VIDEO_TITLE_RE.match(video_title)
        if match:
            return CanonicalTrack(
                title=match.group("title").strip(),
                artists=_clean_artists([match.group("artist")]),
                source_id=video_id,
                source_provider=self.name,
                position=position,
            )
        return CanonicalTrack(
            title=video_title or stripped,
            artists=_clean_artists([channel]),
            source_id=video_id,
            source_provider=self.name,
            position=position,
        )

    def get_saved_tracks(self) -> list[CanonicalTrack]:
        """YouTube Music's Liked Songs is the 'LM' system playlist."""
        return self.get_tracks("LM")

    # -- match --------------------------------------------------------------

    def search(self, track: CanonicalTrack, limit: int = 8) -> list[Candidate]:
        """Search the YouTube Music catalog via ytmusicapi (free, anonymous).

        Songs are preferred over videos: a song result carries structured
        artist/album/duration, whereas a video result is whatever someone
        uploaded. Videos are only consulted when the song search comes back
        thin, which happens for remixes and regional catalog.
        """
        title = search_terms(track.title, track.artists) or track.title
        query = f"{title} {track.primary_artist}".strip()
        # An empty query is rejected with HTTP 400, not an empty result set.
        if not query:
            return []

        candidates = self._search_raw(query, "songs", limit)
        if len(candidates) < 3:
            candidates += self._search_raw(query, "videos", limit - len(candidates))

        seen: set[str] = set()
        unique: list[Candidate] = []
        for candidate in candidates:
            if candidate.id and candidate.id not in seen:
                seen.add(candidate.id)
                unique.append(candidate)
        return unique[:limit]

    def _search_raw(self, query: str, filter_: str, limit: int) -> list[Candidate]:
        if limit <= 0:
            return []

        # Under load YouTube throttles by returning a non-JSON body, which
        # surfaces as a parse error rather than an HTTP status. These are
        # transient, so back off and retry before giving up on the track.
        results = None
        for attempt in range(SEARCH_ATTEMPTS):
            try:
                results = self.ytmusic.search(query, filter=filter_, limit=limit)
                break
            except Exception as exc:  # unofficial client — degrade, never crash
                if attempt == SEARCH_ATTEMPTS - 1:
                    raise ProviderError(
                        f"YouTube Music search failed after {SEARCH_ATTEMPTS} "
                        f"attempts: {exc}"
                    ) from exc
                time.sleep(SEARCH_BACKOFF * (2**attempt))

        out: list[Candidate] = []
        for item in results:
            video_id = item.get("videoId")
            if not video_id:
                continue
            album = item.get("album")
            out.append(
                Candidate(
                    id=video_id,
                    title=item.get("title") or "",
                    artists=_clean_artists(
                        [a.get("name", "") for a in (item.get("artists") or [])]
                    ),
                    duration_ms=_parse_duration(
                        item.get("duration_seconds") or item.get("duration")
                    ),
                    album=album.get("name") if isinstance(album, dict) else album,
                    provider=self.name,
                    raw={"resultType": item.get("resultType")},
                )
            )
        return out

    # -- write --------------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        if self._config.yt_write_mode == "ytmusicapi":
            raise ProviderError(
                "YT_WRITE_MODE=ytmusicapi requires authenticated ytmusicapi headers, "
                "which are not wired up yet (phase 2)."
            )
        response = self._execute(
            self.youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": name, "description": description},
                    "status": {"privacyStatus": "private"},
                },
            ),
            COST_INSERT,
        )
        return response["id"]

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """One request per track — the Data API has no batch insert.

        At 50 units each this is the quota bottleneck; QuotaExceeded propagates
        so the job engine can checkpoint and resume tomorrow.
        """
        for video_id in track_ids:
            self._execute(
                self.youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": video_id,
                            },
                        }
                    },
                ),
                COST_INSERT,
            )

    # -- removal ------------------------------------------------------------

    supports_removal = True

    def list_entries(self, playlist_id: str) -> list[PlaylistEntry]:
        """Raw occurrences, each with its own playlistItem id.

        Deletion needs the item id rather than the video id, and a video
        appearing three times has three distinct item ids — which is exactly
        what makes de-duplication possible.
        """
        entries: list[PlaylistEntry] = []
        page_token = None
        position = 0
        while True:
            response = self._execute(
                self.youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ),
                COST_LIST,
            )
            for item in response.get("items", []):
                video_id = item["contentDetails"].get("videoId")
                if video_id:
                    entries.append(
                        PlaylistEntry(
                            entry_id=item["id"],
                            track_id=video_id,
                            position=position,
                            label=item["snippet"].get("title") or "",
                        )
                    )
                position += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                return entries

    def remove_entries(self, playlist_id: str, entries: list[PlaylistEntry]) -> None:
        """Delete occurrences one at a time — 50 quota units each.

        Item ids are stable, so deleting one does not invalidate the others and
        order does not matter.
        """
        for entry in entries:
            self._execute(
                self.youtube.playlistItems().delete(id=entry.entry_id),
                COST_INSERT,
            )

    def remaining_writes(self) -> int:
        return max(0, (DAILY_QUOTA - self.quota_used) // COST_INSERT)
