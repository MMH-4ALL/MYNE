"""YouTube Music metadata and just-in-time, download-free stream extraction."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from myne.errors import ProviderError
from myne.models import Album, Artist, Playlist, Stream, Track
from myne.providers.base import MusicProvider


class _QuietLogger:
    """yt-dlp diagnostics can contain signed URLs and authentication material."""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        return _name(value.get("name") or value.get("title"))
    return None


def _artists(data: Mapping[str, Any]) -> tuple[str, ...]:
    values = data.get("artists") or data.get("artist") or data.get("author") or ()
    if isinstance(values, (str, Mapping)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(name for value in values if (name := _name(value))))


def _duration(data: Mapping[str, Any]) -> float | None:
    value = data.get("duration_seconds", data.get("lengthSeconds"))
    if value is None:
        value = data.get("duration", data.get("length"))
    try:
        if isinstance(value, str) and ":" in value:
            parts = value.split(":")
            if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
                return None
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + int(part)
        else:
            seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError):
        return None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise ProviderError("A valid YouTube Music identifier is required.")
    return value


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ProviderError("The result limit must be between 1 and 100.")
    return value


class YouTubeMusicProvider(MusicProvider):
    """Inject clients in tests; runtime dependencies are imported only when used.

    Stream URLs are deliberately never cached or persisted: YouTube signs them
    with short expiration times. Only ``resolve_stream`` invokes yt-dlp.
    """

    name = "youtube"

    def __init__(
        self,
        auth_file: str | None = None,
        cookies_file: str | None = None,
        client: Any = None,
        extractor_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.auth_file = auth_file
        self.cookies_file = cookies_file
        self._client = client
        self._extractor_factory = extractor_factory

    def _metadata(self) -> Any:
        if self._client is None:
            try:
                from ytmusicapi import YTMusic

                self._client = YTMusic(auth=self.auth_file)
            except ImportError:
                raise ProviderError("Install ytmusicapi to use YouTube Music.") from None
            except Exception:
                raise ProviderError("YouTube Music setup failed. Check your authentication file.") from None
        return self._client

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._metadata(), method)(*args, **kwargs)
        except ProviderError:
            raise
        except Exception:
            # Do not echo upstream exceptions: these may include credentials/URLs.
            raise ProviderError(
                "YouTube Music could not complete the request. Check your connection "
                "and authentication, then retry."
            ) from None

    def _track(self, data: Any, *, album: str | None = None) -> Track | None:
        if not isinstance(data, Mapping) or data.get("isAvailable") is False:
            return None
        identifier = data.get("videoId")
        title = _name(data.get("title"))
        if not isinstance(identifier, str) or not title:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
            return None
        return Track(
            id=identifier,
            title=title,
            artists=_artists(data),
            duration=_duration(data),
            album=_name(data.get("album")) or album,
            provider=self.name,
            genres=tuple(g for g in data.get("genres", ()) if isinstance(g, str))
            if isinstance(data.get("genres", ()), (list, tuple))
            else (),
        )

    def _tracks(self, rows: Any, *, album: str | None = None) -> list[Track]:
        if not isinstance(rows, (list, tuple)):
            return []
        seen: set[str] = set()
        tracks: list[Track] = []
        for row in rows:
            track = self._track(row, album=album)
            if track is not None and track.id not in seen:
                seen.add(track.id)
                tracks.append(track)
        return tracks

    def search(self, query: str, limit: int = 20) -> list[Track]:
        _limit(limit)
        if not query.strip() or len(query) > 1000:
            raise ProviderError("Enter a search query between 1 and 1000 characters.")
        rows = self._call("search", query.strip(), filter="songs", limit=limit)
        return self._tracks(rows)[:limit]

    def get_song(self, song_id: str) -> Track:
        song_id = _identifier(song_id)
        result = self._call("get_song", song_id)
        details = result.get("videoDetails", {}) if isinstance(result, Mapping) else {}
        track = self._track(details)
        if track is None:
            raise ProviderError("This song is unavailable or has no playable metadata.")
        return track

    def get_playlist(self, playlist_id: str, limit: int = 500) -> Playlist:
        playlist_id = _identifier(playlist_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ProviderError("Import between 1 and 500 playlist tracks at a time.")
        result = self._call("get_playlist", playlist_id, limit=limit + 1)
        if not isinstance(result, Mapping) or not (title := _name(result.get("title"))):
            raise ProviderError("This YouTube playlist is unavailable.")
        rows = result.get("tracks")
        if not isinstance(rows, list):
            raise ProviderError("This YouTube playlist did not expose its tracks.")
        # A playlist can intentionally repeat a song; _tracks deduplicates
        # search results, so map playlist rows individually instead.
        tracks = tuple(track for row in rows[:limit] if (track := self._track(row)) is not None)
        warnings = []
        total = result.get("trackCount")
        if len(rows) > limit or (isinstance(total, int) and total > limit):
            warnings.append(f"Import limited to the first {limit} entries.")
        if len(tracks) < min(len(rows), limit):
            warnings.append("Private, removed, or unavailable entries were omitted by the source.")
        return Playlist(title, tracks, "YouTube Music", warnings=tuple(warnings))

    def get_album(self, album_id: str) -> Album:
        album_id = _identifier(album_id)
        result = self._call("get_album", album_id)
        if not isinstance(result, Mapping) or not (title := _name(result.get("title"))):
            raise ProviderError("This album is unavailable.")
        artists = _artists(result)
        tracks = tuple(
            track if track.artists else replace(track, artists=artists)
            for track in self._tracks(result.get("tracks"), album=title)
        )
        return Album(
            id=album_id,
            title=title,
            artists=artists,
            tracks=tracks,
            provider=self.name,
        )

    def get_artist(self, artist_id: str) -> Artist:
        artist_id = _identifier(artist_id)
        result = self._call("get_artist", artist_id)
        if not isinstance(result, Mapping) or not (name := _name(result.get("name"))):
            raise ProviderError("This artist is unavailable.")
        songs = result.get("songs") or {}
        albums = result.get("albums") or {}
        album_rows = albums.get("results", []) if isinstance(albums, Mapping) else []
        releases: list[Album] = []
        for row in album_rows:
            if not isinstance(row, Mapping):
                continue
            album_id, title = row.get("browseId"), _name(row.get("title"))
            if isinstance(album_id, str) and title:
                releases.append(Album(album_id, title, (name,), provider=self.name))
        top_tracks = self._tracks(songs.get("results")) if isinstance(songs, Mapping) else []
        return Artist(
            id=artist_id,
            name=name,
            top_tracks=tuple(
                track if track.artists else replace(track, artists=(name,)) for track in top_tracks
            ),
            albums=tuple(releases),
            provider=self.name,
        )

    def recommendations(self, track: Track, limit: int = 25) -> list[Track]:
        _limit(limit)
        result = self._call("get_watch_playlist", videoId=_identifier(track.id), radio=True, limit=limit + 1)
        rows = result.get("tracks") if isinstance(result, Mapping) else None
        return [item for item in self._tracks(rows) if item.id != track.id][:limit]

    def lyrics(self, track: Track) -> str | None:
        watch = self._call("get_watch_playlist", videoId=_identifier(track.id), limit=1)
        browse_id = watch.get("lyrics") if isinstance(watch, Mapping) else None
        if not isinstance(browse_id, str) or not browse_id:
            return None
        result = self._call("get_lyrics", browse_id)
        text = result.get("lyrics") if isinstance(result, Mapping) else None
        if isinstance(text, str):
            return text.strip() or None
        return None

    def resolve_stream(self, track: Track) -> Stream:
        if track.provider != self.name:
            raise ProviderError("This track belongs to another music provider.")
        identifier = _identifier(track.id)
        factory = self._extractor_factory
        if factory is None:
            try:
                from yt_dlp import YoutubeDL

                factory = YoutubeDL
            except ImportError:
                raise ProviderError("Install yt-dlp to stream YouTube Music.") from None
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _QuietLogger(),
            "socket_timeout": 20,
            "retries": 2,
            "cachedir": False,
            "js_runtimes": {"deno": {}, "node": {}},
        }
        if self.cookies_file:
            options["cookiefile"] = self.cookies_file
        try:
            with ExitStack() as stack:
                extractor = factory(options)
                if hasattr(extractor, "__enter__"):
                    extractor = stack.enter_context(extractor)
                elif callable(getattr(extractor, "close", None)):
                    stack.callback(extractor.close)
                info = extractor.extract_info(
                    f"https://music.youtube.com/watch?v={identifier}",
                    download=False,
                )
            if not isinstance(info, Mapping):
                raise ValueError("No stream metadata")
            url = info.get("url")
            if not isinstance(url, str) or urlsplit(url).scheme not in ("https", "http"):
                raise ValueError("No playable stream")
            raw_headers = info.get("http_headers") or {}
            headers = (
                {
                    key: value
                    for key, value in raw_headers.items()
                    if isinstance(key, str)
                    and isinstance(value, str)
                    and not any(c in key + value for c in "\r\n")
                }
                if isinstance(raw_headers, Mapping)
                else {}
            )
            return Stream(url=url, headers=headers)
        except Exception:
            raise ProviderError(
                "Could not resolve an audio stream. Update yt-dlp and check your "
                "connection or configured YouTube cookies."
            ) from None
