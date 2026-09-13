"""Transfer job engine: fetch -> match -> write, resumable at every step.

Design rules, all of which exist because this talks to rate-limited third-party
APIs over a slow network and *will* be interrupted:

* Nothing is held only in memory. Every track's outcome is committed as it is
  decided, so `Ctrl-C`, a dropped connection or an exhausted quota costs at most
  one in-flight item.
* Writes are idempotent. An item at WRITTEN is never sent again, so re-running a
  job appends only what is genuinely missing.
* Quota exhaustion is a *pause*, not a failure. The job parks at PAUSED_QUOTA
  with its remaining items intact and continues tomorrow.
* Sync is append-only. Tracks added to the target playlist by hand are never
  removed, because treating the source as absolute truth would silently delete
  the user's own edits.
"""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    ItemStatus,
    JobStatus,
    MatchCache,
    PlaylistLink,
    TransferItem,
    TransferJob,
    utcnow,
)
from ..db.session import get_session
from ..providers.base import MusicProvider, ProviderError, QuotaExceeded
from .matcher import DEFAULT_CONFIG, MatchConfig, match
from .models import SAVED_TRACKS, Bucket, CanonicalTrack

Progress = Callable[[str, int, int], None]

__all__ = [
    "apply_cached_decisions",
    "cache_lookup",
    "cache_store",
    "create_job",
    "drain_jobs",
    "fetch_stage",
    "finalize_job",
    "match_stage",
    "pending_write_queue",
    "write_stage",
]


# --------------------------------------------------------------------------
# match cache
# --------------------------------------------------------------------------


def cache_lookup(
    session: Session, source_provider: str, source_id: str, target_provider: str
) -> MatchCache | None:
    """Find a known mapping, in either direction.

    A confirmed Spotify->YouTube pair is equally valid read backwards, and
    reusing it keeps a round trip consistent with itself: transferring a
    playlist back should land on the track it came from.
    """
    direct = session.scalar(
        select(MatchCache).where(
            MatchCache.source_provider == source_provider,
            MatchCache.source_id == source_id,
            MatchCache.target_provider == target_provider,
        )
    )
    if direct:
        return direct

    # Negative decisions are not symmetric: "this Spotify track has no YouTube
    # counterpart" says nothing about any YouTube track, so they are excluded
    # from the inverse lookup.
    inverse = session.scalar(
        select(MatchCache).where(
            MatchCache.source_provider == target_provider,
            MatchCache.target_provider == source_provider,
            MatchCache.target_id == source_id,
            MatchCache.no_match.is_(False),
        )
    )
    if inverse:
        return MatchCache(
            source_provider=source_provider,
            source_id=source_id,
            target_provider=target_provider,
            target_id=inverse.source_id,
            score=inverse.score,
            user_verified=inverse.user_verified,
            source_label=inverse.target_label,
            target_label=inverse.source_label,
        )
    return None


def cache_store(
    session: Session,
    source_provider: str,
    source_id: str,
    source_label: str,
    target_provider: str,
    target_id: str,
    target_label: str,
    score: float,
    user_verified: bool = False,
    no_match: bool = False,
) -> None:
    """Record a mapping. A human decision is never overwritten by a machine one."""
    existing = session.scalar(
        select(MatchCache).where(
            MatchCache.source_provider == source_provider,
            MatchCache.source_id == source_id,
            MatchCache.target_provider == target_provider,
        )
    )
    if existing:
        if existing.user_verified and not user_verified:
            return
        existing.target_id = target_id
        existing.target_label = target_label
        existing.score = score
        existing.no_match = no_match
        existing.user_verified = existing.user_verified or user_verified
        return

    session.add(
        MatchCache(
            source_provider=source_provider,
            source_id=source_id,
            source_label=source_label,
            target_provider=target_provider,
            target_id=target_id,
            target_label=target_label,
            score=score,
            user_verified=user_verified,
            no_match=no_match,
        )
    )


