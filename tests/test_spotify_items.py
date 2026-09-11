"""Regression tests for Spotify playlist-item parsing.

A live run returned a 303-track playlist as zero tracks: Spotify had moved the
payload to an undocumented "item" key, and the converter dropped every row while
reporting success. These pin the shapes so that cannot recur silently.
"""

import pytest

from playlist_tool.providers.base import ProviderError
from playlist_tool.providers.spotify import SpotifyProvider

TRACK = {
    "id": "abc123",
    "type": "track",
    "name": "Essence",
    "artists": [{"name": "Wizkid"}, {"name": "Tems"}],
    "duration_ms": 248_000,
    "album": {"name": "Made in Lagos"},
    "external_ids": {"isrc": "ZZZ123456789"},
}


def convert(item):
    # Bypass __init__ so no credentials or network are required.
    provider = SpotifyProvider.__new__(SpotifyProvider)
    return provider._to_track(item, 0)


class TestPayloadShapes:
    def test_track_key(self):
        assert convert({"track": TRACK}).title == "Essence"

    def test_undocumented_item_key(self):
        # The shape that silently emptied a 303-track playlist.
        assert convert({"item": TRACK}).title == "Essence"

    def test_bare_track_object(self):
        assert convert(TRACK).title == "Essence"

    def test_metadata_is_carried_across(self):
        result = convert({"item": TRACK})
        assert result.artists == ["Wizkid", "Tems"]
        assert result.duration_ms == 248_000
        assert result.album == "Made in Lagos"
        assert result.isrc == "ZZZ123456789"
        assert result.source_provider == "spotify"


class TestPaginationGuard:
    """A short read must fail loudly rather than silently losing tracks."""

    def _provider(self, pages):
        provider = SpotifyProvider.__new__(SpotifyProvider)

        class FakeClient:
            def __init__(self):
                self.queue = list(pages[1:])

            def next(self, _page):
                return self.queue.pop(0) if self.queue else None

        provider._client = FakeClient()
        return provider

    def test_complete_pagination_returns_everything(self):
        pages = [
            {"total": 3, "items": [{"track": TRACK}, {"track": TRACK}], "next": "u"},
            {"total": 3, "items": [{"track": TRACK}], "next": None},
        ]
        assert len(self._provider(pages)._paginate(pages[0])) == 3

    def test_truncated_pagination_raises(self):
        # A 303-track playlist really did come back as 207 under load.
        pages = [{"total": 303, "items": [{"track": TRACK}] * 207, "next": None}]
        with pytest.raises(ProviderError, match="207 of 303"):
            self._provider(pages)._paginate(pages[0])

    def test_missing_total_is_tolerated(self):
        pages = [{"items": [{"track": TRACK}], "next": None}]
        assert len(self._provider(pages)._paginate(pages[0])) == 1


class TestSkips:
    def test_empty_wrapper_is_skipped(self):
        assert convert({"added_at": "x", "item": None}) is None

    def test_episode_is_skipped(self):
        assert convert({"track": {**TRACK, "type": "episode"}}) is None

    def test_local_file_is_skipped(self):
        assert convert({"is_local": True, "track": {**TRACK, "id": None}}) is None

    def test_track_without_id_is_skipped(self):
        assert convert({"track": {**TRACK, "id": None}}) is None
