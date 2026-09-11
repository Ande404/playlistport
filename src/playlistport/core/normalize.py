"""Title and artist normalization.

Two different jobs live here and they must not be confused:

1. `canonical()` strips *release noise* — text that differs across platforms but
   means nothing musically ("Remastered 2011", "Official Video", "4K").
2. `variant_tags()` detects text that means a *different recording*
   ("Live", "Acoustic", "Karaoke"). This is deliberately read from the ORIGINAL
   title, before noise stripping, because it must survive normalization.

Conflating the two is the classic failure mode: strip "Live" as noise and you
cheerfully match a studio track to a concert recording.
"""

from __future__ import annotations

import re
import unicodedata

# Bracketed content matching any of these is release noise and gets dropped.
_NOISE_KEYWORDS = [
    # The year floats to either side: "2011 Remaster" and "Remastered 2011".
    r"(?:\d{4}\s+)?re-?master(?:ed)?(?:\s+\d{4})?",
    r"official\s+(music\s+)?video",
    r"official\s+(audio|visualizer|lyric\s+video)",
    r"lyrics?(\s+video)?",
    r"music\s+video",
    r"visuali[sz]er",
    r"audio",
    r"hd|hq|4k|1080p|720p",
    r"explicit|clean",
    r"bonus\s+track",
    r"album\s+version",
    r"single\s+version",
    r"radio\s+edit",
    r"deluxe(\s+edition)?",
    r"(?:\d+\w*\s+)?anniversary\s+edition",
    r"mono|stereo(\s+version)?",
    r"full\s+song",
    r"from\s+[\"'].*[\"']",
    r"music\s+from\s+.*",
    r"original\s+(motion\s+picture\s+)?(soundtrack|score)",
]
_NOISE_RE = re.compile(r"^\W*(?:%s)\W*$" % "|".join(_NOISE_KEYWORDS), re.IGNORECASE)

# Bracketed content containing these is musically meaningful — never dropped.
_MEANINGFUL_RE = re.compile(
    r"\b(remix|mix|live|acoustic|instrumental|cover|karaoke|demo|reprise|"
    r"unplugged|version|edit|mashup|sped\s*up|slowed|nightcore)\b",
    re.IGNORECASE,
)

_BRACKET_RE = re.compile(r"[\(\[\{]([^)\]\}]*)[\)\]\}]")

# "- Remastered 2011" / "- Official Video" trailing on a dash instead of brackets.
_TRAILING_NOISE_RE = re.compile(
    r"\s+[-–—]\s+(?:%s)\s*$" % "|".join(_NOISE_KEYWORDS), re.IGNORECASE
)

_FEAT_RE = re.compile(
    r"[\(\[]?\s*\b(?:feat|ft|featuring|with)\b\.?\s+([^)\]\[]+)[\)\]]?",
    re.IGNORECASE,
)

_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|;|\bx\b|\band\b|\bvs\.?\b)\s*", re.IGNORECASE)

# Tag -> pattern. Presence on only one side of a comparison is penalized.
_VARIANT_PATTERNS: dict[str, re.Pattern[str]] = {
    "live": re.compile(r"\blive\b", re.IGNORECASE),
    "acoustic": re.compile(r"\bacoustic|unplugged\b", re.IGNORECASE),
    "remix": re.compile(r"\bremix(es)?\b", re.IGNORECASE),
    "instrumental": re.compile(r"\binstrumental\b", re.IGNORECASE),
    "demo": re.compile(r"\bdemo\b", re.IGNORECASE),
    "cover": re.compile(r"\bcover(ed)?\s*(by|version)?\b", re.IGNORECASE),
    "karaoke": re.compile(r"\bkaraoke|backing\s+track\b", re.IGNORECASE),
    "sped_up": re.compile(r"\bsped\s*up|speed\s*up\b", re.IGNORECASE),
    "slowed": re.compile(r"\bslowed|reverb\b", re.IGNORECASE),
    "nightcore": re.compile(r"\bnightcore\b", re.IGNORECASE),
    "mashup": re.compile(r"\bmashup|mash-up\b", re.IGNORECASE),
}

