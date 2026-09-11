"""Provider-independent domain model.

Nothing here knows that Spotify or YouTube exist. Providers translate their own
payloads into these types on the way in, and consume target IDs on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


#: Below this, the best candidate bears no real resemblance to the source and
#: the track is reported as absent from the target platform rather than as
#: something a human could usefully review. Search engines rarely return
#: *nothing* — Spotify answers almost any query with unrelated filler — so
#: "no candidates" alone is far too strict a test for absence.
ABSENT_SCORE = 0.30


class Bucket(str, Enum):
    """What should happen to a match without further information."""

    AUTO = "auto"
    REVIEW = "needs_review"
    UNMATCHED = "unmatched"


class SkipReason(str, Enum):
    """Why a source item never reached the matcher at all."""

    LOCAL_FILE = "local_file"
    EPISODE = "episode"
    UNAVAILABLE = "unavailable"
    MISSING_METADATA = "missing_metadata"


@dataclass(frozen=True)
class PlaylistRef:
    id: str
    name: str
    #: None when the platform does not report a count on its list endpoint.
    #: Spotify stopped returning the `tracks` object from current_user_playlists,
    #: so reporting 0 there would be a lie — the real count arrives with the
    #: tracks themselves.
    track_count: int | None
    owner: str | None = None
    description: str | None = None
    is_owned: bool = True


@dataclass
class CanonicalTrack:
    """A track as the rest of the system understands it."""

    title: str
    artists: list[str]
    duration_ms: int | None = None
    album: str | None = None
    isrc: str | None = None
    source_id: str | None = None
    source_provider: str | None = None
    position: int | None = None

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def display(self) -> str:
        artists = ", ".join(self.artists) if self.artists else "unknown artist"
        return f"{self.title} — {artists}"


@dataclass
class Candidate:
    """A possible match returned by a target provider's search."""

    id: str
    title: str
    artists: list[str]
    duration_ms: int | None = None
    album: str | None = None
    #: Present only on platforms that expose it. YouTube Music does not.
    isrc: str | None = None
    provider: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def as_track(self) -> CanonicalTrack:
        return CanonicalTrack(
            title=self.title,
            artists=self.artists,
            duration_ms=self.duration_ms,
            album=self.album,
            isrc=self.isrc,
            source_id=self.id,
            source_provider=self.provider,
        )

    def display(self) -> str:
        artists = ", ".join(self.artists) if self.artists else "unknown artist"
        return f"{self.title} — {artists}"


@dataclass
class MatchResult:
    """Scored outcome for one source track."""

    source: CanonicalTrack
    best: Candidate | None
    score: float
    bucket: Bucket
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    runners_up: list[tuple[Candidate, float]] = field(default_factory=list)
    skipped: SkipReason | None = None

    @property
    def matched(self) -> bool:
        return self.bucket is Bucket.AUTO and self.best is not None

    @property
    def reason(self) -> str | None:
        """Why this track did not auto-match — the two cases are different.

        `absent` means the target platform returned nothing at all: the track
        very likely does not exist there (a DJ set, a drone video, a regional
        upload). No amount of review will fix it, and the honest output is a
        "not available" list.

        `low_confidence` means candidates came back but none scored well enough.
        That is the reviewable case, where a human picking from the runners-up
        usually resolves it.
        """
        if self.bucket is Bucket.AUTO:
            return None
        if self.best is None or self.score < ABSENT_SCORE:
            return "absent"
        return "low_confidence"

    def explain(self) -> str:
        """Human-readable score breakdown — used by the report and the review UI."""
        if self.skipped:
            return f"skipped: {self.skipped.value}"
        if not self.best:
            return "no candidates returned"
        parts = [f"{k}={v:.2f}" for k, v in self.components.items()]
        parts += [f"-{k}={v:.2f}" for k, v in self.penalties.items()]
        return " ".join(parts)
