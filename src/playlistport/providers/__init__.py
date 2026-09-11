from .base import MusicProvider, ProviderError

__all__ = ["MusicProvider", "ProviderError", "get_provider"]


def get_provider(name: str) -> MusicProvider:
    """Resolve a provider by name. The only place callers name a platform."""
    key = name.strip().lower()
    if key in {"spotify", "sp"}:
        from .spotify import SpotifyProvider

        return SpotifyProvider()
    if key in {"youtube", "yt", "ytmusic", "youtube-music"}:
        from .youtube import YouTubeProvider

        return YouTubeProvider()
    raise ProviderError(f"Unknown provider {name!r}. Expected 'spotify' or 'youtube'.")