def apply_cached_decisions(session: Session, job_id: int) -> int:
    """Resolve review items the user has already answered, in any job.

    Review state used to live on the item, so a superseded job kept its own copy
    of every unresolved track and asked about it again — six questions survived
    their own answers that way. Decisions belong to the *track*, so they are read
    back from `match_cache` and applied before anyone is prompted.

    Returns the number of items resolved without asking.
    """
    job = session.get(TransferJob, job_id)
    if job is None:
        return 0

    resolved = 0
    for item in session.scalars(
        select(TransferItem).where(
            TransferItem.job_id == job_id,
            TransferItem.status == ItemStatus.NEEDS_REVIEW.value,
        )
    ):
        hit = cache_lookup(
            session, job.source_provider, item.source_track_id, job.target_provider
        )
        if hit is None or not hit.user_verified:
            continue
        if hit.no_match:
            item.status = ItemStatus.SKIPPED.value
            item.reason = "user_no_match"
        else:
            item.target_track_id = hit.target_id
            item.target_label = hit.target_label
            # If that video already reached this playlist — typically via the
            # job that superseded this one — the work is done. Marking it
            # MATCHED instead would leave the job permanently "ready" with
            # phantom pending writes that dedupe silently discards.
            already_written = job.target_playlist_id is not None and session.scalar(
                select(TransferItem.id)
                .join(TransferJob, TransferItem.job_id == TransferJob.id)
                .where(
                    TransferJob.target_playlist_id == job.target_playlist_id,
                    TransferItem.target_track_id == hit.target_id,
                    TransferItem.status == ItemStatus.WRITTEN.value,
                )
            )
            if already_written:
                item.status = ItemStatus.WRITTEN.value
                item.reason = "already_present"
            else:
                item.status = ItemStatus.MATCHED.value
                item.reason = "user"
        resolved += 1
    return resolved


# --------------------------------------------------------------------------
# job lifecycle
# --------------------------------------------------------------------------


def create_job(
    source: MusicProvider,
    target: MusicProvider,
    source_playlist_id: str,
    source_playlist_name: str,
    target_playlist_name: str | None = None,
) -> int:
    """Create (or reuse) a job for a playlist and return its id.

    An unfinished job for the same source playlist is resumed rather than
    duplicated — running the same command twice must not create two playlists.
    """
    with get_session() as session:
        existing = session.scalar(
            select(TransferJob)
            .where(
                TransferJob.source_provider == source.name,
                TransferJob.target_provider == target.name,
                TransferJob.source_playlist_id == source_playlist_id,
                TransferJob.status.notin_(
                    [JobStatus.COMPLETED.value, JobStatus.FAILED.value]
                ),
            )
            .order_by(TransferJob.id.desc())
        )
        if existing:
            return existing.id

        link = session.scalar(
            select(PlaylistLink).where(
                PlaylistLink.source_provider == source.name,
                PlaylistLink.source_playlist_id == source_playlist_id,
                PlaylistLink.target_provider == target.name,
            )
        )

        job = TransferJob(
            source_provider=source.name,
            target_provider=target.name,
            source_playlist_id=source_playlist_id,
            source_playlist_name=source_playlist_name,
            target_playlist_name=target_playlist_name or source_playlist_name,
            # Reuse the previously created playlist so a re-run syncs into it
            # instead of making a duplicate.
            target_playlist_id=link.target_playlist_id if link else None,
        )
        session.add(job)
        session.flush()
        return job.id


def fetch_stage(source: MusicProvider, job_id: int, limit: int | None = None) -> int:
    """Load source tracks into transfer_items. Safe to re-run."""
    with get_session() as session:
        job = session.get(TransferJob, job_id)
        assert job is not None

        if job.source_playlist_id == SAVED_TRACKS:
            tracks = source.get_saved_tracks()
        else:
            tracks = source.get_tracks(job.source_playlist_id)
        if limit:
            tracks = tracks[:limit]

        # Identity is "how many times does this track appear", not "where".
        # Keying on (track, position) looks reasonable until the source playlist
        # changes: removing or reordering a single track shifts every position
        # after it, and every shifted track then reads as new. One skipped track
        # re-added 55 rows to a real job that way. Counting occurrences handles
        # genuine duplicates — a playlist may legitimately hold a track twice —
        # while being immune to reordering.
        existing = Counter(
            item.source_track_id
            for item in session.scalars(
                select(TransferItem).where(TransferItem.job_id == job.id)
            )
        )
        seen: Counter[str] = Counter()

        # Idempotency cannot key off this job: a completed job is never reused,
        # so a re-run would otherwise start empty and write every track a second
        # time. What matters is what already reached the *target playlist*, from
        # any previous job. Those tracks are recorded as WRITTEN up front, which
        # is also what makes sync append-only.
        already: dict[str, str] = {}
        if job.target_playlist_id:
            prior = session.execute(
                select(TransferItem.source_track_id, TransferItem.target_track_id)
                .join(TransferJob, TransferItem.job_id == TransferJob.id)
                .where(
                    TransferJob.target_playlist_id == job.target_playlist_id,
                    TransferJob.source_provider == job.source_provider,
                    TransferItem.status == ItemStatus.WRITTEN.value,
                )
            )
            already = {row[0]: row[1] for row in prior if row[0]}

        added = 0
        for position, track in enumerate(tracks):
            if not track.source_id:
                continue
            seen[track.source_id] += 1
            if seen[track.source_id] <= existing[track.source_id]:
                continue  # this occurrence already has a row
            if track.source_id in already:
                session.add(
                    TransferItem(
                        job_id=job.id,
                        position=position,
                        source_track_id=track.source_id,
                        source_label=track.display(),
                        source_title=track.title,
                        source_artists=json.dumps(track.artists),
                        source_album=track.album,
                        source_isrc=track.isrc,
                        source_duration_ms=track.duration_ms,
                        target_track_id=already[track.source_id],
                        status=ItemStatus.WRITTEN.value,
                        reason="already_present",
                    )
                )
                continue
            session.add(
                TransferItem(
                    job_id=job.id,
                    position=position,
                    source_track_id=track.source_id,
                    source_label=track.display(),
                    source_title=track.title,
                    source_artists=json.dumps(track.artists),
                    source_album=track.album,
                    source_isrc=track.isrc,
                    source_duration_ms=track.duration_ms,
                )
            )
            added += 1

        job.status = JobStatus.MATCHING.value
        return added


