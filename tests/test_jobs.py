"""Job engine: resume, idempotency, quota pausing, cache reuse.

Uses fake providers so the guarantees are tested without touching a network or a
real account — which is the point of the provider abstraction.
"""

import pytest

from playlistport.core.models import Candidate, CanonicalTrack, PlaylistRef
from playlistport.providers.base import MusicProvider, QuotaExceeded


class FakeSource(MusicProvider):
    name = "spotify"

    def __init__(self, tracks):
        self._tracks = tracks

    def list_playlists(self):
        return [PlaylistRef(id="p1", name="Test", track_count=len(self._tracks))]

    def get_tracks(self, playlist_id):
        return list(self._tracks)

    def get_saved_tracks(self):
        return list(self._tracks)

    def search(self, track, limit=8):
        return []

    def create_playlist(self, name, description=""):
        return "new"

    def add_tracks(self, playlist_id, track_ids):
        pass


class FakeTarget(MusicProvider):
    name = "youtube"
    write_batch_size = 1

    def __init__(self, quota=None):
        self.created = []
        self.written = []
        self.searches = 0
        self.quota = quota

    def list_playlists(self):
        return []

    def get_tracks(self, playlist_id):
        return []

    def get_saved_tracks(self):
        return []

    def search(self, track, limit=8):
        self.searches += 1
        return [
            Candidate(
                id=f"yt-{track.source_id}",
                title=track.title,
                artists=track.artists,
                duration_ms=track.duration_ms,
                provider=self.name,
            )
        ]

    def create_playlist(self, name, description=""):
        self.created.append(name)
        return "yt-playlist"

    def add_tracks(self, playlist_id, track_ids):
        if self.quota is not None and len(self.written) >= self.quota:
            raise QuotaExceeded("daily quota exhausted")
        self.written.extend(track_ids)


def make_tracks(n):
    return [
        CanonicalTrack(
            title=f"Song {i}",
            artists=["Artist"],
            duration_ms=200_000,
            source_id=f"sp-{i}",
            source_provider="spotify",
        )
        for i in range(n)
    ]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the engine at a throwaway SQLite file."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import playlistport.config as config_module
    import playlistport.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_Session", None)
    config_module.load_config.cache_clear() if hasattr(
        config_module.load_config, "cache_clear"
    ) else None
    yield


def run_full(source, target, limit=None):
    from playlistport.core.jobs import create_job, fetch_stage, match_stage, write_stage

    job_id = create_job(source, target, "p1", "Test")
    fetch_stage(source, job_id, limit=limit)
    match_stage(source, target, job_id, workers=2)
    return job_id, write_stage(target, job_id)


class TestHappyPath:
    def test_all_tracks_written_once(self, db):
        source, target = FakeSource(make_tracks(5)), FakeTarget()
        _, result = run_full(source, target)
        assert result["written"] == 5
        assert target.written == [f"yt-sp-{i}" for i in range(5)]
        assert target.created == ["Test"]

    def test_playlist_order_is_preserved(self, db):
        source, target = FakeSource(make_tracks(4)), FakeTarget()
        run_full(source, target)
        assert target.written == ["yt-sp-0", "yt-sp-1", "yt-sp-2", "yt-sp-3"]


class TestIdempotency:
    def test_rerun_writes_nothing_new(self, db):
        source, target = FakeSource(make_tracks(4)), FakeTarget()
        run_full(source, target)
        assert len(target.written) == 4

        # Same playlist, same tracks: a second pass must not duplicate.
        _, second = run_full(source, target)
        assert second["written"] == 0
        assert len(target.written) == 4

    def test_rerun_reuses_the_same_playlist(self, db):
        source, target = FakeSource(make_tracks(3)), FakeTarget()
        run_full(source, target)
        run_full(source, target)
        assert target.created == ["Test"], "must not create a second playlist"

    def test_sync_appends_only_new_tracks(self, db):
        tracks = make_tracks(3)
        source, target = FakeSource(tracks), FakeTarget()
        run_full(source, target)

        tracks.extend(
            [
                CanonicalTrack(
                    title="Song 99",
                    artists=["Artist"],
                    duration_ms=200_000,
                    source_id="sp-99",
                    source_provider="spotify",
                )
            ]
        )
        _, result = run_full(FakeSource(tracks), target)
        assert result["written"] == 1
        assert target.written[-1] == "yt-sp-99"


