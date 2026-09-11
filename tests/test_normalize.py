from playlistport.core.normalize import (
    canonical,
    canonical_artists,
    canonical_title,
    extract_features,
    search_terms,
    split_artists,
    strip_artist_prefix,
    strip_noise,
    variant_tags,
)


class TestSearchTerms:
    """Search wants the shortest unambiguous phrase; matching wants everything."""

    def test_drops_every_bracketed_aside(self):
        # This exact title returned one wrong result from Spotify search.
        terms = search_terms(
            "Badman mbanyumize jenva - Eagle eye ( freestyle audio )", ["SHIRTLESS"]
        )
        assert "freestyle" not in terms.lower()
        assert "badman" in terms.lower()

    def test_drops_artist_prefix(self):
        assert search_terms("saybik - PAINKILLER", ["saybik"]) == "PAINKILLER"

    def test_keeps_plain_title_intact(self):
        assert search_terms("Blinding Lights", ["The Weeknd"]) == "Blinding Lights"

    def test_never_returns_empty_for_bracket_only_title(self):
        # Guard: providers fall back to the raw title, but this must not crash.
        assert search_terms("(Live)", ["X"]) == ""


class TestStripArtistPrefix:
    def test_strips_matching_prefix(self):
        assert strip_artist_prefix("saybik - PAINKILLER", ["saybik"]) == "PAINKILLER"

    def test_leaves_non_artist_prefix_alone(self):
        title = "Marvin Gaye - What's Going On"
        assert strip_artist_prefix(title, ["Someone Else"]) == title

    def test_no_artists_is_a_noop(self):
        assert strip_artist_prefix("A - B", []) == "A - B"


class TestStripNoise:
    def test_removes_remaster_brackets(self):
        assert strip_noise("Bohemian Rhapsody (Remastered 2011)") == "Bohemian Rhapsody"

    def test_removes_trailing_dash_noise(self):
        assert strip_noise("Come Together - Remastered 2009") == "Come Together"

    def test_removes_youtube_video_noise(self):
        assert strip_noise("Blinding Lights (Official Music Video)") == "Blinding Lights"

    def test_removes_stacked_suffixes(self):
        assert strip_noise("Song - Remastered - Official Video") == "Song"

    def test_preserves_musically_meaningful_brackets(self):
        # Dropping these would match a studio cut to a live recording.
        assert "live" in strip_noise("Song (Live at Wembley)").lower()
        assert "remix" in strip_noise("Song (Deadmau5 Remix)").lower()


class TestFeatures:
    def test_lifts_feat_out_of_title(self):
        title, featured = extract_features("Sunflower (feat. Post Malone)")
        assert title == "Sunflower"
        assert featured == ["Post Malone"]

    def test_handles_ft_without_brackets(self):
        title, featured = extract_features("Love Me ft. Drake")
        assert title == "Love Me"
        assert featured == ["Drake"]

    def test_splits_multiple_features(self):
        _, featured = extract_features("Track (feat. A & B)")
        assert featured == ["A", "B"]


class TestSplitArtists:
    def test_splits_on_common_separators(self):
        assert split_artists("Jay-Z & Kanye West") == ["Jay-Z", "Kanye West"]
        assert split_artists("A, B, C") == ["A", "B", "C"]

    def test_deduplicates_case_insensitively(self):
        assert split_artists("Drake & drake") == ["Drake"]


class TestCanonical:
    def test_folds_accents_and_case(self):
        assert canonical("Tiësto") == canonical("Tiesto")

    def test_expands_ampersand(self):
        assert canonical("Simon & Garfunkel") == "simon and garfunkel"

    def test_title_pipeline_returns_features(self):
        title, featured = canonical_title("Sicko Mode (feat. Drake) [Official Audio]")
        assert title == "sicko mode"
        assert featured == ["Drake"]

    def test_artist_set_is_flattened(self):
        assert canonical_artists(["Jay-Z & Kanye West"]) == {"jay z", "kanye west"}


class TestVariantTags:
    def test_detects_live(self):
        assert "live" in variant_tags("Song (Live at Wembley)")

    def test_detects_karaoke(self):
        assert "karaoke" in variant_tags("Song - Karaoke Version")

    def test_clean_title_has_no_tags(self):
        assert variant_tags("Blinding Lights") == set()

    def test_reads_from_original_not_stripped_title(self):
        # The whole point: noise stripping must not erase variant signal.
        title = "Song (Live) (Remastered 2011)"
        assert "live" in variant_tags(title)
