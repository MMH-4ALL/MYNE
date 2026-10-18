"""Application use cases, independent of terminal presentation."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from myne.config import Config
from myne.container import create_registry
from myne.errors import MyneError, PlaybackError, ProviderError
from myne.lyrics import LyricsDocument, LyricsService
from myne.mix import AIMixStrategy, MixService
from myne.models import Album, Artist, Track
from myne.playback.client import PlayerClient
from myne.plugins import PluginRegistry
from myne.providers.base import MusicProvider
from myne.storage import Store


class Application:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        registry: PluginRegistry | None = None,
        player: PlayerClient | None = None,
        lyrics_service: LyricsService | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.database)
        self.registry = registry or create_registry(config)
        self.player = player or PlayerClient(config)
        self._live_player = player or PlayerClient(config, timeout=2.0)
        self._lyrics = lyrics_service or LyricsService(self.registry.provider, config.data_dir)

    @property
    def provider(self) -> MusicProvider:
        return self.registry.provider(self.config.provider)

    def search(self, query: str, limit: int = 20) -> list[Track]:
        if not query.strip():
            raise MyneError("Enter a search query.")
        return self.provider.search(query.strip(), limit)

    def resolve(self, query: str) -> Track:
        try:
            local = Path(query).expanduser()
            if local.is_file():
                return self.registry.provider("local").get_song(str(local.resolve()))
        except OSError:
            pass  # Long search text need not be a valid filesystem component.
        parsed = urlparse(query)
        host = (parsed.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
            song_id = (
                parsed.path.strip("/")
                if host.endswith("youtu.be")
                else parse_qs(parsed.query).get("v", [""])[0]
            )
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", song_id):
                raise MyneError("YouTube URL must identify a single song with a video ID.")
            return self.registry.provider("youtube").get_song(song_id)
        if parsed.scheme in {"http", "https"}:
            raise MyneError("Provide a YouTube song URL, a local file, or a search query.")
        if self.config.provider == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", query):
            try:
                return self.provider.get_song(query)
            except ProviderError:
                pass  # An eleven-character song title may resemble a video ID.
        tracks = self.search(query, 1)
        if not tracks:
            raise MyneError(f"No tracks found for {query!r}.")
        return tracks[0]

    def album(self, album_id: str) -> Album:
        return self.provider.get_album(album_id)

    def artist(self, artist_id: str) -> Artist:
        return self.provider.get_artist(artist_id)

    def play(self, tracks: list[Track]) -> dict[str, Any]:
        if not tracks:
            raise MyneError("There are no playable tracks.")
        self.player.ensure_running()
        return self.player.request("play", tracks=[t.to_dict() for t in tracks])

    def enqueue(self, tracks: list[Track]) -> dict[str, Any]:
        # Adding music should work without mpv or an active player.
        self.store.enqueue(tracks)
        return {"added": len(tracks)}

    def queue(self) -> list[Track]:
        return self.store.queue()

    def now(self) -> dict[str, Any]:
        try:
            return self._live_player.request("now")
        except PlaybackError as exc:
            return {
                "track": None,
                "paused": False,
                "playing": False,
                "position": 0.0,
                "duration": None,
                "volume": self.config.volume,
                "radio": False,
                "error": str(exc) if self.config.socket_path.exists() else None,
            }

    def control(self, action: str, **payload: Any) -> dict[str, Any]:
        if action == "resume":
            self.player.ensure_running()
        return self.player.request(action, **payload)

    def lyrics(self) -> str | None:
        document = self.lyrics_document()
        if document is None:
            return None
        return "Instrumental · no sung lyrics." if document.instrumental else document.plain

    def lyrics_document(self, track: Track | None = None, refresh: bool = False) -> LyricsDocument | None:
        if track is None:
            data = self.now().get("track")
            if data:
                track = Track.from_dict(data)
        if track is None:
            raise MyneError("Nothing is playing. Start a song with myne play first.")
        return self._lyrics.get(track, refresh=refresh)

    def visualizer(self, enabled: bool = True) -> dict[str, Any]:
        try:
            return self._live_player.request("visualizer", enabled=enabled)
        except PlaybackError as exc:
            return {"available": False, "rms": [], "peak": [], "error": str(exc)}

    def audio_levels(self) -> dict[str, Any]:
        try:
            return self._live_player.request("levels")
        except PlaybackError as exc:
            return {
                "available": False,
                "rms": [],
                "peak": [],
                "playing": False,
                "paused": False,
                "error": str(exc),
            }

    def mix(self, mood: str, limit: int = 20, ai: bool = False, strategy: str | None = None) -> list[Track]:
        if ai and strategy:
            raise MyneError("Choose either --ai or --strategy for this mix.")
        selected = (
            self.registry.mix(strategy) if strategy else AIMixStrategy.from_environment() if ai else None
        )
        if ai and selected is None:
            raise MyneError(
                "Set MYNE_MIX_ENDPOINT (and optionally MYNE_MIX_API_KEY/MYNE_MIX_MODEL) for AI mixes."
            )
        return MixService(self.provider, selected).generate(mood, limit)

    def radio(self, query: str) -> dict[str, Any]:
        seed = self.resolve(query)
        self.player.ensure_running()
        return self.player.request("radio", seed=seed.to_dict())

    def stats(self) -> dict[str, Any]:
        return self.store.stats()
