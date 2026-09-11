"""Phase 1 dry run: fetch, match, report. Never writes to any platform.

This is the go/no-go instrument for the whole project. If match rates on real
playlists are poor, no amount of UI or job-engine work will save it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..providers.base import MusicProvider, ProviderError
from .matcher import DEFAULT_CONFIG, MatchConfig, match
from .models import Bucket, CanonicalTrack, MatchResult

#: Sentinel playlist id meaning "the user's Liked Songs / saved library".
SAVED_TRACKS = "__saved__"


@dataclass
class DryRunReport:
    source_provider: str
    target_provider: str
    playlist_name: str
    playlist_id: str
    results: list[MatchResult] = field(default_factory=list)
    errors: list[tuple[CanonicalTrack, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    def bucket(self, bucket: Bucket) -> list[MatchResult]:
        return [r for r in self.results if r.bucket is bucket]

    @property
    def auto_rate(self) -> float:
        return len(self.bucket(Bucket.AUTO)) / self.total if self.total else 0.0

    @property
    def coverage(self) -> float:
        """Auto + reviewable — the ceiling if the user reviews every ambiguity."""
        if not self.total:
            return 0.0
        reachable = len(self.bucket(Bucket.AUTO)) + len(self.bucket(Bucket.REVIEW))
        return reachable / self.total

    def to_dict(self) -> dict:
        return {
            "source": self.source_provider,
            "target": self.target_provider,
            "playlist": {"id": self.playlist_id, "name": self.playlist_name},
            "summary": {
                "total": self.total,
                "auto": len(self.bucket(Bucket.AUTO)),
                "needs_review": len(self.bucket(Bucket.REVIEW)),
                "unmatched": len(self.bucket(Bucket.UNMATCHED)),
                # Split out the tracks the target platform simply does not have,
                # so a low match rate is not mistaken for a matcher failure.
                "absent": sum(1 for r in self.results if r.reason == "absent"),
                "low_confidence": sum(
                    1 for r in self.results if r.reason == "low_confidence"
                ),
                "errors": len(self.errors),
                "auto_rate": round(self.auto_rate, 4),
                "coverage": round(self.coverage, 4),
            },
            "tracks": [
                {
                    "source": result.source.display(),
                    "source_id": result.source.source_id,
                    "duration_ms": result.source.duration_ms,
                    "bucket": result.bucket.value,
                    "reason": result.reason,
                    "score": round(result.score, 4),
                    "match": result.best.display() if result.best else None,
                    "match_id": result.best.id if result.best else None,
                    "match_duration_ms": (
                        result.best.duration_ms if result.best else None
                    ),
                    "components": {
                        k: round(v, 4) for k, v in result.components.items()
                    },
                    "penalties": {k: round(v, 4) for k, v in result.penalties.items()},
                    "alternatives": [
                        {"id": c.id, "label": c.display(), "score": round(s, 4)}
                        for c, s in result.runners_up
                    ],
                }
                for result in self.results
            ],
            "errors": [
                {"source": track.display(), "error": message}
                for track, message in self.errors
            ],
        }


def dry_run(
    source: MusicProvider,
    target: MusicProvider,
    playlist_id: str,
    playlist_name: str,
    limit: int | None = None,
    workers: int = 6,
    config: MatchConfig = DEFAULT_CONFIG,
    on_progress: Callable[[int, int], None] | None = None,
) -> DryRunReport:
    """Match every track in a source playlist against the target platform.

    Search is I/O bound and independent per track, so it is fanned out. Workers
    are kept modest — both platforms rate-limit, and hammering them turns a slow
    run into a failed one.
    """
    if playlist_id == SAVED_TRACKS:
        tracks = source.get_saved_tracks()
    else:
        tracks = source.get_tracks(playlist_id)
    if limit:
        tracks = tracks[:limit]

    report = DryRunReport(
        source_provider=source.name,
        target_provider=target.name,
        playlist_name=playlist_name,
        playlist_id=playlist_id,
    )

    def _match_one(track: CanonicalTrack) -> tuple[CanonicalTrack, MatchResult | str]:
        try:
            candidates = target.search(track)
        except ProviderError as exc:
            return track, str(exc)
        return track, match(track, candidates, config)

    total = len(tracks)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for track, outcome in pool.map(_match_one, tracks):
            if isinstance(outcome, str):
                report.errors.append((track, outcome))
            else:
                report.results.append(outcome)
            done += 1
            if on_progress:
                on_progress(done, total)

    # Restore playlist order; ThreadPoolExecutor.map preserves it, but explicit
    # sorting keeps the report stable if the executor is ever swapped out.
    report.results.sort(key=lambda r: r.source.position or 0)
    return report
