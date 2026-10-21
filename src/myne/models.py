"""Provider-independent, serializable domain objects."""

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
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            artists=tuple(data.get("artists") or ()),
            duration=float(data["duration"]) if data.get("duration") is not None else None,
            album=data.get("album"),
            provider=str(data.get("provider", "youtube")),
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
