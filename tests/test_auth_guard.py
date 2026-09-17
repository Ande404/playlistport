"""Authorization must fail loudly when nobody can complete it.

An expired Google refresh token sent a scheduled run into
`InstalledAppFlow.run_local_server()`, which blocks until a browser callback
arrives. Headless that is not an error but an indefinite hang: the process ran
for 20 hours holding port 8080, and launchd would not start another copy behind
it, so three days of scheduled transfers silently did not happen.

These tests pin the guard. They must never block — a regression here would hang
the suite, which is itself the signal.
"""

import pytest

from playlistport.config import interactive_session
from playlistport.providers.base import AuthRequired


class TestInteractiveDetection:
    def test_env_var_forces_true(self, monkeypatch):
        monkeypatch.setenv("PLAYLISTPORT_INTERACTIVE", "1")
        assert interactive_session() is True

    def test_env_var_forces_false(self, monkeypatch):
        monkeypatch.setenv("PLAYLISTPORT_INTERACTIVE", "0")
        assert interactive_session() is False

    def test_non_tty_is_not_interactive(self, monkeypatch):
        monkeypatch.delenv("PLAYLISTPORT_INTERACTIVE", raising=False)

        class NotATty:
            def isatty(self):
                return False

        monkeypatch.setattr("sys.stdin", NotATty())
        monkeypatch.setattr("sys.stdout", NotATty())
        assert interactive_session() is False

    def test_detection_survives_a_detached_stdin(self, monkeypatch):
        # Under launchd stdin can be closed entirely; isatty() then raises.
        monkeypatch.delenv("PLAYLISTPORT_INTERACTIVE", raising=False)

        class Closed:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr("sys.stdin", Closed())
        assert interactive_session() is False


class TestYouTubeAuthGuard:
    def test_expired_credentials_raise_instead_of_opening_a_browser(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PLAYLISTPORT_INTERACTIVE", "0")
        secrets = tmp_path / "google_client_secret.json"
        secrets.write_text('{"installed": {"client_id": "x"}}')
        monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_FILE", str(secrets))

        from playlistport.providers.youtube import YouTubeProvider

        provider = YouTubeProvider()
        # No cached token at all: the old code would have started a browser
        # flow and blocked here forever.
        with pytest.raises(AuthRequired, match="no terminal"):
            provider._credentials()

    def test_error_names_the_fix_and_the_root_cause(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PLAYLISTPORT_INTERACTIVE", "0")
        secrets = tmp_path / "google_client_secret.json"
        secrets.write_text('{"installed": {"client_id": "x"}}')
        monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_FILE", str(secrets))

        from playlistport.providers.youtube import YouTubeProvider

        with pytest.raises(AuthRequired) as excinfo:
            YouTubeProvider()._credentials()
        message = str(excinfo.value)
        assert "auth youtube" in message          # what to run
        assert "consent screen" in message        # why it keeps happening


class TestSpotifyAuthGuard:
    def test_missing_cache_raises_when_headless(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PLAYLISTPORT_INTERACTIVE", "0")
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "id")
        monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "secret")

        from playlistport.providers.spotify import SpotifyProvider

        with pytest.raises(AuthRequired, match="no terminal"):
            _ = SpotifyProvider().client