def match_stage(
    source: MusicProvider,
    target: MusicProvider,
    job_id: int,
    workers: int = 6,
    config: MatchConfig = DEFAULT_CONFIG,
    on_progress: Progress | None = None,
) -> dict[str, int]:
    """Resolve every pending item to a target track.

    The cache is consulted first: repeated tracks across playlists cost nothing,
    and human corrections from earlier runs are applied automatically.
    """
    with get_session() as session:
        job = session.get(TransferJob, job_id)
        assert job is not None
        # FAILED is retryable, not terminal: search failures are usually
        # transient throttling, and a track must not be abandoned because of one
        # bad response. Re-running the command picks them back up.
        pending = list(
            session.scalars(
                select(TransferItem).where(
                    TransferItem.job_id == job.id,
                    TransferItem.status.in_(
                        [ItemStatus.PENDING.value, ItemStatus.FAILED.value]
                    ),
                )
            )
        )
        for item in pending:
            item.error = None

        to_search: list[TransferItem] = []
        for item in pending:
            # Nothing to search with. Left to the providers this becomes an
            # HTTP 400 that looks transient, so the item is retried forever and
            # the job can never complete. It is a permanent property of the
            # track, so record it as such.
            if not item.source_title.strip() and not json.loads(
                item.source_artists or "[]"
            ):
                item.status = ItemStatus.SKIPPED.value
                item.reason = "missing_metadata"
                item.error = None
                continue
            hit = cache_lookup(session, source.name, item.source_track_id, target.name)
            if hit and hit.no_match and hit.user_verified:
                # Already answered "no counterpart exists" — do not search for
                # it again, and never ask a second time.
                item.status = ItemStatus.SKIPPED.value
                item.reason = "user_no_match"
            elif hit and hit.target_id:
                item.target_track_id = hit.target_id
                item.target_label = hit.target_label
                item.score = hit.score
                item.status = ItemStatus.MATCHED.value
                item.reason = "cache"
            else:
                to_search.append(item)

        total = len(to_search)
        if on_progress:
            on_progress("match", len(pending) - total, len(pending))

        # Snapshot what the search threads need; ORM objects are not thread-safe.
        payload = [
            (
                item.id,
                CanonicalTrack(
                    title=item.source_title,
                    artists=json.loads(item.source_artists or "[]"),
                    duration_ms=item.source_duration_ms,
                    album=item.source_album,
                    isrc=item.source_isrc,
                    source_id=item.source_track_id,
                    source_provider=source.name,
                ),
            )
            for item in to_search
        ]

        def _search(entry):
            item_id, track = entry
            try:
                # Exact identity first where the platform supports it: one
                # lookup replaces a search plus fuzzy scoring, and cannot be
                # wrong about which recording it found.
                if target.supports_isrc_lookup and track.isrc:
                    exact = target.lookup_by_isrc(track.isrc)
                    if exact is not None:
                        return item_id, track, [exact], None
                return item_id, track, target.search(track), None
            except ProviderError as exc:
                return item_id, track, [], str(exc)

        done = 0
        by_id = {item.id: item for item in to_search}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item_id, track, candidates, error in pool.map(_search, payload):
                item = by_id[item_id]
                if error:
                    item.status = ItemStatus.FAILED.value
                    item.error = error
                else:
                    result = match(track, candidates, config)
                    item.score = result.score
                    item.candidates = json.dumps(
                        [
                            {"id": c.id, "label": c.display(), "score": round(s, 4)}
                            for c, s in ([(result.best, result.score)] if result.best else [])
                            + result.runners_up
                        ]
                    )
                    if result.bucket is Bucket.AUTO and result.best:
                        item.target_track_id = result.best.id
                        item.target_label = result.best.display()
                        item.status = ItemStatus.MATCHED.value
                        cache_store(
                            session,
                            source.name,
                            item.source_track_id,
                            item.source_label,
                            target.name,
                            result.best.id,
                            result.best.display(),
                            result.score,
                        )
                    elif result.reason == "absent":
                        item.status = ItemStatus.ABSENT.value
                        item.reason = "absent"
                    else:
                        item.status = ItemStatus.NEEDS_REVIEW.value
                        item.reason = "low_confidence"
                        if result.best:
                            item.target_label = result.best.display()
                done += 1
                if on_progress:
                    on_progress("match", len(pending) - total + done, len(pending))

        job.status = JobStatus.READY.value
        return job.counts()


