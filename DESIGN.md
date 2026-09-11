# Playlist Tool — Design

Transfer playlists between Spotify and YouTube Music for an authorized user who
holds accounts on both. Local single-user tool first; multi-user web app later.

---

## 1. The core problem

The hard part is **track matching**, not OAuth or playlist CRUD.

There is **no shared identifier** between the platforms. Spotify exposes ISRC;
YouTube Music does not surface it. So matching is fuzzy scoring over
title / artist / duration / album, and it will never be 100%. Realistic
expectation on mainstream catalog is ~85–92%, materially worse on remixes,
classical, non-Latin scripts, and regional music.

That single fact drives every other decision below:

- Every track gets a **confidence score**, not a boolean.
- Three outcomes: `auto` / `needs_review` / `unmatched`.
- A **human review step** is a first-class feature, not an error path.
- Human decisions are **persisted and reused** (`match_cache`), so the tool gets
  better the more it is used.

## 2. Platform constraints

### 2.1 YouTube quota — the binding constraint

YouTube Data API v3 gives **10,000 quota units/day** by default.

| Call | Units |
| --- | --- |
| `playlists.list` | 1 |
| `playlistItems.list` | 1 |
| `playlists.insert` | 50 |
| `playlistItems.insert` | 50 |
| `search.list` | **100** |

Naive design (official search + official insert) = 150 units/track =
**~66 tracks/day**. That is unusable. Quota increases require a compliance audit
that is slow and routinely denied for hobby projects.

**Chosen split (hybrid):**

| Operation | Client | Cost |
| --- | --- | --- |
| List YT playlists and items | Official Data API | ~1 unit — negligible |
| Search for a track | `ytmusicapi`, **unauthenticated** | free |
| Enrich videoId → song metadata | `ytmusicapi`, **unauthenticated** | free |
| Create playlist / add tracks | Official Data API | 50 units each |

`ytmusicapi` never receives credentials — it is used only for anonymous search
and metadata lookup, the lowest-risk possible use of an unofficial client. All
authenticated access goes through Google OAuth.

Remaining ceiling: 10,000 / 50 = **~198 tracks/day written**. The write path is
therefore behind `YT_WRITE_MODE=official|ytmusicapi` so it can be flipped for a
large backlog without touching anything else.

### 2.2 Why ytmusicapi for search specifically

Searching YouTube proper returns music videos, lyric videos, sped-up edits,
8-hour loops and live cuts. Searching YouTube **Music** returns catalog *song*
entries with structured `artists`, `album` and `duration` fields — exactly the
signals the matcher needs. The same asymmetry applies in reverse: parsing
`"Artist – Title (Official Music Video) [4K]"` back into a Spotify query is
lossy, whereas a YTM song entry hands over clean metadata.

### 2.3 Google OAuth gotchas

- Creating playlists needs `youtube` / `youtube.force-ssl`, which are
  **restricted** scopes — full verification is required for production.
  Acceptable for personal use.
- An unverified app in **"Testing"** status issues refresh tokens that
  **expire after 7 days**. Weekly re-authorization during development is
  expected; the auth flow must be cheap to re-run, not a surprise.

### 2.4 Spotify gotchas

- Apps in Development Mode are capped at **25 users**, added by email in the
  dashboard. Fine for MVP; extension quota is a real hurdle for a public app.
- Endpoints deprecated for new apps (Nov 2024): `audio-features`,
  `audio-analysis`, `recommendations`, `related-artists`, 30s previews. Most
  relevant here: **algorithmic playlists (Discover Weekly, Daily Mix, Release
  Radar) cannot be read at all.** Only owned and followed playlists.
- Redirect URI must be `http://127.0.0.1:...` — `localhost` is rejected.
- `playlist_add_items` accepts 100 URIs per request; playlists cap at 10,000
  items.

### 2.5 Content edge cases

Local files in Spotify playlists (no track ID), podcast episodes, region-blocked
tracks, duplicate entries, and playlists exceeding platform limits. Each is
skipped with an explicit reason recorded, never silently dropped.

## 3. Architecture

```
src/playlist_tool/
  providers/
    base.py        MusicProvider ABC — the entire contract
    spotify.py     spotipy
    youtube.py     official API (auth/read/write) + ytmusicapi (search/enrich)
  core/
    models.py      CanonicalTrack, Candidate, MatchResult
    normalize.py   title/artist cleaning, variant-tag detection
    matcher.py     scoring — provider-independent, pure functions
    jobs.py        fetch -> match -> review -> write state machine   [phase 2]
  db/              SQLAlchemy + SQLite                               [phase 2]
  api/             FastAPI                                           [phase 3]
web/               React + Vite + TanStack Query                     [phase 3]
```

