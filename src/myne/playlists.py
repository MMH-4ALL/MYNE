"""Read shared playlist metadata and match songs to MYNE's playback provider.

No Spotify audio, preview URLs, credentials, or expiring streams are saved.
The public Spotify embed is a best-effort view, not a complete-playlist API.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from html.parser import HTMLParser
from itertools import islice
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from myne.errors import MyneError, ProviderError
from myne.models import Playlist, Track
from myne.providers.base import MusicProvider
from myne.providers.youtube import _QuietLogger

MAX_TRACKS = 500
Progress = Callable[[int, int, str], None]


def playlist_link(value: str) -> tuple[str, str, str]:
    """Return a source, ID and canonical URL, rejecting unrelated web links."""
    value = value.strip()
    if re.fullmatch(r"spotify:playlist:[A-Za-z0-9]{22}", value):
        value = "https://open.spotify.com/playlist/" + value.rsplit(":", 1)[1]
    if len(value) > 2048 or re.search(r"[\x00-\x20\x7f]", value):
        raise MyneError("Paste a complete playlist link without spaces.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
    except ValueError:
        raise MyneError("Use an HTTPS playlist link from a supported music platform.") from None
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if host == "open.spotify.com":
        match = re.fullmatch(r"/(?:intl-[a-zA-Z-]+/|embed/)?playlist/([A-Za-z0-9]{22})", path)
        if match:
            identifier = match[1]
            return "Spotify", identifier, f"https://open.spotify.com/playlist/{identifier}"
    if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}:
        identifiers = parse_qs(parsed.query).get("list", [])
        if len(identifiers) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{2,128}", identifiers[0]):
            identifier = identifiers[0]
            source = "YouTube Music" if host == "music.youtube.com" else "YouTube"
            return source, identifier, f"https://www.youtube.com/playlist?list={identifier}"
    if host in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}:
        if re.fullmatch(r"/[A-Za-z0-9_-]+/sets/[A-Za-z0-9_-]+", path):
            return "SoundCloud", path, "https://soundcloud.com" + path
    raise MyneError(
        "Use a Spotify playlist, YouTube / YouTube Music playlist, or SoundCloud set link. "
        "For short share links, open the link and copy the full playlist address."
    )


class _SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname or new.port not in (None, 443):
            raise ProviderError("The playlist service redirected to an unexpected address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_url(url: str, headers: dict[str, str]) -> bytes:
    try:
        opener = build_opener(_SameHostRedirect())
        with opener.open(Request(url, headers={"User-Agent": "Mozilla/5.0 MYNE", **headers}), timeout=20) as response:
            data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ProviderError("Playlist metadata is too large. Try a smaller playlist.")
        return data
    except HTTPError as exc:
        if exc.code == 429:
            raise ProviderError("The playlist service is busy. Wait a little before importing again.") from None
        if exc.code in (401, 403):
            raise ProviderError(
                "The playlist is private or access was refused. Use a public playlist, or check "
                "MYNE_SPOTIFY_TOKEN and your access to the playlist."
            ) from None
        raise ProviderError("The playlist could not be opened. Check the link and try again.") from None
    except (URLError, OSError, ValueError):
        raise ProviderError("Could not reach the playlist service. Check your connection and retry.") from None


class _EmbedData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)


def _duration(value: Any, divisor: float = 1.0) -> float | None:
    try:
        result = float(value) / divisor
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError):
        return None


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\b(official|audio|video|lyrics|lyric|remastered|remaster|hd)\b", " ", value)
    return " ".join(re.findall(r"[^\W_]+", value))


def match_track(source: Track, candidates: list[Track]) -> Track | None:
    """Reject weak title/artist matches and obviously different recordings."""
    best: Track | None = None
    best_score = 0.0
    title = _normal(source.title)
    if not title:
        return None
    for candidate in candidates:
        if candidate.provider != "youtube":
            continue
        candidate_title = _normal(candidate.title)
        title_score = SequenceMatcher(None, title, candidate_title).ratio()
        if candidate.artists:
            core = candidate_title.removeprefix(_normal(candidate.artists[0]) + " ")
            title_score = max(title_score, SequenceMatcher(None, title, core).ratio())
        if title_score < 0.78:
            continue
        # Avoid substituting covers, live versions or remixes for studio songs.
        versions = {"live", "cover", "remix", "instrumental", "karaoke", "sped", "slowed"}
        if (set(candidate_title.split()) & versions) - (set(title.split()) & versions):
            continue
        artist_score = 1.0
        if source.artists:
            expected = _normal(source.artists[0])
            actual = _normal(" ".join(candidate.artists))
            artist_score = SequenceMatcher(None, expected, actual).ratio()
            if expected and expected in actual:
                artist_score = 1.0
            if artist_score < 0.55:
                continue
        if source.duration and candidate.duration:
            if abs(source.duration - candidate.duration) > max(20, source.duration * 0.15):
                continue
        score = title_score * 0.75 + artist_score * 0.25
        if score >= 0.82 and score > best_score:
            best, best_score = candidate, score
    return best


@dataclass(frozen=True, slots=True)
class ImportResult:
    playlist: Playlist
    scanned: int
    skipped: tuple[str, ...] = ()


class PlaylistImporter:
    def __init__(self, youtube: MusicProvider, *, cookies_file: str | None = None,
                 fetch: Callable[[str, dict[str, str]], bytes] = _read_url,
                 extractor_factory: Callable[[dict[str, Any]], Any] | None = None,
                 spotify_token: str | None = None) -> None:
        self.youtube = youtube
        self.cookies_file = cookies_file
        self.fetch = fetch
        self.extractor_factory = extractor_factory
        self.spotify_token = spotify_token if spotify_token is not None else os.environ.get("MYNE_SPOTIFY_TOKEN")

    def read(self, link: str, *, limit: int = MAX_TRACKS, progress: Progress | None = None,
             cancel: Event | None = None) -> ImportResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TRACKS:
            raise MyneError(f"Import between 1 and {MAX_TRACKS} tracks at a time.")
        source, identifier, url = playlist_link(link)
        self._check_cancel(cancel)
        if progress:
            progress(0, 0, f"Reading {source} playlist…")
        try:
            if source == "Spotify":
                playlist = self._spotify(identifier, limit, cancel)
            elif source.startswith("YouTube"):
                try:
                    playlist = replace(self.youtube.get_playlist(identifier, limit), source=source)
                except ProviderError:
                    playlist = self._extract(url, source, limit, cancel)
            else:
                playlist = self._extract(url, source, limit, cancel)
        except MyneError:
            raise
        except Exception:
            raise ProviderError("Playlist metadata could not be read. The service may have changed; try again later.") from None
        self._check_cancel(cancel)
        matched: list[Track] = []
        skipped: list[str] = []
        cache: dict[Track, Track | None] = {}
        total = len(playlist.tracks)
        for index, track in enumerate(playlist.tracks):
            found: Track | None
            self._check_cancel(cancel)
            if progress:
                progress(index, total, track.title)
            if track.provider == "youtube":
                found = track
            elif track in cache:
                found = cache[track]
            else:
                try:
                    found = match_track(track, self.youtube.search(
                        f"{track.title} {' '.join(track.artists)}".strip()[:1000], limit=5
                    ))
                except ProviderError:
                    # An unavailable provider is not an unmatched song. Never save
                    # a misleading partial library after an authentication failure.
                    raise ProviderError("Song matching failed. Check YouTube Music access and retry the import.") from None
                cache[track] = found
            if found:
                matched.append(found)
            else:
                skipped.append(f"{track.title} · {', '.join(track.artists) or 'Unknown artist'}")
        self._check_cancel(cancel)
        if not matched:
            raise ProviderError("No playable songs could be matched. Nothing was saved; try another playlist.")
        warnings = list(playlist.warnings)
        if skipped:
            warnings.append(f"Skipped {len(skipped)} songs without a confident YouTube match.")
            warnings.extend(f"Unmatched: {name}" for name in skipped)
        if progress:
            progress(total, total, "Ready to save")
        return ImportResult(replace(playlist, tracks=tuple(matched), source_url=url, warnings=tuple(warnings)),
                            total, tuple(skipped))

    @staticmethod
    def _check_cancel(cancel: Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise MyneError("Playlist import cancelled. Nothing was saved.")

    def _spotify(self, identifier: str, limit: int, cancel: Event | None) -> Playlist:
        if self.spotify_token:
            return self._spotify_api(identifier, limit, cancel)
        parser = _EmbedData()
        parser.feed(self.fetch(f"https://open.spotify.com/embed/playlist/{identifier}", {}).decode("utf-8"))
        data = json.loads("".join(parser.parts))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        if entity.get("type") != "playlist":
            raise ProviderError("This Spotify playlist is unavailable. Use a public playlist link.")
        rows = entity.get("trackList") or []
        tracks = []
        for row in rows[:limit]:
            if not isinstance(row, Mapping) or row.get("entityType", "track") != "track" or not row.get("title"):
                continue
            uri = row.get("uri", "")
            if not isinstance(uri, str) or not re.fullmatch(r"spotify:track:[A-Za-z0-9]{22}", uri):
                continue
            subtitle = str(row.get("subtitle") or "")
            tracks.append(Track(uri, str(row["title"]), tuple(x.strip() for x in subtitle.split(",") if x.strip()),
                                _duration(row.get("duration"), 1000), provider="spotify"))
        warnings = ["Spotify public view: only exposed songs were read; this may be a partial playlist."]
        if len(rows) > limit:
            warnings.append(f"Import limited to the first {limit} entries.")
        return Playlist(str(entity.get("name") or entity.get("title") or "Spotify playlist"),
                        tuple(tracks), "Spotify", warnings=tuple(warnings))

    def _spotify_api(self, identifier: str, limit: int, cancel: Event | None) -> Playlist:
        headers = {"Authorization": f"Bearer {self.spotify_token}"}
        root = f"https://api.spotify.com/v1/playlists/{identifier}"
        metadata = json.loads(self.fetch(root + "?fields=name", headers))
        tracks: list[Track] = []
        warnings: list[str] = []
        for offset in range(0, limit, 50):
            self._check_cancel(cancel)
            size = min(50, limit - offset)
            page = json.loads(self.fetch(f"{root}/items?limit={size}&offset={offset}", headers))
            rows = page.get("items")
            if not isinstance(rows, list):
                raise ProviderError("Spotify did not expose playlist items. Check your ownership or collaborator access.")
            for row in rows[:size]:
                item = (row.get("item") or row.get("track")) if isinstance(row, Mapping) else None
                if (not isinstance(item, Mapping) or item.get("type") != "track" or not item.get("name")
                        or row.get("is_local") or item.get("is_local")):
                    warnings.append("An unavailable song, local file, or podcast was omitted.")
                    continue
                artists = tuple(str(a["name"]) for a in item.get("artists", []) if isinstance(a, Mapping) and a.get("name"))
                tracks.append(Track(str(item.get("id") or len(tracks)), str(item["name"]), artists,
                                    _duration(item.get("duration_ms"), 1000), provider="spotify"))
            if not page.get("next"):
                break
            if offset + size >= limit:
                warnings.append(f"Import limited to the first {limit} entries.")
        return Playlist(str(metadata.get("name") or "Spotify playlist"), tuple(tracks), "Spotify",
                        warnings=tuple(dict.fromkeys(warnings)))

    def _extract(self, url: str, source: str, limit: int, cancel: Event | None = None) -> Playlist:
        factory = self.extractor_factory
        if factory is None:
            from yt_dlp import YoutubeDL
            factory = YoutubeDL
        options: dict[str, Any] = {
            # SoundCloud's flat set entries contain IDs and URLs, not song
            # titles. Resolve their metadata, but never download any media.
            "extract_flat": "in_playlist" if source.startswith("YouTube") else False,
            "skip_download": True, "quiet": True,
            "no_warnings": True, "logger": _QuietLogger(), "socket_timeout": 20,
            "retries": 1, "extractor_retries": 1, "cachedir": False,
            "playlistend": limit + 1, "lazy_playlist": True, "ignoreerrors": True,
        }
        if self.cookies_file and source.startswith("YouTube"):
            options["cookiefile"] = self.cookies_file
        with ExitStack() as stack:
            extractor = factory(options)
            if hasattr(extractor, "__enter__"):
                extractor = stack.enter_context(extractor)
            elif callable(getattr(extractor, "close", None)):
                stack.callback(extractor.close)
            info = extractor.extract_info(url, download=False)
            if not isinstance(info, Mapping) or "entries" not in info:
                raise ProviderError("No accessible playlist was found at that link.")
            rows = []
            for row in islice(info.get("entries") or (), limit + 1):
                self._check_cancel(cancel)
                rows.append(row)
        warnings: list[str] = []
        if len(rows) > limit:
            warnings.append(f"Import limited to the first {limit} entries.")
        warnings.append("Private, removed, or region-blocked entries may be omitted by the source.")
        tracks: list[Track] = []
        for row in rows[:limit]:
            if not isinstance(row, Mapping) or not row.get("title"):
                continue
            if row.get("availability") in {"private", "premium_only", "subscriber_only", "needs_auth"}:
                continue
            identifier = str(row.get("id") or "")
            if source.startswith("YouTube") and not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
                continue
            artist = row.get("artist") or row.get("uploader") or row.get("channel")
            tracks.append(Track(identifier, str(row["title"]), (str(artist),) if artist else (),
                                _duration(row.get("duration")), provider="youtube" if source.startswith("YouTube") else "soundcloud"))
        return Playlist(str(info.get("title") or f"{source} playlist"), tuple(tracks), source,
                        warnings=tuple(warnings))
