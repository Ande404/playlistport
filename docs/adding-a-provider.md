# Adding a platform

A platform is one file in `src/playlist_tool/providers/`. The matcher, the job
engine and the CLI need no changes — if you find yourself editing them to add a
platform, the abstraction has leaked and that is worth raising in an issue.

## The contract

```python
class MusicProvider(ABC):
    name: str                      # stored beside every mapped ID; keep it stable
    write_batch_size: int = 50     # tracks per write request
    supports_isrc_lookup: bool = False

    def list_playlists() -> list[PlaylistRef]
    def get_tracks(playlist_id) -> list[CanonicalTrack]
    def get_saved_tracks() -> list[CanonicalTrack]
    def search(track, limit=8) -> list[Candidate]
    def create_playlist(name, description) -> str
    def add_tracks(playlist_id, track_ids) -> None

    def lookup_by_isrc(isrc) -> Candidate | None   # optional
```

Register it in `providers/__init__.py:get_provider()`.

## Rules that matter

**Translate into `CanonicalTrack` on the way in.** Nothing outside your file may
learn that your platform exists. Populate `isrc` if the platform exposes it —
it is the only exact identity signal available, and it lets the matcher skip
fuzzy scoring entirely.

**Never return a short read.** If pagination is interrupted, raise; do not
return what you have. This is not hypothetical: Spotify silently ended
pagination early under load and a 303-track playlist came back as 207 with no
error. In a dry run that is a wrong number, but once writing is involved the
transfer creates an incomplete playlist *and records it as fully synced*, so the
missing tracks are never retried. Compare against the platform's reported total
and raise on a mismatch.

**Skip unusable items explicitly.** Local files, podcast episodes, deleted and
private videos: drop them, and let the engine report them. Never let them reach
the matcher.

**Retry transient failures, raise permanent ones.** Throttling frequently
arrives as a malformed body or an HTTP 409 rather than a clean 429 — retry those
with backoff. Raise `QuotaExceeded` for a hard daily ceiling so the engine can
pause and resume tomorrow instead of failing the job.

**Set `write_batch_size` honestly.** Spotify accepts 100 URIs per request;
YouTube has no batch insert, so it uses 1 and the engine checkpoints after each
write. Getting this wrong costs durability, not just speed.

**Beware degrading silently.** If metadata enrichment fails and you fall back to
parsing a title, you may substitute an uploader channel for an artist and quietly
wreck match quality with no error. Retry first; reach the fallback only when the
data genuinely is not there.

## Search vs. match

These want opposite things and must not share a code path:

- **`search()`** should send the *shortest unambiguous* query. Use
  `normalize.search_terms()`, which strips the artist prefix, release noise and
  every bracketed aside. A real query containing `( freestyle audio )` returned
  one wrong result; the cleaned form returned eight with the correct track first.
- **The matcher** needs every qualifier intact, so it can penalise a live take
  or a karaoke version. Return candidates with their titles unmodified.

## Testing

Write tests that need no network. `tests/test_jobs.py` has `FakeSource` and
`FakeTarget` to copy; `tests/test_spotify_items.py` shows how to test payload
parsing by bypassing `__init__` with `Provider.__new__(Provider)`.

Encode the failure modes you actually hit, not happy paths. The most valuable
tests in this repo all came from bugs found by running against real data:

- a 303-track playlist read as 0 because the payload moved to an undocumented key
- a live recording nearly auto-matched to a studio cut
- a job marked complete while a track had failed to write
- one video written to a playlist three times

## Platform notes

| Platform | Status |
| --- | --- |
| **Tidal / Deezer** | Best next targets. Open registration, no fee, ISRC support. |
| **Apple Music** | Requires the $99/yr Apple Developer Program, *and* a Music User Token obtainable only through MusicKit JS in a browser — there is no CLI path, so a web UI is a hard prerequisite. |
| **SoundCloud** | Not recommended. Public API registration has been closed since 2019 with no legitimate route to a `client_id`. Independently a poor match target: the catalogue is largely user-uploaded remixes and DJ sets with no counterpart on licensed services. |

Two gaps to expect if you add a platform that needs them: there is no
region/storefront concept, and auth is file-cached per provider with no
abstraction for browser-based flows.