def pending_write_queue(target_provider: str) -> list[tuple[int, int, str]]:
    """Jobs with tracks ready to write, cheapest first.

    Cheapest first so that a run cut short by the quota leaves finished
    playlists behind rather than several part-done ones.

    Returns (track_count, job_id, playlist_name).
    """
    with get_session() as session:
        rows = []
        for job in session.scalars(select(TransferJob)):
            if job.target_provider != target_provider:
                continue
            count = sum(
                1
                for item in job.items
                if item.status == ItemStatus.MATCHED.value and item.target_track_id
            )
            if count:
                rows.append((count, job.id, job.source_playlist_name))
    return sorted(rows)


def drain_jobs(
    target: MusicProvider,
    on_job: Callable[[str, int, dict], None] | None = None,
) -> dict:
    """Write every queued job until the daily quota is exhausted.

    A scheduler invoking one playlist per day would leave most of the budget
    unused — the smallest remaining job costs 2,550 of 10,000 units — so this
    works through the queue and stops only when the platform refuses.

    Stopping is a *pause*, not a failure: the interrupted job keeps its
    remaining items and later jobs are left untouched, so the next run resumes
    exactly where this one stopped.
    """
    queue = pending_write_queue(target.name)
    written = 0
    completed: list[str] = []

    for count, job_id, name in queue:
        result = write_stage(target, job_id)
        written += result["written"]
        if on_job:
            on_job(name, job_id, result)

        if result.get("paused"):
            return {
                "written": written,
                "completed": completed,
                "paused_on": name,
                "remaining": result["remaining"],
                "jobs_untouched": len(queue) - len(completed) - 1,
            }

        finalize_job(job_id)
        completed.append(name)

    return {
        "written": written,
        "completed": completed,
        "paused_on": None,
        "remaining": 0,
        "jobs_untouched": 0,
    }


def finalize_job(job_id: int) -> str:
    """Mark a job completed when nothing is outstanding.

    The write path already does this, but it is skipped when there is nothing
    to write — which is exactly the state a fully-transferred playlist ends in.
    Without this a finished job sits at `ready` forever and `jobs` misreports
    it as having work left.
    """
    with get_session() as session:
        job = session.get(TransferJob, job_id)
        if job is None:
            return ""
        outstanding = session.scalar(
            select(TransferItem).where(
                TransferItem.job_id == job_id,
                TransferItem.status.in_(
                    [
                        ItemStatus.PENDING.value,
                        ItemStatus.MATCHED.value,
                        ItemStatus.NEEDS_REVIEW.value,
                        ItemStatus.FAILED.value,
                    ]
                ),
            )
        )
        if outstanding is None and job.status != JobStatus.PAUSED_QUOTA.value:
            job.status = JobStatus.COMPLETED.value
        return job.status


