"""Environment-backed configuration. Fails loudly rather than half-working."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path(env_key: str, default: str) -> Path:
    raw = os.getenv(env_key, default)
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


@dataclass(frozen=True)
class Config:
    spotify_client_id: str | None
    spotify_client_secret: str | None
    spotify_redirect_uri: str
    google_client_secrets_file: Path
    yt_write_mode: str
    data_dir: Path

    @property
    def spotify_token_cache(self) -> Path:
        return self.data_dir / "spotify_token.json"

    @property
    def google_token_cache(self) -> Path:
        return self.data_dir / "google_token.json"

    def require_spotify(self) -> None:
        if not self.spotify_client_id or not self.spotify_client_secret:
            raise SystemExit(
                "Spotify credentials missing. Copy .env.example to .env and set "
                "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
            )
        if "localhost" in self.spotify_redirect_uri:
            raise SystemExit(
                "SPOTIFY_REDIRECT_URI uses 'localhost'. Spotify rejects it — "
                "use http://127.0.0.1:8888/callback instead."
            )

    def require_google(self) -> None:
        if not self.google_client_secrets_file.exists():
            raise SystemExit(
                f"Google client secrets not found at {self.google_client_secrets_file}.\n"
                "Create a 'Desktop app' OAuth client in Google Cloud Console, enable the "
                "YouTube Data API v3, download the JSON, and place it there."
            )


def load_config() -> Config:
    cfg = Config(
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        spotify_redirect_uri=os.getenv(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
        ),
        google_client_secrets_file=_path(
            "GOOGLE_CLIENT_SECRETS_FILE", "./.data/google_client_secret.json"
        ),
        yt_write_mode=os.getenv("YT_WRITE_MODE", "official"),
        data_dir=_path("DATA_DIR", "./.data"),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
