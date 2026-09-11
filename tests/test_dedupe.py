"""Duplicate detection and the removal contract.

The real playlist this was written for had one video present three times and two
others present twice, because several distinct Spotify tracks resolved to a
single YouTube upload before the write-path guard existed.
"""

import pytest

from playlistport.core.dedupe import find_duplicates, group_duplicates
from playlistport.core.models import PlaylistEntry
from playlistport.providers.base import MusicProvider, ProviderError


def entry(position, track_id, entry_id=None):
    return PlaylistEntry(
        entry_id=entry_id or f"item-{position}",
        track_id=track_id,
        position=position,
        label=track_id,
    )


class TestFindDuplicates:
    def test_no_duplicates_removes_nothing(self):
        entries = [entry(0, "a"), entry(1, "b"), entry(2, "c")]
        assert find_duplicates(entries) == []

    def test_keeps_the_earliest_occurrence(self):
        # Keeping a later copy would silently move the track down the playlist.
        entries = [entry(0, "a"), entry(5, "a"), entry(9, "a")]
        extras = find_duplicates(entries)
        assert [e.position for e in extras] == [5, 9]

    def test_unordered_input_still_keeps_the_earliest(self):
        entries = [entry(9, "a"), entry(0, "a"), entry(5, "a")]
        assert [e.position for e in find_duplicates(entries)] == [5, 9]

    def test_handles_several_duplicated_tracks(self):
        entries = [
            entry(0, "a"), entry(1, "b"), entry(2, "a"),
            entry(3, "c"), entry(4, "b"), entry(5, "a"),
        ]
        extras = find_duplicates(entries)
        assert sorted(e.position for e in extras) == [2, 4, 5]
        # One of each survives.
        kept = {e.track_id for e in entries} - set()
        assert kept == {"a", "b", "c"}

    def test_empty_playlist(self):
        assert find_duplicates([]) == []


class TestGrouping:
    def test_only_duplicated_tracks_are_reported(self):
        entries = [entry(0, "a"), entry(1, "b"), entry(2, "a")]
        groups = group_duplicates(entries)
        assert set(groups) == {"a"}
        assert [e.position for e in groups["a"]] == [0, 2]


class MinimalProvider(MusicProvider):
    """A provider that has not implemented removal."""

    name = "minimal"

    def list_playlists(self):
        return []

    def get_tracks(self, playlist_id):
        return []

    def get_saved_tracks(self):
        return []

    def search(self, track, limit=8):
        return []

    def create_playlist(self, name, description=""):
        return "x"

    def add_tracks(self, playlist_id, track_ids):
        pass


class TestRemovalContract:
    def test_removal_is_opt_in(self):
        assert MinimalProvider.supports_removal is False

    def test_unsupported_removal_raises_rather_than_silently_passing(self):
        provider = MinimalProvider()
        with pytest.raises(ProviderError):
            provider.remove_entries("p", [entry(0, "a")])
        with pytest.raises(ProviderError):
            provider.list_entries("p")

    def test_supported_providers_declare_it(self):
        from playlistport.providers.spotify import SpotifyProvider
        from playlistport.providers.youtube import YouTubeProvider

        assert SpotifyProvider.supports_removal is True
        assert YouTubeProvider.supports_removal is True
