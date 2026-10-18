"""A small local-file provider; metadata stays independent of audio playback."""

from pathlib import Path

from myne.errors import ProviderError
from myne.models import Album, Artist, Stream, Track
from myne.providers.base import MusicProvider


class LocalMusicProvider(MusicProvider):
    name = "local"
    extensions = frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".aiff", ".wma"})

    def __init__(self, library_path: str | Path | None = None) -> None:
        self.library_path = Path(library_path).expanduser().resolve() if library_path else None

    def _file(self, path: str) -> Path:
        try:
            file = Path(path).expanduser().resolve()
            if not file.is_file() or file.suffix.lower() not in self.extensions:
                raise ProviderError("Choose an existing, supported local audio file.")
            return file
        except (OSError, ValueError):
            raise ProviderError("The local audio file could not be accessed.") from None

    def get_song(self, song_id: str) -> Track:
        file = self._file(song_id)
        pieces = file.stem.split(" - ", 1)
        artist, title = (pieces[0], pieces[1]) if len(pieces) == 2 else (None, file.stem)
        return Track(
            id=str(file),
            title=title,
            artists=(artist,) if artist else (),
            album=file.parent.name,
            provider=self.name,
        )

    def search(self, query: str, limit: int = 20) -> list[Track]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ProviderError("The result limit must be between 1 and 100.")
        if not query.strip():
            raise ProviderError("Enter a local music search query.")
        if self.library_path is None:
            raise ProviderError("Configure a local library path or play an audio file by path.")
        if not self.library_path.is_dir():
            raise ProviderError("The configured local music library is not a directory.")
        tracks: list[Track] = []
        try:
            for file in sorted(self.library_path.rglob("*")):
                if file.suffix.lower() in self.extensions and query.casefold() in file.stem.casefold():
                    try:
                        tracks.append(self.get_song(str(file)))
                    except ProviderError:
                        continue
                    if len(tracks) >= limit:
                        break
        except OSError:
            raise ProviderError("The local music library could not be read.") from None
        return tracks

    def get_album(self, album_id: str) -> Album:
        raise ProviderError("Local album browsing is not available; search your library by filename.")

    def get_artist(self, artist_id: str) -> Artist:
        raise ProviderError("Local artist browsing is not available; search your library by filename.")

    def resolve_stream(self, track: Track) -> Stream:
        if track.provider != self.name:
            raise ProviderError("This track belongs to another music provider.")
        return Stream(self._file(track.id).as_uri())