### 3.1 The provider contract

```python
class MusicProvider(ABC):
    def list_playlists() -> list[PlaylistRef]
    def get_tracks(playlist_id) -> list[CanonicalTrack]
    def get_saved_tracks() -> list[CanonicalTrack]
    def search(track: CanonicalTrack) -> list[Candidate]
    def create_playlist(name, description) -> str
    def add_tracks(playlist_id, ids) -> None
```

`CanonicalTrack = {title, artists[], album, duration_ms, isrc?, source_id,
source_provider}`.

Nothing outside `providers/` knows Spotify or YouTube exist. That is what makes
Apple Music a plugin rather than a rewrite, and what lets the matcher be unit
tested against fixtures with zero network access.

**Note:** the interface is synchronous. Both `spotipy` and `ytmusicapi` are
blocking; an async facade over them would be theatre. FastAPI runs sync
dependencies in a threadpool, and parallelism where it matters (search fan-out)
uses an explicit `ThreadPoolExecutor`.

### 3.2 Matcher

Normalization strips release noise (`(Remastered 2011)`, `- Radio Edit`,
`[Official Video]`), lifts `feat.` credits out of the title into the artist
list, folds unicode and collapses punctuation.

**Titles are normalized pairwise, not in isolation** (`prepare_title_pair`).
A fixed noise list cannot enumerate every one-sided qualifier platforms attach —
soundtrack names, album editions, market tags:

```
Spotify:        "Sunflower"
YouTube Music:  "Sunflower (Spider-Man: Into the Spider-Verse)"
```

Compared directly these score ~0.28 and an obviously correct match is lost. So a
bracket group is dropped when it is both absent from the other title and free of
variant tags. The safety property that makes this sound: `(Live)` and
`(Skrillex Remix)` carry variant tags and therefore *always* survive pruning, so
it can never collapse a studio cut onto a live recording.

The same rule fixed a second class of bug found in the first live run — a noise
phrase stripped in dash form (`- Radio Edit`) but preserved in bracket form
(`(Radio Edit)`), because the substring `edit` looked musically meaningful.
Noise is now matched before meaningfulness, so both forms agree.

Scored components:

| Signal | Weight | Notes |
| --- | --- | --- |
| Title similarity | 0.42 | blend of sequence ratio and token Jaccard |
| Artist similarity | 0.33 | **set overlap**, not string equality — feature-credit ordering differs across platforms |
| Duration delta | 0.20 | **strongest single signal**; ≤2s ≈ certain, >25s incurs an extra penalty |
| Album similarity | 0.05 | weak positive, often absent on YouTube |

Then subtractive **variant penalties** for tags present on one side only:
`live`, `acoustic`, `remix`, `instrumental`, `demo`, `sped up`, `slowed`,
`nightcore`, `mashup` (0.12 each, capped at 0.35), and a flat 0.30 for
`cover` / `karaoke` mismatch — the failure mode that most annoys users.

Buckets: `auto ≥ 0.85` · `needs_review 0.55–0.85` · `unmatched < 0.55`.

Weights are a starting point to be **tuned against a real fixture set**, which
is what phase 1 exists to produce.

### 3.3 Persistence (phase 2)

SQLite. The schema is what makes resume, sync and review all fall out for free.

| Table | Purpose |
| --- | --- |
| `match_cache` | canonical track → target ID, with `confidence` and `user_verified`. Repeated songs across playlists cost nothing forever. |
| `transfer_items` | one row per track per job with status. **The resume mechanism** — kill the process mid-transfer, restart, it continues. |
| `playlist_links` | source playlist ↔ target playlist. **The sync mechanism** — re-runs diff and append only what is new. |
| `oauth_tokens` | encrypted at rest. Single user for now, but a `user_id` column exists from day one so multi-user is a migration, not a rewrite. |

Review-screen corrections write back to `match_cache` with `user_verified=true`,
making them permanent and reusable.

**Sync is append-only.** If a track is added to the destination playlist by hand,
sync leaves it alone. Treating the source as absolute truth would delete a user's
own edits, which is unrecoverable; the reverse error merely leaves an extra
track.

## 4. Build order

All scope items land; the sequence front-loads the risky part.