# Mismatching these means the wrong performer entirely, not a different mix.
SEVERE_VARIANTS = frozenset({"cover", "karaoke"})


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def strip_noise(title: str) -> str:
    """Remove release noise while preserving musically meaningful qualifiers."""

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        # Noise is checked FIRST. "Radio Edit" contains the meaningful-looking
        # word "edit", but it is a known noise phrase in full — and the trailing
        # "- Radio Edit" path strips it regardless, so checking meaningfulness
        # first would make the bracket and dash forms disagree.
        if _NOISE_RE.match(inner):
            return " "
        if _MEANINGFUL_RE.search(inner):
            return match.group(0)
        return match.group(0)

    out = _BRACKET_RE.sub(_replace, title)
    # Apply repeatedly: "Song - Remastered - Official Video" has stacked suffixes.
    while True:
        stripped = _TRAILING_NOISE_RE.sub("", out)
        if stripped == out:
            break
        out = stripped
    return re.sub(r"\s+", " ", out).strip(" -–—")


def extract_features(title: str) -> tuple[str, list[str]]:
    """Lift 'feat. X' credits out of a title into a list of artist names.

    Platforms disagree constantly about whether a feature belongs in the title
    or the artist field, so both sides get flattened the same way before
    comparison.
    """
    featured: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        featured.extend(split_artists(match.group(1)))
        return " "

    cleaned = _FEAT_RE.sub(_capture, title)
    return re.sub(r"\s+", " ", cleaned).strip(" -–—"), featured


def split_artists(raw: str) -> list[str]:
    """Split a combined artist string into individual names, order preserved."""
    parts = [p.strip() for p in _ARTIST_SPLIT_RE.split(raw)]
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        key = part.casefold()
        if key not in seen:
            seen.add(key)
            out.append(part)
    return out


def canonical(text: str) -> str:
    """Aggressively fold a string for comparison. Not reversible, not for display."""
    text = strip_accents(text).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_title(title: str) -> tuple[str, list[str]]:
    """Full title pipeline. Returns (canonical title, featured artists)."""
    without_noise = strip_noise(title)
    base, featured = extract_features(without_noise)
    return canonical(base), featured


#: "Artist - Title", the near-universal YouTube video naming convention.
_TITLE_PREFIX_RE = re.compile(r"^\s*(?P<head>[^-–—:|]{1,60}?)\s*[-–—:|]\s*(?P<rest>.+)$")


def strip_artist_prefix(title: str, artists: list[str]) -> str:
    """Drop a leading "<artist> -" that merely repeats the artist field.

    YouTube video titles are self-contained because a video has no artist
    column: "saybik - PAINKILLER". Spotify stores the same track as title
    "PAINKILLER" with artist "saybik". Compared raw, that scores ~0.64 and a
    correct match drops below the auto threshold.

    The head must equal an artist name *exactly* after folding, so ordinary
    titles that happen to contain a dash are left alone.
    """
    if not artists:
        return title

    known: set[str] = set()
    for artist in artists:
        for part in [artist, *split_artists(artist)]:
            folded = canonical(part)
            if folded:
                known.add(folded)
    if not known:
        return title

    # At most two passes: "Label - Artist - Title" occurs, deeper nesting does not.
    for _ in range(2):
        match = _TITLE_PREFIX_RE.match(title)
        if not match or canonical(match.group("head")) not in known:
            break
        title = match.group("rest").strip()
    return title