class TestQuotaPause:
    def test_pause_keeps_written_work_and_reports_remaining(self, db):
        source = FakeSource(make_tracks(10))
        target = FakeTarget(quota=4)
        _, result = run_full(source, target)

        assert result["paused"] == 1
        assert result["written"] == 4
        assert result["remaining"] == 6
        assert len(target.written) == 4

    def test_resume_after_quota_writes_only_the_rest(self, db):
        source = FakeSource(make_tracks(10))
        target = FakeTarget(quota=4)
        job_id, _ = run_full(source, target)

        # Next day: quota restored.
        target.quota = None
        from playlistport.core.jobs import write_stage

        result = write_stage(target, job_id)
        assert result["written"] == 6
        assert len(target.written) == 10
        assert len(set(target.written)) == 10, "no track written twice"


class FlakyTarget(FakeTarget):
    """Fails every search until `fail_until` searches have been attempted."""

    def __init__(self, fail_until):
        super().__init__()
        self.fail_until = fail_until

    def search(self, track, limit=8):
        self.searches += 1
        if self.searches <= self.fail_until:
            from playlistport.providers.base import ProviderError

            raise ProviderError("throttled")
        return super().search(track, limit)


class TestTransientFailures:
    def test_failed_searches_are_retried_on_a_later_run(self, db):
        from playlistport.core.jobs import create_job, fetch_stage, match_stage
        from playlistport.db.models import ItemStatus as S

        source = FakeSource(make_tracks(3))
        target = FlakyTarget(fail_until=3)  # whole first pass fails

        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)
        counts = match_stage(source, target, job_id, workers=1)
        assert counts.get(S.FAILED.value) == 3

        # A throttled search must not permanently abandon the track.
        counts = match_stage(source, target, job_id, workers=1)
        assert counts.get(S.FAILED.value, 0) == 0
        assert counts.get(S.MATCHED.value) == 3


