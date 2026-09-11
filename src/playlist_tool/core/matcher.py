"""Fuzzy track matching.

Pure functions over canonical models — no network, no provider knowledge, so the
whole thing is testable against fixtures. The weights below are a *starting
point*; phase 1 exists to tune them against real playlists.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import Bucket, Candidate, CanonicalTrack, MatchResult
from .normalize import (
    SEVERE_VARIANTS,
    canonical_artists,
    canonical_isrc,
    canonical_title,
    prepare_title_pair,
    strip_artist_prefix,
    variant_tags,
)


@dataclass(frozen=True)
class MatchConfig:
    title_weight: float = 0.42
    artist_weight: float = 0.33
    duration_weight: float = 0.20
    album_weight: float = 0.05

    auto_threshold: float = 0.85
    review_threshold: float = 0.55

    variant_penalty: float = 0.12
    variant_penalty_cap: float = 0.35
    severe_variant_penalty: float = 0.30
    duration_outlier_penalty: float = 0.25

    #: Beyond this gap the recordings are almost certainly different.
    duration_outlier_seconds: float = 25.0


DEFAULT_CONFIG = MatchConfig()


def similarity(a: str, b: str) -> float:
    """Blend sequence similarity with token overlap.

    Sequence ratio alone punishes reordering ("Beyoncé, Jay-Z" vs "Jay-Z,
    Beyoncé"); Jaccard alone ignores word order entirely. Together they behave
    sensibly on both.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    union = tokens_a | tokens_b
    jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
    return 0.6 * seq + 0.4 * jaccard


def artist_similarity(source: set[str], target: set[str]) -> float:
    """Set containment, not equality.

    Platforms disagree about how many featured artists to credit, so a subset
    match should score high. The smaller set is the denominator.
    """
    if not source or not target:
        return 0.0

    # Score every artist on the *smaller* side against its best counterpart, so
    # that one platform crediting extra featured artists costs nothing. Each
    # pairing is graded rather than being exact-or-nothing, which is what
    # rescues real catalog differences: "Ricardo Gi"/"Ricardo",
    # "Diddy"/"P. Diddy", "Damian Marley"/"Damian \"Jr. Gong\" Marley".
    small, large = (source, target) if len(source) <= len(target) else (target, source)
    total = 0.0
    for name in small:
        if name in large:
            total += 1.0
            continue
        best = max((similarity(name, other) for other in large), default=0.0)
        # Weak resemblance is more likely coincidence than a spelling variant.
        total += best if best > 0.6 else best * 0.5
    return total / len(small)


def duration_score(source_ms: int | None, target_ms: int | None) -> float:
    """Duration is the strongest single signal available.

    Returns a neutral 0.5 when either side is unknown so that missing data
    neither rewards nor punishes a candidate.
    """
    if not source_ms or not target_ms:
        return 0.5
    delta = abs(source_ms - target_ms) / 1000.0
    if delta <= 2:
        return 1.0
    if delta <= 5:
        return 0.9
    if delta <= 10:
        return 0.6
    if delta <= 20:
        return 0.25
    return 0.0


def score_candidate(
    source: CanonicalTrack,
    candidate: Candidate,
    config: MatchConfig = DEFAULT_CONFIG,
) -> tuple[float, dict[str, float], dict[str, float]]:
    """Score one candidate. Returns (score, components, penalties)."""
    # A YouTube video title repeats its artist ("saybik - PAINKILLER") because a
    # video has no artist column. Both sides' artist names are used to strip
    # such a prefix from either title: the name is known to the comparison
    # regardless of which platform supplied it, which is what rescues
    # "DMX - Ruff Ryders Anthem" when the uploader channel is the wrong artist.
    known_artists = source.artists + candidate.artists
    source_title = strip_artist_prefix(source.title, known_artists)
    candidate_title = strip_artist_prefix(candidate.title, known_artists)

    # Titles are normalized against each other, not in isolation, so that
    # one-sided qualifiers ("(Spider-Man: Into the Spider-Verse)") drop out
    # while variant markers ("(Live)") always survive. See prepare_title_pair.
    src_title, src_featured, cand_title, cand_featured = prepare_title_pair(
        source_title, candidate_title
    )

    src_artists = canonical_artists(source.artists + src_featured)
    cand_artists = canonical_artists(candidate.artists + cand_featured)

    if source.album and candidate.album:
        album_score = similarity(
            canonical_title(source.album)[0], canonical_title(candidate.album)[0]
        )
    else:
        album_score = 0.0

    components = {
        "title": similarity(src_title, cand_title),
        "artist": artist_similarity(src_artists, cand_artists),
        "duration": duration_score(source.duration_ms, candidate.duration_ms),
        "album": album_score,
    }

    score = (
        components["title"] * config.title_weight
        + components["artist"] * config.artist_weight
        + components["duration"] * config.duration_weight
        + components["album"] * config.album_weight
    )

    penalties: dict[str, float] = {}

    # Variant tags are read from the ORIGINAL titles/albums, pre-normalization.
    src_tags = variant_tags(source.title, source.album)
    cand_tags = variant_tags(candidate.title, candidate.album)
    mismatched = src_tags.symmetric_difference(cand_tags)

    severe = mismatched & SEVERE_VARIANTS
    if severe:
        penalties["variant:" + ",".join(sorted(severe))] = config.severe_variant_penalty
        score -= config.severe_variant_penalty

    ordinary = mismatched - SEVERE_VARIANTS
    if ordinary:
        amount = min(
            len(ordinary) * config.variant_penalty, config.variant_penalty_cap
        )
        penalties["variant:" + ",".join(sorted(ordinary))] = amount
        score -= amount

    if source.duration_ms and candidate.duration_ms:
        delta = abs(source.duration_ms - candidate.duration_ms) / 1000.0
        if delta > config.duration_outlier_seconds:
            penalties["duration_outlier"] = config.duration_outlier_penalty
            score -= config.duration_outlier_penalty

    return max(0.0, min(1.0, score)), components, penalties


def bucket_for(score: float, config: MatchConfig = DEFAULT_CONFIG) -> Bucket:
    if score >= config.auto_threshold:
        return Bucket.AUTO
    if score >= config.review_threshold:
        return Bucket.REVIEW
    return Bucket.UNMATCHED


def match(
    source: CanonicalTrack,
    candidates: list[Candidate],
    config: MatchConfig = DEFAULT_CONFIG,
    keep_runners_up: int = 3,
) -> MatchResult:
    """Score every candidate and pick a winner.

    Runners-up are retained because the review UI needs alternatives to offer,
    and because they are the raw material for tuning the weights.
    """
    if not candidates:
        return MatchResult(
            source=source, best=None, score=0.0, bucket=Bucket.UNMATCHED
        )

    # An ISRC identifies a recording, so agreement is proof of identity, not
    # evidence toward it. Fuzzy scoring cannot improve on that and can only
    # second-guess it — a correct match whose title and artist are written
    # differently on each platform would otherwise land in the review queue.
    source_isrc = canonical_isrc(source.isrc)
    if source_isrc:
        for candidate in candidates:
            if canonical_isrc(candidate.isrc) == source_isrc:
                return MatchResult(
                    source=source,
                    best=candidate,
                    score=1.0,
                    bucket=Bucket.AUTO,
                    components={"isrc": 1.0},
                    runners_up=[(c, 0.0) for c in candidates if c is not candidate][
                        :keep_runners_up
                    ],
                )

    scored = []
    for candidate in candidates:
        value, components, penalties = score_candidate(source, candidate, config)
        scored.append((candidate, value, components, penalties))

    scored.sort(key=lambda row: row[1], reverse=True)
    best, score, components, penalties = scored[0]

    return MatchResult(
        source=source,
        best=best,
        score=score,
        bucket=bucket_for(score, config),
        components=components,
        penalties=penalties,
        runners_up=[(c, s) for c, s, _, _ in scored[1 : 1 + keep_runners_up]],
    )