_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def canonical_isrc(value: str | None) -> str | None:
    """Normalize an ISRC, or return None if it is not a valid one.

    An ISRC identifies a *recording*, so two tracks sharing one are the same
    performance regardless of how their titles or artist credits are written.
    That makes it the only exact identity signal available across platforms —
    but only when it is genuinely well-formed, hence the validation: a malformed
    value matching another malformed value must never be treated as proof.
    """
    if not value:
        return None
    folded = re.sub(r"[\s-]", "", value).upper()
    return folded if _ISRC_RE.match(folded) else None


def search_terms(title: str, artists: list[str]) -> str:
    """Reduce a title to the words worth sending to a provider's search.

    Matching and searching want opposite things. Matching needs every
    qualifier, so it can penalize a live take. Searching needs the *shortest
    unambiguous* phrase, because search engines degrade badly with noise —
    a real query for

        "Badman mbanyumize jenva - Eagle eye ( freestyle audio )"

    returned exactly one (wrong) result, while the track's core title finds it.
    So for search purposes only, the artist prefix, release noise and *every*
    bracketed aside are dropped. Nothing here affects scoring.
    """
    base = strip_artist_prefix(title, artists)
    base = strip_noise(base)
    base = _BRACKET_RE.sub(" ", base)
    base, _ = extract_features(base)
    return re.sub(r"\s+", " ", base).strip(" -–—")


def prepare_title_pair(left: str, right: str) -> tuple[str, list[str], str, list[str]]:
    """Normalize two titles *against each other* before comparison.

    Beyond the fixed noise list, platforms attach one-sided qualifiers that are
    impossible to enumerate — soundtrack names, album editions, market tags:

        Spotify:       "Sunflower"
        YouTube Music: "Sunflower (Spider-Man: Into the Spider-Verse)"

    Comparing those directly scores ~0.28 and loses an obviously correct match.
    So a bracket group is dropped when it is (a) absent from the other title and
    (b) carries no variant tag. Asymmetric bracket content that means nothing
    musically *is* noise, by definition — but "(Live)" or "(Skrillex Remix)"
    carries a variant tag and always survives, so this can never collapse a
    studio cut onto a live recording.

    Returns (canonical left, left features, canonical right, right features).
    """
    left_clean, right_clean = strip_noise(left), strip_noise(right)
    left_ref, right_ref = canonical(left_clean), canonical(right_clean)

    def _prune(text: str, other_canonical: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            inner = match.group(1).strip()
            if variant_tags(inner):
                return match.group(0)
            folded = canonical(inner)
            if folded and folded in other_canonical:
                return match.group(0)
            return " "

        pruned = _BRACKET_RE.sub(_replace, text)
        return re.sub(r"\s+", " ", pruned).strip(" -–—")

    left_pruned = _prune(left_clean, right_ref)
    right_pruned = _prune(right_clean, left_ref)

    left_base, left_featured = extract_features(left_pruned)
    right_base, right_featured = extract_features(right_pruned)
    return canonical(left_base), left_featured, canonical(right_base), right_featured


#: Spotify disambiguates same-named acts with a country or number suffix —
#: "Maz (BR)", "APACHE (FR)", "ADDAM (BE)". YouTube Music carries the bare name,
#: so the suffix is pure noise when comparing artists.
_ARTIST_SUFFIX_RE = re.compile(r"\s*[\(\[]\s*[A-Z]{2,3}\d?\s*[\)\]]\s*$")


def canonical_artists(artists: list[str]) -> set[str]:
    """Flatten an artist list into a comparable token set."""
    out: set[str] = set()
    for artist in artists:
        for part in split_artists(_ARTIST_SUFFIX_RE.sub("", artist)):
            folded = canonical(_ARTIST_SUFFIX_RE.sub("", part))
            if folded:
                out.add(folded)
    return out


def variant_tags(*texts: str | None) -> set[str]:
    """Detect recording-variant markers. Read from ORIGINAL, un-stripped text."""
    blob = " ".join(t for t in texts if t)
    return {tag for tag, pattern in _VARIANT_PATTERNS.items() if pattern.search(blob)}
