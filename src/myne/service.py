"""Application use cases, independent of terminal presentation."""

import re
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import parse_qs, urlparse

from myne.config import Config
from myne.container import create_registry
from myne.errors import MyneError, PlaybackError, ProviderError
from myne.lyrics import LyricsDocument, LyricsService
from myne.mix import AIMixStrategy, MixService
from myne.models import Album, Artist, Playlist, Track
from myne.playback.client import PlayerClient
from myne.playlists import ImportResult, PlaylistImporter, Progress
from myne.plugins import PluginRegistry
from myne.preferences import Preferences
from myne.providers.base import MusicProvider
from myne.storage import Store
from myne.updates import UpdateInfo, UpdateManager


class Application:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        registry: PluginRegistry | None = None,
        player: PlayerClient | None = None,
        lyrics_service: LyricsService | None = None,
        playlist_importer: PlaylistImporter | None = None,
        updater: UpdateManager | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.database)
        self.registry = registry or create_registry(config)
        self.player = player or PlayerClient(config)
        self._live_player = player or PlayerClient(config, timeout=2.0)
        self._lyrics = lyrics_service or LyricsService(self.registry.provider, config.data_dir)
        self._playlist_importer = playlist_importer
        self._updater = updater

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

    def play_queued(self, index: int, expected: Track) -> dict[str, Any]:
        self.player.ensure_running()
        return self.player.request("play_queued", index=index, expected=expected.to_dict())

    def queue(self) -> list[Track]:
        return self.store.queue()

    def queue_remove(self, index: int, expected: Track) -> dict[str, Any]:
        self.store.remove(index, expected)
        return {"removed": 1}

    def queue_move(self, index: int, step: int, expected: Track) -> dict[str, Any]:
        return {"queue_index": self.store.move(index, step, expected)}

    def queue_shuffle(self) -> dict[str, Any]:
        return {"shuffled": self.store.shuffle_queue()}

    def favorites(self) -> list[Track]:
        return self.store.favorites()

    def recent_tracks(self) -> list[Track]:
        return self.store.recent_tracks()

    def toggle_favorite(self, track: Track) -> bool:
        return self.store.toggle_favorite(track)

    def playlists(self) -> list[tuple[str, int]]:
        return self.store.playlists()

    def playlist(self, name: str) -> Playlist:
        return self.store.playlist(name)

    def import_playlist(self, link: str, name: str | None = None, *, limit: int = 500,
                        progress: Progress | None = None, cancel: Event | None = None) -> ImportResult:
        if name is not None and (not name.strip() or len(name.strip()) > 80):
            raise MyneError("Give the playlist a name between 1 and 80 characters.")
        importer = self._playlist_importer or PlaylistImporter(
            self.registry.provider("youtube"), cookies_file=self.config.cookies_file
        )
        result = importer.read(link, limit=limit, progress=progress, cancel=cancel)
        if cancel is not None and cancel.is_set():
            raise MyneError("Playlist import cancelled. Nothing was saved.")
        from dataclasses import replace
        playlist = replace(result.playlist, name=name.strip()) if name is not None else result.playlist
        return replace(result, playlist=self.store.import_playlist(playlist))

    def save_playlist(self, name: str) -> int:
        if not name.strip() or len(name) > 80:
            raise MyneError("Give the saved queue a name between 1 and 80 characters.")
        return self.store.save_queue(name.strip())

    def restore_playlist(self, name: str) -> dict[str, Any]:
        return {"restored": self.store.restore_queue(name)}

    def ui_theme(self) -> str:
        value = self.store.get_setting("ui_theme", "myne-studio")
        return value if isinstance(value, str) else "myne-studio"

    def save_ui_theme(self, name: str) -> None:
        Preferences.from_dict({"theme": name}, strict=True)
        self.store.set_setting("ui_theme", name)

    def preferences(self) -> Preferences:
        values = Preferences(volume=self.config.volume).to_dict()
        saved = self.store.get_setting("preferences", {})
        if isinstance(saved, dict):
            values.update({key: value for key, value in saved.items() if key in values})
        values["theme"] = self.store.get_setting("ui_theme", values["theme"])
        values["volume"] = self.store.get_setting("volume", values["volume"])
        return Preferences.from_dict(values)

    def save_preferences(self, values: dict[str, Any]) -> Preferences:
        preferences = Preferences.from_dict(values, strict=True)
        self.store.set_settings({"preferences": preferences.to_dict(), "ui_theme": preferences.theme,
                                 "volume": preferences.volume})
        # Saving settings must never start mpv. A reachable running player can
        # use the new volume immediately; otherwise the next start reads it.
        if self.config.socket_path.exists():
            try:
                self._live_player.request("volume", value=preferences.volume)
            except PlaybackError:
                pass
        return preferences

    @property
    def updates(self) -> UpdateManager:
        if self._updater is None:
            self._updater = UpdateManager(self.store)
        return self._updater

    def update_status(self) -> UpdateInfo:
        return self.updates.status()

    def check_update(self, force: bool = True) -> UpdateInfo:
        return self.updates.check(force=force)

    def install_update(self, progress: Any = None) -> UpdateInfo:
        return self.updates.install(progress)

    def now(self) -> dict[str, Any]:
        try:
            return self._live_player.request("now")
        except PlaybackError as exc:
            volume = self.store.get_setting("volume", self.config.volume)
            if type(volume) is not int or not 0 <= volume <= 100:
                volume = self.config.volume
            return {
                "track": None,
                "paused": False,
                "playing": False,
                "position": 0.0,
                "duration": None,
                "volume": volume,
                "muted": False,
                "audio_output": None,
                "audio_device": None,
                "radio": False,
                "repeat": self.store.get_setting("repeat", "off"),
                "error": str(exc) if self.config.socket_path.exists() else None,
            }

    def control(self, action: str, **payload: Any) -> dict[str, Any]:
        if action in {"resume", "volume", "mute", "repeat"}:
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