def write_stage(
    target: MusicProvider,
    job_id: int,
    on_progress: Progress | None = None,
) -> dict[str, int]:
    """Create the target playlist if needed and append matched tracks.

    Each batch is committed as it lands, so an interruption or a quota wall
    never loses or double-writes work.
    """
    with get_session() as session:
        job = session.get(TransferJob, job_id)
        assert job is not None
        job.status = JobStatus.WRITING.value

        if not job.target_playlist_id:
            job.target_playlist_id = target.create_playlist(
                job.target_playlist_name,
                f"Transferred from {job.source_provider} by playlist-tool",
            )
            session.flush()

        link = session.scalar(
            select(PlaylistLink).where(
                PlaylistLink.source_provider == job.source_provider,
                PlaylistLink.source_playlist_id == job.source_playlist_id,
                PlaylistLink.target_provider == job.target_provider,
            )
        )
        if link is None:
            session.add(
                PlaylistLink(
                    source_provider=job.source_provider,
                    source_playlist_id=job.source_playlist_id,
                    target_provider=job.target_provider,
                    target_playlist_id=job.target_playlist_id,
                    name=job.target_playlist_name,
                )
            )
        else:
            link.target_playlist_id = job.target_playlist_id
            link.last_synced_at = utcnow()

    # Commit the playlist id before writing any track, so an interruption here
    # still resumes into the same playlist rather than creating a second one.
    with get_session() as session:
        job = session.get(TransferJob, job_id)
        assert job is not None
        # A previously FAILED write is retryable as long as it still knows which
        # track it meant to add. Write failures are usually transient (YouTube
        # returns 409 under concurrency), and abandoning the track would leave a
        # silent hole in the playlist.
        queued = list(
            session.scalars(
                select(TransferItem)
                .where(
                    TransferItem.job_id == job.id,
                    TransferItem.status.in_(
                        [ItemStatus.MATCHED.value, ItemStatus.FAILED.value]
                    ),
                    TransferItem.target_track_id.is_not(None),
                )
                .order_by(TransferItem.position)
            )
        )
        for item in queued:
            item.error = None

        # Several distinct source tracks can legitimately resolve to the same
        # target video — a single and an album cut on Spotify are one upload on
        # YouTube. Writing each would put the same video in the playlist two or
        # three times, so the first one wins and the rest are recorded as
        # skipped duplicates rather than silently dropped.
        seen_targets: set[str] = set(
            row[0]
            for row in session.execute(
                select(TransferItem.target_track_id)
                .join(TransferJob, TransferItem.job_id == TransferJob.id)
                .where(
                    TransferJob.target_playlist_id == job.target_playlist_id,
                    TransferItem.status == ItemStatus.WRITTEN.value,
                    TransferItem.target_track_id.is_not(None),
                )
            )
        )

        deduped: list[TransferItem] = []
        for item in queued:
            if item.target_track_id in seen_targets:
                item.status = ItemStatus.SKIPPED.value
                item.reason = "duplicate_target"
                continue
            seen_targets.add(item.target_track_id)
            deduped.append(item)
        queued = deduped
        session.commit()

        total = len(queued)
        written = 0
        batch_size = max(1, target.write_batch_size)

        for start in range(0, total, batch_size):
            batch = queued[start : start + batch_size]
            ids = [item.target_track_id for item in batch if item.target_track_id]
            try:
                target.add_tracks(job.target_playlist_id, ids)
            except QuotaExceeded as exc:
                job.status = JobStatus.PAUSED_QUOTA.value
                job.error = str(exc)
                session.commit()
                return {"written": written, "remaining": total - written, "paused": 1}
            except ProviderError as exc:
                for item in batch:
                    item.status = ItemStatus.FAILED.value
                    item.error = str(exc)
                session.commit()
                continue

            for item in batch:
                item.status = ItemStatus.WRITTEN.value
            written += len(batch)
            # Checkpoint after every batch, not at the end.
            session.commit()
            if on_progress:
                on_progress("write", written, total)

        # FAILED counts as outstanding work. Marking the job COMPLETED while a
        # track failed to write would retire it silently, and the playlist would
        # stay one track short forever.
        remaining = session.scalar(
            select(TransferItem).where(
                TransferItem.job_id == job.id,
                TransferItem.status.in_(
                    [
                        ItemStatus.PENDING.value,
                        ItemStatus.MATCHED.value,
                        ItemStatus.FAILED.value,
                    ]
                ),
            )
        )
        job.status = (
            JobStatus.COMPLETED.value if remaining is None else JobStatus.READY.value
        )
        job.error = None

        link = session.scalar(
            select(PlaylistLink).where(
                PlaylistLink.source_provider == job.source_provider,
                PlaylistLink.source_playlist_id == job.source_playlist_id,
                PlaylistLink.target_provider == job.target_provider,
            )
        )
        if link is not None:
            link.last_synced_at = utcnow()

        return {"written": written, "remaining": total - written, "paused": 0}