| Phase | Contents |
| --- | --- |
| **1** ✅ | Provider interface, Spotify + YouTube **read** paths, matcher, CLI dry-run producing a match-quality report. No writes, no UI. |
| **2** ✅ | Writes + job engine. Resumable, quota-aware backoff, idempotent via `match_cache`. CLI `transfer` / `jobs` / `review`. |
| **3** | React UI — playlist picker, live job progress, **review screen**. |
| **4** | Reverse direction (YT → Spotify). Mostly free if the abstraction held. |
| **5** | Liked Songs + ongoing sync. |

**Phase 1 is the go/no-go.** If match rates on real playlists are poor,
everything downstream is wasted effort. Validate against at least one mainstream
playlist, one remix/electronic-heavy playlist, and whatever is most obscure in
the library.

## 4b. Phase 1 findings

Measured over **2,471 tracks across 8 playlists**, Spotify → YouTube Music:
**94.7% auto, 4.5% review, 0.9% absent**, with **zero false positives** (18 of
2,339 auto-matches had a weak component; all were verified correct artist
spelling variants). Reverse direction on Liked Songs: 35% auto, 50% absent —
structural, since half that sample is not music.

**Go.**

Six bugs that only live data exposed:

1. **Spotify returned a 303-track playlist as zero tracks.** The payload had
   moved to an undocumented `item` key; the converter dropped every row and
   reported success. Silent data loss — now pinned by fixtures for all three
   payload shapes.
2. **Search recall, not scoring, was the reverse-direction bottleneck.** Queries
   built from raw YouTube titles returned one wrong result; the same track with
   a cleaned query returned eight, correct one first. Hence `search_terms()`:
   searching wants the shortest unambiguous phrase, matching wants every
   qualifier. They are now separate code paths.
3. **YouTube titles repeat the artist** ("saybik - PAINKILLER"), pushing correct
   matches to ~0.64 title similarity. Stripped using *both* sides' artist names,
   since the uploader channel is frequently the wrong artist.
4. **Spotify no longer returns track counts** when listing playlists, so
   `track_count` is `int | None` and renders as `?` rather than a fabricated 0.
5. **Pagination truncates silently under load.** A 303-track playlist came back
   as 207 with no error while another job was running. Harmless in a dry run,
   dangerous once writing: the transfer would create an incomplete playlist and
   record it as fully synced, so the missing tracks would never be retried. Both
   providers now compare against the reported total and raise instead.
6. **Artist credits vary more than titles do.** Country suffixes (`ADDAM (BE)`),
   aliases (`Diddy`/`Puff Daddy`), partial names (`Ricardo Gi`/`Ricardo`) and
   differing feature credits held ~50 correct matches below the threshold.
   Artist scoring now grades each name on the smaller side against its best
   counterpart rather than requiring exact set membership.

**`absent` vs `low_confidence`.** Search engines rarely return nothing —
Spotify answers almost any query with unrelated filler — so "no candidates" is
far too strict a test for absence. A best score below `ABSENT_SCORE` (0.30)
means the track is not on the target platform at all. Conflating the two makes a
playlist that is half DJ sets look like a broken matcher.

**Thresholds are now validated.** The bimodal distribution seen in the first
40-track sample was an artifact of its size; at 2,471 tracks the review band
carries a healthy 4.5% and the 0.85 auto threshold produced no false positives.
Electronic music is the weakest genre (88.3%), where remix and edit credits vary
most between platforms.

## 4c. Phase 2 findings (first real write)

**Dancehall vibe → YouTube, 182 tracks.** 176 matched, 6 sent to review, 0
absent. Verified by reading the playlist back from YouTube: **176 tracks
present**, ~8,900 quota units consumed.

Three bugs the live write exposed, none of which the fakes could have:

1. **A single HTTP 409 ("The operation was aborted") dropped one track.** A
   concurrency conflict on YouTube's side, not a bad request. Transient statuses
   (409, 5xx) are now retried with backoff.
2. **The job was marked COMPLETED with a failed track.** It would have been
   retired silently and the playlist left one short forever. FAILED now counts
   as outstanding work, and a write failure is retryable — the re-run wrote
   exactly the one missing track and nothing else, confirming idempotency
   against a real account.
3. **176 source tracks produced only 172 distinct videos.** Distinct Spotify
   track ids collapse onto one YouTube upload — a single and an album cut, or a
   "(feat. X)" variant alongside the main entry. Writing each put the same video
   in the playlist up to three times. The first now wins; the rest are recorded
   as `SKIPPED`/`duplicate_target` rather than silently dropped.

