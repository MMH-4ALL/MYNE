"""The public provider extension contract."""

from abc import ABC, abstractmethod

from myne.errors import ProviderError
from myne.models import Album, Artist, Stream, Track


class MusicProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: str, limit: int = 20) -> list[Track]: ...

    @abstractmethod
    def get_song(self, song_id: str) -> Track: ...

    @abstractmethod
    def get_album(self, album_id: str) -> Album: ...

    @abstractmethod
    def get_artist(self, artist_id: str) -> Artist: ...

    @abstractmethod
    def resolve_stream(self, track: Track) -> Stream: ...

    def recommendations(self, track: Track, limit: int = 25) -> list[Track]:
        raise ProviderError(f"{self.name} does not support radio.")

    def lyrics(self, track: Track) -> str | None:
        return None
