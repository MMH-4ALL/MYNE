"""Provider-independent, serializable domain objects."""

import math
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Track:
    id: str
    title: str
    artists: tuple[str, ...] = ()
    duration: float | None = None
    album: str | None = None
    provider: str = "youtube"
    genres: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track":
        for key in ("id", "title"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"Track {key} must be a non-empty string.")
        provider = data.get("provider", "youtube")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("Track provider must be a non-empty string.")
        for key in ("artists", "genres"):
            values = data.get(key)
            if values is not None and (
                not isinstance(values, (list, tuple))
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError(f"Track {key} must be a list of strings.")
        album = data.get("album")
        if album is not None and not isinstance(album, str):
            raise ValueError("Track album must be a string or null.")
        raw_duration = data.get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
        if isinstance(raw_duration, bool) or duration is not None and (
            not math.isfinite(duration) or duration < 0
        ):
            raise ValueError("Track duration must be a finite, non-negative number.")
        return cls(
            id=data["id"],
            title=data["title"],
            artists=tuple(data.get("artists") or ()),
            duration=duration,
            album=album,
            provider=provider,
            genres=tuple(data.get("genres") or ()),
        )


@dataclass(frozen=True, slots=True)
class Album:
    id: str
    title: str
    artists: tuple[str, ...] = ()
    tracks: tuple[Track, ...] = ()
    provider: str = "youtube"


@dataclass(frozen=True, slots=True)
class Artist:
    id: str
    name: str
    top_tracks: tuple[Track, ...] = ()
    albums: tuple[Album, ...] = ()
    provider: str = "youtube"


@dataclass(frozen=True, slots=True)
class Playlist:
    name: str
    tracks: tuple[Track, ...] = ()
    source: str = "Local"
    source_url: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
