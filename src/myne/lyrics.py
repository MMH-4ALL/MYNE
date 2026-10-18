"""Cached, provider-independent lyrics with seek-safe LRC synchronization.

LRCLIB's anonymous API supplies synchronized lyrics without an API key. Existing
music providers remain the fallback when a recording has no LRCLIB match.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import tempfile
import threading
import time as time_module
import unicodedata
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeGuard
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from myne import __version__
from myne.models import Track
from myne.providers.base import MusicProvider

_MAX_RESPONSE = 1_048_576
_MAX_TEXT = 200_000
_MAX_LINES = 10_000
_MAX_CACHE_ENTRIES = 128
_MAX_DISK_ENTRIES = 256
_POSITIVE_TTL = 7 * 24 * 60 * 60
_MISSING_TTL = 15 * 60
_FAILURE_TTL = 60
_TIMESTAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"\[offset\s*:\s*([+-]?\d{1,9})\]", re.IGNORECASE)
_WORD_TIMESTAMP = re.compile(r"<\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?>")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PRESENTATION_TAG = re.compile(
    r"\s*[\[(](?:official(?:\s+(?:music\s+)?video|\s+audio|\s+lyrics)?|"
    r"lyrics?(?:\s+video)?|audio|music video|hd|hq)[\])]\s*",
    re.IGNORECASE,
)


def _text(value: object) -> str:
    """Keep lyric content as plain text, including intentional newlines."""
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value[:_MAX_TEXT])
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159)


def _finite_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class LyricsLine:
    time: float
    text: str


@dataclass(frozen=True, slots=True)
class LyricsDocument:
    title: str
    artist: str
    plain: str
    synced: tuple[LyricsLine, ...]
    source: str
    instrumental: bool = False

    def current_index(self, position: float) -> int | None:
        """Find the current line afresh, so pausing and backwards seeks work."""
        if not self.synced or not math.isfinite(position):
            return None
        index = bisect_right(self.synced, position, key=lambda line: line.time) - 1
        return index if index >= 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "plain": self.plain,
            "synced": [{"time": line.time, "text": line.text} for line in self.synced],
            "source": self.source,
            "instrumental": self.instrumental,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LyricsDocument:
        rows = data.get("synced", [])
        if not isinstance(rows, (list, tuple)) or len(rows) > _MAX_LINES:
            raise ValueError("Invalid synchronized lyrics.")
        lines: list[LyricsLine] = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("text"), str):
                raise ValueError("Invalid lyric line.")
            timestamp = row.get("time")
            if not _finite_number(timestamp) or timestamp < 0:
                raise ValueError("Invalid lyric timestamp.")
            lines.append(LyricsLine(float(timestamp), _text(row["text"])))
        return cls(
            title=_text(data.get("title")),
            artist=_text(data.get("artist")),
            plain=_text(data.get("plain")),
            synced=tuple(sorted(lines, key=lambda line: line.time)),
            source=_text(data.get("source")),
            instrumental=data.get("instrumental") is True,
        )


def parse_lrc(value: str) -> tuple[LyricsLine, ...]:
    """Read line timestamps, repeated tags, global offsets and word tags.

    LRC's positive global offset makes lyrics appear earlier. Equal timestamps
    are merged in their original order; duplicate text is displayed once.
    """
    value = _text(value)
    offsets = _OFFSET.findall(value)
    offset = int(offsets[-1]) / 1000 if offsets else 0.0
    by_time: dict[float, list[str]] = {}
    for raw_line in value.splitlines()[:_MAX_LINES]:
        raw_line = raw_line.lstrip("\ufeff \t")
        # Only leading timestamps count. Literal timestamps in lyric text do not.
        matches = []
        start = 0
        while match := _TIMESTAMP.match(raw_line, start):
            matches.append(match)
            start = match.end()
        if not matches:
            continue
        text = _WORD_TIMESTAMP.sub("", raw_line[start:]).strip()
        for match in matches:
            fraction = float("0." + match[3]) if match[3] else 0.0
            timestamp = max(0.0, round(int(match[1]) * 60 + int(match[2]) + fraction - offset, 6))
            texts = by_time.setdefault(timestamp, [])
            if text not in texts:
                texts.append(text)
    return tuple(
        LyricsLine(timestamp, "\n".join(text for text in texts if text))
        for timestamp, texts in sorted(by_time.items())
    )


def _normalize(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _metadata(track: Track) -> tuple[str, tuple[str, ...]]:
    title = _PRESENTATION_TAG.sub(" ", track.title).strip()
    artists = tuple(
        re.sub(r"(?:\s+-\s+Topic|VEVO)$", "", artist, flags=re.IGNORECASE).strip()
        for artist in track.artists
        if artist.strip()
    )
    if not artists and track.provider == "local" and " - " in title:
        artist, title = title.split(" - ", 1)
        artists = (artist.strip(),)
    return title, artists


def _artist_matches(candidate: str, artists: tuple[str, ...]) -> bool:
    wanted = {_normalize(artist) for artist in artists}
    if _normalize(candidate) in wanted:
        return True
    parts = re.split(r"\s+(?:feat\.?|ft\.?|featuring|and)\s+|[,;&]", candidate, flags=re.IGNORECASE)
    return any(_normalize(part) in wanted for part in parts if part.strip())


class LyricsService:
    """Fetch and cache lyrics with explicit recording matching and no credentials.

    ``cache_dir`` is the app's cache root; this service owns its ``lyrics`` child.
    Pass ``opener`` and ``clock`` to test the service without network or sleeps.
    """

    def __init__(
        self,
        provider_lookup: Callable[[str], MusicProvider],
        cache_dir: Path | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time_module.time,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._provider_lookup = provider_lookup
        self._cache_dir = Path(cache_dir) / "lyrics" if cache_dir is not None else None
        self._opener = opener or urlopen
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.RLock()
        self._memory: OrderedDict[str, tuple[float, LyricsDocument | None]] = OrderedDict()
        self._last_request = 0.0
        self._retry_after = 0.0

    def get(self, track: Track, refresh: bool = False) -> LyricsDocument | None:
        title, artists = _metadata(track)
        key = hashlib.sha256(
            json.dumps(
                [track.provider, track.id, title, artists, track.album, track.duration],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        # Serializing requests also coalesces simultaneous CLI/TUI lookups and
        # honors LRCLIB's requirement to avoid concurrent request bursts.
        with self._lock:
            if not refresh:
                cached = self._cached(key)
                if cached is not None:
                    return cached[1]
            document = None
            temporary_failure = False
            if title and artists:
                document, temporary_failure = self._lrclib(track, title, artists)
            if document is None:
                try:
                    raw = self._provider_lookup(track.provider).lyrics(track)
                    if isinstance(raw, str) and raw.strip():
                        synced = parse_lrc(raw)
                        document = LyricsDocument(
                            title=title,
                            artist=", ".join(artists),
                            plain="\n".join(line.text for line in synced) if synced else _text(raw).strip(),
                            synced=synced,
                            source="YouTube Music" if track.provider == "youtube" else track.provider,
                        )
                except Exception:
                    # A provider's signed URLs, tokens, or raw server responses
                    # must never escape into error messages or the cache.
                    temporary_failure = True
            ttl = (
                _POSITIVE_TTL if document is not None else _FAILURE_TTL if temporary_failure else _MISSING_TTL
            )
            self._remember(key, self._clock() + ttl, document)
            return document

    def _request(self, endpoint: str, parameters: dict[str, str]) -> tuple[object, bool]:
        if self._clock() < self._retry_after:
            return None, True
        wait = 0.25 - (time_module.monotonic() - self._last_request)
        if wait > 0:
            self._sleeper(wait)
        request = Request(
            "https://lrclib.net/api/" + endpoint + "?" + urlencode(parameters),
            headers={
                "User-Agent": f"Myne CLI/{__version__} (https://github.com/MMH-4ALL/myne-cli)",
                "Accept": "application/json",
            },
        )
        try:
            self._last_request = time_module.monotonic()
            with self._opener(request, timeout=7) as response:
                content_type = response.headers.get("Content-Type", "")
                if content_type and "application/json" not in content_type.lower():
                    return None, True
                body = response.read(_MAX_RESPONSE + 1)
            if len(body) > _MAX_RESPONSE:
                return None, True
            return json.loads(body), False
        except HTTPError as error:
            if error.code == 404:
                return None, False
            if error.code == 429:
                raw_retry = error.headers.get("Retry-After", "60") if error.headers else "60"
                try:
                    delay = float(raw_retry)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(raw_retry).timestamp() - self._clock()
                    except (TypeError, ValueError, OverflowError):
                        delay = 60.0
                if not math.isfinite(delay):
                    delay = 60.0
                delay = max(1.0, delay)
                self._retry_after = self._clock() + delay + random.uniform(0, delay * 0.1)
            return None, True
        except Exception:
            return None, True

    def _lrclib(
        self, track: Track, title: str, artists: tuple[str, ...]
    ) -> tuple[LyricsDocument | None, bool]:
        params = {"track_name": title, "artist_name": artists[0]}
        if track.album:
            params["album_name"] = track.album
        if track.duration is not None and math.isfinite(track.duration) and 1 <= track.duration <= 3600:
            params["duration"] = str(round(track.duration, 3))
        exact, failed = self._request("get", params)
        if isinstance(exact, Mapping):
            document = self._document(exact, track, title, artists)
            if document is not None and (document.synced or document.instrumental):
                return document, failed
        else:
            document = None
        # Do not double an outage or a 429 into a second request. The underlying
        # provider can still return its own plain lyrics immediately.
        if failed:
            return document, True
        result, failed = self._request("search", {"track_name": title, "artist_name": artists[0]})
        candidates: list[tuple[tuple[int, int, float], LyricsDocument]] = []
        if isinstance(result, list):
            for row in result[:20]:
                if not isinstance(row, Mapping):
                    continue
                candidate = self._document(row, track, title, artists)
                if candidate is None:
                    continue
                duration = row.get("duration")
                distance = (
                    abs(float(duration) - track.duration)
                    if (
                        _finite_number(duration)
                        and track.duration is not None
                        and math.isfinite(track.duration)
                    )
                    else 0.0
                )
                score = (
                    int(bool(candidate.synced)),
                    int(
                        bool(
                            track.album
                            and _normalize(str(row.get("albumName", ""))) == _normalize(track.album)
                        )
                    ),
                    -distance,
                )
                candidates.append((score, candidate))
        if candidates:
            return max(candidates, key=lambda candidate: candidate[0])[1], failed
        return document, failed

    @staticmethod
    def _document(
        record: Mapping[str, Any], track: Track, title: str, artists: tuple[str, ...]
    ) -> LyricsDocument | None:
        candidate_title = _text(record.get("trackName") or record.get("name"))
        candidate_artist = _text(record.get("artistName"))
        if _normalize(candidate_title) != _normalize(title) or not _artist_matches(candidate_artist, artists):
            return None
        duration = record.get("duration")
        if track.duration is not None and math.isfinite(track.duration) and track.duration > 0:
            if not _finite_number(duration) or abs(duration - track.duration) > 3:
                return None
        synced = parse_lrc(_text(record.get("syncedLyrics")))
        plain = _text(record.get("plainLyrics")).strip()
        instrumental = record.get("instrumental") is True
        if not plain and synced:
            plain = "\n".join(line.text for line in synced).strip()
        if not plain and not synced and not instrumental:
            return None
        return LyricsDocument(candidate_title, candidate_artist, plain, synced, "LRCLIB", instrumental)

    def _cached(self, key: str) -> tuple[float, LyricsDocument | None] | None:
        entry = self._memory.get(key)
        if entry is not None:
            if entry[0] > self._clock():
                self._memory.move_to_end(key)
                return entry
            del self._memory[key]
        if self._cache_dir is None:
            return None
        try:
            file = self._cache_dir / (key + ".json")
            if file.stat().st_size > _MAX_RESPONSE:
                return None
            value = json.loads(file.read_text(encoding="utf-8"))
            expires = value["expires"]
            if (
                not isinstance(expires, (int, float))
                or not math.isfinite(expires)
                or expires <= self._clock()
            ):
                return None
            if expires > self._clock() + _POSITIVE_TTL:
                return None
            raw = value["document"]
            if raw is not None and not isinstance(raw, Mapping):
                return None
            document = LyricsDocument.from_dict(raw) if raw is not None else None
            entry = (float(expires), document)
            self._memory[key] = entry
            self._bound_memory()
            return entry
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None

    def _bound_memory(self) -> None:
        while len(self._memory) > _MAX_CACHE_ENTRIES:
            self._memory.popitem(last=False)

    def _remember(self, key: str, expires: float, document: LyricsDocument | None) -> None:
        self._memory[key] = (expires, document)
        self._memory.move_to_end(key)
        self._bound_memory()
        if self._cache_dir is None:
            return
        temporary: str | None = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            body = json.dumps(
                {
                    "expires": expires,
                    "document": document.to_dict() if document is not None else None,
                },
                ensure_ascii=False,
            )
            if len(body.encode()) > _MAX_RESPONSE:
                return
            descriptor, temporary = tempfile.mkstemp(prefix=".lyrics-", dir=self._cache_dir)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(body)
            os.replace(temporary, self._cache_dir / (key + ".json"))
            temporary = None
            files = sorted(
                self._cache_dir.glob("[0-9a-f]" * 64 + ".json"), key=lambda file: file.stat().st_mtime
            )
            for old in files[:-_MAX_DISK_ENTRIES]:
                old.unlink(missing_ok=True)
        except OSError:
            # Read-only/full cache directories must not break lyric lookup.
            pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