The third is inherent to cross-platform mapping and will recur on any platform
pair, so dedupe belongs in the engine rather than in a provider.

**Reading a YouTube playlist was also the slowest operation in the system** —
180 tracks took over four minutes, because enrichment is one serial ytmusicapi
call per track. Fanned out over 6 workers: **67s**. The retry added at the same
time matters more than the speed: enrichment failure falls back to parsing the
video title, which substitutes an uploader channel for the artist, so a
throttled call would have quietly degraded match quality with no error. Only a
genuine absence of catalog data should reach that fallback.

## 4d. Exact matching by ISRC

An **ISRC identifies a recording**, so two tracks sharing one are the same
performance however their titles and artist credits happen to be written. Where
both platforms expose it, that is proof of identity rather than evidence toward
it, and fuzzy scoring can only second-guess it — so `match()` short-circuits:
score 1.0, bucket `auto`, no review.

Guards that keep this honest:

- **Validated, not merely compared.** A malformed value matching another
  malformed value (`"unknown"` on both sides) must never count as proof, so
  anything failing the ISRC format check is discarded.
- **Mismatch proves nothing.** A re-release can carry a different ISRC, so a
  disagreement falls through to fuzzy scoring rather than blocking the match.
- **Only the source's ISRC starts the path.** No ISRC on the source means the
  ordinary pipeline runs unchanged.

The engine also skips the fuzzy search entirely when a target declares
`supports_isrc_lookup`, replacing a search plus scoring with one exact lookup.

**This does nothing for the Spotify↔YouTube pair** — YouTube Music exposes no
ISRC — and was built as groundwork for platforms that do. Verified live against
Spotify: 7/7 tracks carried an ISRC and every lookup resolved the correct
recording at 1.0. Note that a lookup returns the right *recording*, not
necessarily the same release: 1 of 5 round-tripped to the identical Spotify
track id, the rest to a different release of the same audio. For playlist
transfer that is the desired behaviour.

## 4e. Adding platforms

| Platform | Verdict | Why |
| --- | --- | --- |
| **Apple Music** | Viable, gated | $99/yr Apple Developer Program; needs *two* tokens (a developer JWT plus a Music User Token). The user token comes only from MusicKit JS in a **browser** — unobtainable from a CLI, so the web UI must exist first. Requires an active subscription and a storefront (region), which the model currently lacks. Payoff: `filter[isrc]` catalog lookup, giving near-exact matching against Spotify. |
| **Tidal / Deezer** | Recommended next | Open developer registration, no fee, ISRC support. Proves the abstraction without Apple's gate. |
| **SoundCloud** | Avoid | Public API registration has been **closed since 2019** with no legitimate route to a `client_id`; the only workaround is scraping one from the web player, which is fragile and against their terms. Independently a poor fit: the catalogue is largely user-uploaded remixes, bootlegs and DJ sets, so a large share of it has no counterpart on any licensed service — the YouTube→Spotify failure mode, but structural and permanent. |

Remaining provider-contract gaps for a new platform: no region/storefront
concept, and auth is file-cached per provider with no abstraction for
browser-based flows.

## 4f. Distribution

A **free public webapp is not viable on these APIs**, and the arithmetic is the
reason, not ambition:

- YouTube's 10,000 units/day is **per application, not per user** — about 198
  track-writes per day across the entire userbase.
- Spotify apps in development mode are capped at **25 users**; extended quota
  requires a real organization.
- Raising either means a compliance audit, which an app writing to user accounts
  via an unofficial client is poorly placed to pass.

Self-hosted open source inverts this: each user brings their own credentials and
their own quota, which is demonstrably sufficient for one person. A hosted
**dry-run demo** is safe to operate, since matching consumes no YouTube quota at
all.

## 5. Deliberately deferred

Multi-user auth, hosted deployment, Apple Music/Tidal providers, playlist cover
art, collaborative-playlist semantics, and Spotify quota extension.

## 6. Risk register

| Risk | Mitigation |
| --- | --- |
| `ytmusicapi` breaks on a YouTube change | Isolated behind `providers/youtube.py`; official API can serve search at reduced throughput |
| YouTube write quota exhausted | `YT_WRITE_MODE` flag; job engine resumes next day |
| Google testing-mode token expiry (7 days) | Cheap re-auth command; detect `invalid_grant` and prompt |
| Poor match rate on obscure catalog | Review UI + persisted user corrections |
| ToS ambiguity around unofficial client | Personal use, unauthenticated search only, swappable provider |
