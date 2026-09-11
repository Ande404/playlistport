"""Matcher behaviour, encoded as the failure modes that actually matter.

No network. These are the fixtures the weights get tuned against.
"""

from playlist_tool.core.matcher import DEFAULT_CONFIG, duration_score, match, score_candidate
from playlist_tool.core.models import Bucket, Candidate, CanonicalTrack


def track(title, artists, duration_ms=200_000, album=None):
    return CanonicalTrack(
        title=title, artists=artists, duration_ms=duration_ms, album=album
    )


def candidate(title, artists, duration_ms=200_000, album=None, id_="x"):
    return Candidate(
        id=id_, title=title, artists=artists, duration_ms=duration_ms, album=album
    )


class TestDurationScore:
    def test_exact_is_perfect(self):
        assert duration_score(200_000, 200_500) == 1.0

    def test_large_gap_is_zero(self):
        assert duration_score(200_000, 260_000) == 0.0

    def test_unknown_duration_is_neutral(self):
        # Missing data must neither reward nor punish a candidate.
        assert duration_score(200_000, None) == 0.5


class TestObviousMatches:
    def test_identical_track_auto_matches(self):
        result = match(
            track("Blinding Lights", ["The Weeknd"], 200_040),
            [candidate("Blinding Lights", ["The Weeknd"], 200_000)],
        )
        assert result.bucket is Bucket.AUTO
        assert result.score > 0.9

    def test_release_noise_does_not_block_a_match(self):
        result = match(
            track("Bohemian Rhapsody - Remastered 2011", ["Queen"], 354_000),
            [candidate("Bohemian Rhapsody (Official Video)", ["Queen"], 355_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_feature_credited_in_title_vs_artist_field(self):
        # Spotify puts the feature in the title; YouTube Music in the artist
        # list. Flattening both sides must make them equivalent.
        result = match(
            track("Sunflower (feat. Post Malone)", ["Swae Lee"], 158_000),
            [candidate("Sunflower", ["Swae Lee", "Post Malone"], 158_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_accent_difference_still_matches(self):
        result = match(
            track("Adagio For Strings", ["Tiësto"], 400_000),
            [candidate("Adagio for Strings", ["Tiesto"], 400_000)],
        )
        assert result.bucket is Bucket.AUTO


class TestAsymmetricQualifiers:
    """Regressions from the first live run against YouTube Music."""

    def test_soundtrack_qualifier_on_one_side_only(self):
        # YT Music appends the film name; Spotify does not. Previously 0.28.
        result = match(
            track("Sunflower (feat. Post Malone)", ["Swae Lee"], 158_040),
            [
                candidate(
                    "Sunflower (Spider-Man: Into the Spider-Verse)",
                    ["Post Malone", "Swae Lee"],
                    158_000,
                )
            ],
        )
        assert result.bucket is Bucket.AUTO

    def test_radio_edit_bracket_and_dash_forms_agree(self):
        # "- Radio Edit" was stripped but "(Radio Edit)" was not. Previously 0.45.
        result = match(
            track("Levels - Radio Edit", ["Avicii"], 200_000),
            [candidate("Levels (Radio Edit)", ["Avicii"], 200_500)],
        )
        assert result.bucket is Bucket.AUTO

    def test_pruning_never_collapses_a_live_recording(self):
        # The guard on the fix above: variant tags survive pruning.
        result = match(
            track("Song", ["Artist"], 200_000),
            [candidate("Song (Live at Wembley)", ["Artist"], 201_000)],
        )
        assert result.bucket is not Bucket.AUTO


class TestYouTubeTitleConventions:
    """Regressions from the first live YouTube -> Spotify run."""

    def test_artist_prefix_in_source_title(self):
        # A YouTube video title repeats its artist. Previously 0.80.
        result = match(
            track("saybik - PAINKILLER", ["saybik"], 180_000),
            [candidate("Painkiller", ["saybik"], 180_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_artist_prefix_known_only_from_the_other_side(self):
        # The uploader channel is the wrong artist, so the prefix can only be
        # recognized using the candidate's artist list.
        result = match(
            track("DMX - Ruff Ryders Anthem", ["Alt Vault"], 210_000),
            [candidate("Ruff Ryders' Anthem", ["DMX"], 210_000)],
        )
        assert result.components["title"] > 0.85

    def test_ordinary_dashed_title_is_untouched(self):
        # The head must match an artist exactly, so this must not be stripped.
        result = match(
            track("Marvin Gaye - What's Going On", ["Someone Else"], 200_000),
            [candidate("Marvin Gaye - What's Going On", ["Someone Else"], 200_000)],
        )
        assert result.components["title"] == 1.0

    def test_live_upload_still_rejected_after_prefix_strip(self):
        # Stripping the prefix must not let a live cut through.
        result = match(
            track("DMX - Ruff Ryders Anthem (Live)", ["Alt Vault"], 250_000),
            [candidate("Ruff Ryders' Anthem", ["DMX"], 210_000)],
        )
        assert result.bucket is not Bucket.AUTO


class TestVariantRejection:
    def test_live_version_is_not_auto_matched(self):
        result = match(
            track("Song", ["Artist"], 200_000),
            [candidate("Song (Live at Wembley)", ["Artist"], 202_000)],
        )
        assert result.bucket is not Bucket.AUTO

    def test_karaoke_is_heavily_penalized(self):
        result = match(
            track("Shape of You", ["Ed Sheeran"], 233_000),
            [candidate("Shape of You (Karaoke Version)", ["Ed Sheeran"], 233_000)],
        )
        assert result.bucket is not Bucket.AUTO
        assert any("karaoke" in key for key in result.penalties)

    def test_remix_does_not_match_original(self):
        result = match(
            track("Levels", ["Avicii"], 200_000),
            [candidate("Levels (Skrillex Remix)", ["Avicii"], 205_000)],
        )
        assert result.bucket is not Bucket.AUTO

    def test_matching_remix_on_both_sides_is_fine(self):
        # The penalty is for *mismatch*, not for the word appearing.
        result = match(
            track("Levels (Skrillex Remix)", ["Avicii"], 200_000),
            [candidate("Levels (Skrillex Remix)", ["Avicii"], 200_500)],
        )
        assert result.bucket is Bucket.AUTO

    def test_sped_up_edit_is_rejected(self):
        result = match(
            track("Say It Right", ["Nelly Furtado"], 190_000),
            [candidate("Say It Right (Sped Up)", ["Nelly Furtado"], 160_000)],
        )
        assert result.bucket is not Bucket.AUTO


class TestArtistCreditVariation:
    """From the 2,472-track sample: correct matches held below the threshold."""

    def test_country_disambiguation_suffix(self):
        result = match(
            track("Muhuuuuu", ["Lazare", "ADDAM (BE)"], 200_000),
            [candidate("Muhuuuuu", ["Lazare", "ADDAM"], 200_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_partial_artist_name(self):
        result = match(
            track("Noba", ["DJ Tomer", "Ricardo Gi", "NaakMusiQ"], 200_000),
            [candidate("Noba (feat. NaakMusiQ)", ["DJ Tomer", "Ricardo"], 200_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_alias_spelling(self):
        result = match(
            track("Bad Boy for Life", ["Diddy", "Black Rob"], 200_000),
            [candidate("Bad Boy for Life", ["P. Diddy", "Black Rob"], 200_000)],
        )
        assert result.bucket is Bucket.AUTO

    def test_extra_featured_artist_on_one_side(self):
        result = match(
            track("Jolie Fille", ["Maz", "Antdot", "Ginton", "Layefa"], 200_000),
            [
                candidate(
                    "Jolie Fille (feat. Maz & Antdot)",
                    ["Ginton", "Layefa", "Dawn Patrol"],
                    200_000,
                )
            ],
        )
        assert result.bucket is Bucket.AUTO

    def test_unrelated_artists_still_rejected(self):
        # The guard: leniency must not make different performers match.
        result = match(
            track("Hallelujah", ["Jeff Buckley"], 409_000),
            [candidate("Hallelujah", ["Pentatonix"], 409_000)],
        )
        assert result.bucket is not Bucket.AUTO


class TestIsrcMatching:
    """ISRC identifies a recording, so agreement settles identity outright."""

    ISRC = "GBUM71029604"

    def test_isrc_match_wins_despite_unrecognisable_metadata(self):
        # Different title, different artist spelling, different duration — the
        # fuzzy path would reject this outright.
        result = match(
            CanonicalTrack("Song", ["Artist"], 200_000, isrc=self.ISRC),
            [
                Candidate(
                    id="exact",
                    title="Completely Different Title",
                    artists=["Someone Else"],
                    duration_ms=999_000,
                    isrc=self.ISRC,
                )
            ],
        )
        assert result.bucket is Bucket.AUTO
        assert result.score == 1.0
        assert result.components == {"isrc": 1.0}

    def test_isrc_is_normalised(self):
        result = match(
            CanonicalTrack("Song", ["Artist"], 200_000, isrc="gb-um7-10-29604"),
            [candidate("Song", ["Artist"], 200_000, id_="x")],
        )
        # No candidate ISRC to compare, so it falls through to fuzzy scoring.
        assert result.components != {"isrc": 1.0}

        result = match(
            CanonicalTrack("Song", ["Artist"], 200_000, isrc="gb-um7-10-29604"),
            [
                Candidate(
                    id="exact", title="Other", artists=["Other"], isrc="GBUM71029604"
                )
            ],
        )
        assert result.score == 1.0

    def test_malformed_isrc_is_ignored(self):
        # Two identical junk values must not be treated as proof of identity.
        result = match(
            CanonicalTrack("Song", ["Artist"], 200_000, isrc="unknown"),
            [
                Candidate(
                    id="x",
                    title="Totally Different",
                    artists=["Nobody"],
                    duration_ms=400_000,
                    isrc="unknown",
                )
            ],
        )
        assert result.bucket is not Bucket.AUTO

    def test_mismatched_isrc_does_not_block_fuzzy_match(self):
        # A re-release can carry a different ISRC; absence of proof is not proof
        # of absence, so fuzzy scoring must still be allowed to succeed.
        result = match(
            CanonicalTrack("Blinding Lights", ["The Weeknd"], 200_000, isrc=self.ISRC),
            [
                Candidate(
                    id="y",
                    title="Blinding Lights",
                    artists=["The Weeknd"],
                    duration_ms=200_000,
                    isrc="USUG11904206",
                )
            ],
        )
        assert result.bucket is Bucket.AUTO

    def test_isrc_only_considered_when_source_has_one(self):
        result = match(
            track("Song", ["Artist"], 200_000),
            [Candidate(id="z", title="Song", artists=["Artist"],
                       duration_ms=200_000, isrc=self.ISRC)],
        )
        assert result.bucket is Bucket.AUTO
        assert "title" in result.components


class TestWrongTrack:
    def test_different_song_is_unmatched(self):
        result = match(
            track("Blinding Lights", ["The Weeknd"], 200_000),
            [candidate("Levels", ["Avicii"], 200_000)],
        )
        assert result.bucket is Bucket.UNMATCHED

    def test_right_title_wrong_artist_is_not_auto(self):
        # Cover-song trap: correct title, unrelated performer.
        result = match(
            track("Hallelujah", ["Jeff Buckley"], 409_000),
            [candidate("Hallelujah", ["Random Cover Band"], 409_000)],
        )
        assert result.bucket is not Bucket.AUTO

    def test_no_candidates_is_unmatched(self):
        result = match(track("Anything", ["Someone"]), [])
        assert result.bucket is Bucket.UNMATCHED
        assert result.best is None


class TestRanking:
    def test_best_candidate_wins_and_others_are_kept(self):
        result = match(
            track("Song", ["Artist"], 200_000),
            [
                candidate("Song (Live)", ["Artist"], 240_000, id_="live"),
                candidate("Song", ["Artist"], 200_000, id_="studio"),
                candidate("Song (Karaoke)", ["Artist"], 200_000, id_="karaoke"),
            ],
        )
        assert result.best.id == "studio"
        # Runners-up are what the review UI offers the user.
        assert len(result.runners_up) == 2
        assert result.runners_up[0][1] <= result.score

    def test_components_are_reported_for_tuning(self):
        result = match(
            track("Song", ["Artist"], 200_000),
            [candidate("Song", ["Artist"], 200_000)],
        )
        assert set(result.components) == {"title", "artist", "duration", "album"}
        assert result.explain()


class TestThresholds:
    def test_score_is_bounded(self):
        score, _, _ = score_candidate(
            track("Song", ["Artist"], 200_000),
            candidate("Song", ["Artist"], 200_000),
            DEFAULT_CONFIG,
        )
        assert 0.0 <= score <= 1.0