class FlakyWriteTarget(FakeTarget):
    """Fails the Nth write once, the way YouTube's 409 did in a real run."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on
        self.attempts = 0

    def add_tracks(self, playlist_id, track_ids):
        self.attempts += 1
        if self.attempts == self.fail_on:
            from playlistport.providers.base import ProviderError

            raise ProviderError("409 The operation was aborted")
        self.written.extend(track_ids)


class TestWriteFailures:
    def test_job_is_not_completed_while_a_track_failed(self, db):
        from playlistport.db.models import JobStatus, TransferJob
        from playlistport.db.session import get_session

        source = FakeSource(make_tracks(5))
        target = FlakyWriteTarget(fail_on=3)
        job_id, result = run_full(source, target)

        assert result["written"] == 4
        with get_session() as session:
            job = session.get(TransferJob, job_id)
            # Retiring the job here would leave the playlist permanently short.
            assert job.status != JobStatus.COMPLETED.value

    def test_failed_write_is_retried_and_completes(self, db):
        from playlistport.core.jobs import write_stage
        from playlistport.db.models import JobStatus, TransferJob
        from playlistport.db.session import get_session

        source = FakeSource(make_tracks(5))
        target = FlakyWriteTarget(fail_on=3)
        job_id, _ = run_full(source, target)

        result = write_stage(target, job_id)
        assert result["written"] == 1
        assert len(target.written) == 5
        assert len(set(target.written)) == 5, "no track written twice"
        with get_session() as session:
            assert session.get(TransferJob, job_id).status == JobStatus.COMPLETED.value


class CollapsingTarget(FakeTarget):
    """Every source track resolves to the same video, as near-duplicates do."""

    def search(self, track, limit=8):
        self.searches += 1
        return [
            Candidate(
                id="yt-same",
                title=track.title,
                artists=track.artists,
                duration_ms=track.duration_ms,
                provider=self.name,
            )
        ]


class TestDuplicateTargets:
    def test_same_video_is_written_only_once(self, db):
        # A real transfer put one video in the playlist three times, because the
        # Spotify playlist held it as three separate track ids.
        source, target = FakeSource(make_tracks(4)), CollapsingTarget()
        _, result = run_full(source, target)
        assert target.written == ["yt-same"]
        assert result["written"] == 1

    def test_duplicates_are_recorded_not_lost(self, db):
        from sqlalchemy import select

        from playlistport.db.models import ItemStatus as S
        from playlistport.db.models import TransferItem
        from playlistport.db.session import get_session

        source, target = FakeSource(make_tracks(4)), CollapsingTarget()
        job_id, _ = run_full(source, target)
        with get_session() as session:
            skipped = list(
                session.scalars(
                    select(TransferItem).where(
                        TransferItem.job_id == job_id,
                        TransferItem.status == S.SKIPPED.value,
                    )
                )
            )
        assert len(skipped) == 3
        assert all(item.reason == "duplicate_target" for item in skipped)


class TestDecisionsOutliveJobs:
    """A review answer belongs to the track, not to the job that asked."""

    def _review_item(self, job_id, index=0):
        from sqlalchemy import select

        from playlistport.db.models import ItemStatus as S
        from playlistport.db.models import TransferItem
        from playlistport.db.session import get_session

        with get_session() as session:
            items = list(
                session.scalars(
                    select(TransferItem).where(
                        TransferItem.job_id == job_id,
                        TransferItem.status == S.NEEDS_REVIEW.value,
                    )
                )
            )
            return items[index] if items else None

    def _make_review_job(self, target):
        """A job whose single track lands in needs_review."""
        from playlistport.core.jobs import create_job, fetch_stage, match_stage
        from playlistport.db.models import ItemStatus as S
        from playlistport.db.session import get_session

        source = FakeSource(make_tracks(1))
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)
        match_stage(source, target, job_id, workers=1)
        with get_session() as session:
            from sqlalchemy import select

            from playlistport.db.models import TransferItem

            item = session.scalar(
                select(TransferItem).where(TransferItem.job_id == job_id)
            )
            item.status = S.NEEDS_REVIEW.value  # simulate an ambiguous match
        return job_id

    def test_positive_decision_resolves_a_later_job(self, db):
        from playlistport.core.jobs import apply_cached_decisions, cache_store
        from playlistport.db.models import ItemStatus as S
        from playlistport.db.session import get_session

        target = FakeTarget()
        job_id = self._make_review_job(target)

        with get_session() as session:
            cache_store(session, "spotify", "sp-0", "Song 0 — Artist", "youtube",
                        "yt-picked", "Picked — Artist", 0.7, user_verified=True)
            assert apply_cached_decisions(session, job_id) == 1

        item = self._review_item(job_id)
        assert item is None, "already-answered track must not be asked again"

    def test_negative_decision_is_remembered(self, db):
        # "No counterpart exists" was previously recorded nowhere, so the same
        # question came back in the next job.
        from playlistport.core.jobs import apply_cached_decisions, cache_store
        from playlistport.db.session import get_session

        target = FakeTarget()
        job_id = self._make_review_job(target)

        with get_session() as session:
            cache_store(session, "spotify", "sp-0", "Song 0 — Artist", "youtube",
                        "", "", 0.4, user_verified=True, no_match=True)
            assert apply_cached_decisions(session, job_id) == 1

        assert self._review_item(job_id) is None

    def test_negative_decision_prevents_a_new_search(self, db):
        from playlistport.core.jobs import (
            create_job,
            fetch_stage,
            cache_store,
            match_stage,
        )
        from playlistport.db.models import ItemStatus as S
        from playlistport.db.session import get_session

        target = FakeTarget()
        with get_session() as session:
            cache_store(session, "spotify", "sp-0", "Song 0 — Artist", "youtube",
                        "", "", 0.4, user_verified=True, no_match=True)

        source = FakeSource(make_tracks(1))
        job_id = create_job(source, target, "p9", "Other")
        fetch_stage(source, job_id)
        counts = match_stage(source, target, job_id, workers=1)

        assert target.searches == 0, "must not re-search a known non-match"
        assert counts.get(S.SKIPPED.value) == 1

    def test_already_written_track_is_not_queued_again(self, db):
        # A superseded job must not report phantom pending writes for tracks the
        # job that replaced it already wrote.
        from playlistport.core.jobs import apply_cached_decisions, cache_store
        from playlistport.db.models import ItemStatus as S
        from playlistport.db.models import TransferItem, TransferJob
        from playlistport.db.session import get_session
        from sqlalchemy import select

        source, target = FakeSource(make_tracks(1)), FakeTarget()
        done_job, _ = run_full(source, target)  # writes yt-sp-0

        review_job = self._make_review_job(target)
        with get_session() as session:
            # Same playlist as the completed job.
            job = session.get(TransferJob, review_job)
            job.target_playlist_id = session.get(
                TransferJob, done_job
            ).target_playlist_id
            cache_store(session, "spotify", "sp-0", "Song 0 — Artist", "youtube",
                        "yt-sp-0", "Song 0 — Artist", 0.7, user_verified=True)
            assert apply_cached_decisions(session, review_job) == 1

        with get_session() as session:
            item = session.scalar(
                select(TransferItem).where(TransferItem.job_id == review_job)
            )
            assert item.status == S.WRITTEN.value
            assert item.reason == "already_present"

    def test_unverified_cache_does_not_auto_resolve(self, db):
        # Only *human* decisions may skip review; a machine guess must not.
        from playlistport.core.jobs import apply_cached_decisions, cache_store
        from playlistport.db.session import get_session

        target = FakeTarget()
        job_id = self._make_review_job(target)
        with get_session() as session:
            cache_store(session, "spotify", "sp-0", "Song 0 — Artist", "youtube",
                        "yt-guess", "Guess", 0.6, user_verified=False)
            assert apply_cached_decisions(session, job_id) == 0
        assert self._review_item(job_id) is not None


class IsrcTarget(FakeTarget):
    """A platform that can resolve a recording exactly, as Apple/Tidal can."""

    supports_isrc_lookup = True

    def __init__(self):
        super().__init__()
        self.lookups = 0

    def lookup_by_isrc(self, isrc):
        self.lookups += 1
        return Candidate(
            id=f"yt-isrc-{isrc}", title="Exact", artists=["Exact"], isrc=isrc
        )


class TestIsrcPath:
    def test_exact_lookup_replaces_fuzzy_search(self, db):
        tracks = [
            CanonicalTrack(
                title="Song",
                artists=["Artist"],
                duration_ms=200_000,
                isrc="GBUM71029604",
                source_id="sp-0",
                source_provider="spotify",
            )
        ]
        source, target = FakeSource(tracks), IsrcTarget()
        _, result = run_full(source, target)

        assert target.lookups == 1
        assert target.searches == 0, "exact identity must not also fuzzy search"
        assert target.written == ["yt-isrc-GBUM71029604"]
        assert result["written"] == 1

    def test_falls_back_to_search_without_an_isrc(self, db):
        source, target = FakeSource(make_tracks(2)), IsrcTarget()
        run_full(source, target)
        assert target.lookups == 0
        assert target.searches == 2

    def test_target_without_support_never_looks_up(self, db):
        tracks = [
            CanonicalTrack(
                title="Song",
                artists=["Artist"],
                duration_ms=200_000,
                isrc="GBUM71029604",
                source_id="sp-0",
                source_provider="spotify",
            )
        ]
        # YouTube exposes no ISRC; it must take the fuzzy path as before.
        source, target = FakeSource(tracks), FakeTarget()
        run_full(source, target)
        assert target.searches == 1


class TestSourceChanges:
    """Re-fetching must survive the source playlist being edited.

    Keying items on (track, position) meant removing one track shifted every
    later position, and each shifted track read as new — 55 spurious rows were
    added to a real job that way.
    """

    def _items(self, job_id):
        from sqlalchemy import select

        from playlistport.db.models import TransferItem
        from playlistport.db.session import get_session

        with get_session() as session:
            return list(
                session.scalars(
                    select(TransferItem).where(TransferItem.job_id == job_id)
                )
            )

    def test_reordering_the_source_adds_nothing(self, db):
        from playlistport.core.jobs import create_job, fetch_stage

        tracks = make_tracks(5)
        source, target = FakeSource(tracks), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        assert fetch_stage(source, job_id) == 5

        reordered = list(reversed(tracks))
        assert fetch_stage(FakeSource(reordered), job_id) == 0
        assert len(self._items(job_id)) == 5

    def test_removing_a_track_from_the_source_adds_nothing(self, db):
        from playlistport.core.jobs import create_job, fetch_stage

        tracks = make_tracks(5)
        source, target = FakeSource(tracks), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)

        # Drop the second track: every later position shifts by one.
        assert fetch_stage(FakeSource(tracks[:1] + tracks[2:]), job_id) == 0
        assert len(self._items(job_id)) == 5

    def test_a_genuinely_new_track_is_still_added(self, db):
        from playlistport.core.jobs import create_job, fetch_stage

        tracks = make_tracks(3)
        source, target = FakeSource(tracks), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)

        extra = CanonicalTrack(
            title="New", artists=["A"], source_id="sp-new", source_provider="spotify"
        )
        assert fetch_stage(FakeSource([extra] + tracks), job_id) == 1
        assert len(self._items(job_id)) == 4

    def test_a_second_copy_of_an_existing_track_is_added(self, db):
        # A playlist may legitimately hold the same track twice, so occurrence
        # counting must not collapse them.
        from playlistport.core.jobs import create_job, fetch_stage

        tracks = make_tracks(2)
        source, target = FakeSource(tracks), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)

        assert fetch_stage(FakeSource(tracks + [tracks[0]]), job_id) == 1
        assert len(self._items(job_id)) == 3


class TestFinalize:
    def test_job_with_nothing_outstanding_is_completed(self, db):
        from playlistport.core.jobs import finalize_job
        from playlistport.db.models import JobStatus

        job_id, _ = run_full(FakeSource(make_tracks(3)), FakeTarget())
        assert finalize_job(job_id) == JobStatus.COMPLETED.value

    def test_job_with_a_review_item_is_not_completed(self, db):
        from sqlalchemy import select

        from playlistport.core.jobs import create_job, fetch_stage, finalize_job
        from playlistport.db.models import ItemStatus as S
        from playlistport.db.models import JobStatus, TransferItem
        from playlistport.db.session import get_session

        source, target = FakeSource(make_tracks(1)), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)
        with get_session() as session:
            item = session.scalar(
                select(TransferItem).where(TransferItem.job_id == job_id)
            )
            item.status = S.NEEDS_REVIEW.value

        assert finalize_job(job_id) != JobStatus.COMPLETED.value


class TestUnsearchableTracks:
    """A real playlist contained a track with no title and no artists."""

    def test_empty_metadata_is_skipped_not_searched(self, db):
        from playlistport.core.jobs import create_job, fetch_stage, match_stage
        from playlistport.db.models import ItemStatus as S

        tracks = [
            CanonicalTrack(
                title="",
                artists=[],
                duration_ms=None,
                source_id="sp-empty",
                source_provider="spotify",
            )
        ]
        source, target = FakeSource(tracks), FakeTarget()
        job_id = create_job(source, target, "p1", "Test")
        fetch_stage(source, job_id)
        counts = match_stage(source, target, job_id, workers=1)

        # Searching for nothing is an HTTP 400, which looks transient and would
        # be retried forever, so the job could never complete.
        assert target.searches == 0
        assert counts.get(S.SKIPPED.value) == 1
        assert counts.get(S.FAILED.value, 0) == 0

    def test_job_with_only_unsearchable_tracks_still_completes(self, db):
        from playlistport.db.models import JobStatus, TransferJob
        from playlistport.db.session import get_session

        tracks = [
            CanonicalTrack(
                title="", artists=[], source_id="sp-empty", source_provider="spotify"
            )
        ]
        job_id, _ = run_full(FakeSource(tracks), FakeTarget())
        with get_session() as session:
            assert session.get(TransferJob, job_id).status == JobStatus.COMPLETED.value


class TestMatchCache:
    def test_second_job_does_not_research_known_tracks(self, db):
        tracks = make_tracks(5)
        target = FakeTarget()
        run_full(FakeSource(tracks), target)
        assert target.searches == 5

        # A different playlist containing the same tracks costs no searches.
        from playlistport.core.jobs import create_job, fetch_stage, match_stage

        source2 = FakeSource(tracks)
        job2 = create_job(source2, target, "p2", "Other")
        fetch_stage(source2, job2)
        match_stage(source2, target, job2, workers=2)
        assert target.searches == 5, "cache should have served every track"
