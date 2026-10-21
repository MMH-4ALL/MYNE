"""Provider contract tests use synthetic metadata and never access a network."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from myne.errors import ProviderError
from myne.models import Track
from myne.providers.local import LocalMusicProvider
from myne.providers.youtube import YouTubeMusicProvider


@pytest.fixture
def metadata() -> Mock:
    return Mock()


def song(identifier: str = "song1", **extra: object) -> dict[str, object]:
    return {"videoId": identifier, "title": "Test song", "artists": [{"name": "Test artist"}], **extra}


def test_search_maps_metadata_and_skips_unavailable_or_duplicate_rows(metadata: Mock) -> None:
    metadata.search.return_value = [
        song(duration="3:05", album={"name": "Test album"}),
        song(),
        song("unavailable", isAvailable=False),
        {"browseId": "album1", "title": "An album"},
        None,
    ]
    provider = YouTubeMusicProvider(client=metadata)
    tracks = provider.search(" query ", 5)
    assert tracks == [Track("song1", "Test song", ("Test artist",), 185.0, "Test album")]
    metadata.search.assert_called_once_with("query", filter="songs", limit=5)


def test_get_song_maps_video_details(metadata: Mock) -> None:
    metadata.get_song.return_value = {
        "videoDetails": {"videoId": "song1", "title": "Song", "author": "Artist", "lengthSeconds": "60"}
    }
    assert YouTubeMusicProvider(client=metadata).get_song("song1") == Track(
        "song1", "Song", ("Artist",), 60.0
    )


def test_youtube_playlist_preserves_duplicate_entries_and_reports_limits(metadata: Mock) -> None:
    metadata.get_playlist.return_value = {
        "title": "Collection", "trackCount": 12,
        "tracks": [song("first"), song("first"), song("private", isAvailable=False), song("last")],
    }
    playlist = YouTubeMusicProvider(client=metadata).get_playlist("PLsample", 3)
    assert [t.id for t in playlist.tracks] == ["first", "first"]
    assert "first 3" in playlist.warnings[0]
    assert "omitted" in playlist.warnings[1]
    metadata.get_playlist.assert_called_once_with("PLsample", limit=4)


@pytest.mark.parametrize("duration", ["unknown", float("nan"), -1, None])
def test_bad_optional_durations_are_not_fatal(metadata: Mock, duration: object) -> None:
    metadata.search.return_value = [song(duration_seconds=duration)]
    assert YouTubeMusicProvider(client=metadata).search("song")[0].duration is None


def test_album_tracks_inherit_album_and_artist(metadata: Mock) -> None:
    metadata.get_album.return_value = {
        "title": "Collection",
        "artists": [{"name": "Artist"}],
        "tracks": [{"videoId": "song1", "title": "Song"}],
    }
    album = YouTubeMusicProvider(client=metadata).get_album("album1")
    assert album.id == "album1"
    assert album.tracks[0].album == "Collection"
    assert album.tracks[0].artists == ("Artist",)


def test_artist_has_tracks_and_shallow_releases(metadata: Mock) -> None:
    metadata.get_artist.return_value = {
        "name": "Artist",
        "songs": {"results": [{"videoId": "song1", "title": "Song"}]},
        "albums": {"results": [{"browseId": "album1", "title": "Collection"}]},
    }
    artist = YouTubeMusicProvider(client=metadata).get_artist("artist1")
    assert artist.top_tracks[0].artists == ("Artist",)
    assert artist.albums[0].id == "album1"
    assert artist.albums[0].tracks == ()


def test_radio_requests_real_recommendations_and_omits_seed(metadata: Mock) -> None:
    metadata.get_watch_playlist.return_value = {
        "tracks": [song(), song("song2"), song("song2"), song("song3")]
    }
    provider = YouTubeMusicProvider(client=metadata)
    assert [t.id for t in provider.recommendations(Track("song1", "Song"), 1)] == ["song2"]
    metadata.get_watch_playlist.assert_called_once_with(videoId="song1", radio=True, limit=2)


def test_lyrics_lookup_and_missing_lyrics(metadata: Mock) -> None:
    metadata.get_watch_playlist.return_value = {"lyrics": None}
    provider = YouTubeMusicProvider(client=metadata)
    assert provider.lyrics(Track("song1", "Song")) is None
    metadata.get_lyrics.assert_not_called()
    metadata.get_watch_playlist.return_value = {"lyrics": "lyrics1"}
    metadata.get_lyrics.return_value = {"lyrics": " A synthetic line for testing.\n"}
    assert provider.lyrics(Track("song1", "Song")) == "A synthetic line for testing."


def test_stream_is_resolved_without_downloading_or_metadata_lookup(metadata: Mock) -> None:
    extractor = Mock()
    extractor.extract_info.return_value = {
        "url": "https://stream.example/audio?signature=secret",
        "http_headers": {"User-Agent": "Myne", "Bad": "x\r\ny"},
    }
    factory = Mock(return_value=extractor)
    provider = YouTubeMusicProvider(client=metadata, extractor_factory=factory, cookies_file="cookies.txt")
    stream = provider.resolve_stream(Track("song1", "Song"))
    assert stream.url == "https://stream.example/audio?signature=secret"
    assert stream.headers == {"User-Agent": "Myne"}
    extractor.extract_info.assert_called_once_with("https://music.youtube.com/watch?v=song1", download=False)
    extractor.close.assert_called_once()
    assert factory.call_args.args[0]["skip_download"] is True
    assert factory.call_args.args[0]["cookiefile"] == "cookies.txt"
    metadata.get_song.assert_not_called()


def test_extraction_failures_do_not_leak_signed_urls(capsys: pytest.CaptureFixture[str]) -> None:
    extractor = Mock()
    extractor.extract_info.side_effect = RuntimeError("https://secret.example/?token=private")
    factory = Mock(return_value=extractor)
    provider = YouTubeMusicProvider(extractor_factory=factory)
    with pytest.raises(ProviderError) as error:
        provider.resolve_stream(Track("song1", "Song"))
    assert "secret" not in str(error.value)
    for name in ("debug", "info", "warning", "error"):
        getattr(factory.call_args.args[0]["logger"], name)("https://secret.example")
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_metadata_failure_is_sanitized(metadata: Mock) -> None:
    metadata.search.side_effect = RuntimeError("Authorization: private")
    with pytest.raises(ProviderError) as error:
        YouTubeMusicProvider(client=metadata).search("song")
    assert "private" not in str(error.value)


@pytest.mark.parametrize("identifier", ["", "../bad", "song?token=x", "https://example.com"])
def test_identifiers_are_validated_before_extraction(identifier: str) -> None:
    factory = Mock()
    with pytest.raises(ProviderError):
        YouTubeMusicProvider(extractor_factory=factory).resolve_stream(Track(identifier, "Song"))
    factory.assert_not_called()


def test_local_provider_search_and_file_uri(tmp_path: Path) -> None:
    file = tmp_path / "Artist - Song name.wav"
    file.write_bytes(b"test")
    (tmp_path / "not audio.txt").write_text("ignored")
    provider = LocalMusicProvider(tmp_path)
    tracks = provider.search("song NAME")
    assert len(tracks) == 1
    assert tracks[0].artists == ("Artist",)
    assert tracks[0].title == "Song name"
    assert provider.resolve_stream(tracks[0]).url == file.as_uri()
    file.unlink()
    with pytest.raises(ProviderError):
        provider.resolve_stream(tracks[0])


def test_local_provider_allows_explicit_path_without_library(tmp_path: Path) -> None:
    file = tmp_path / "Song.ogg"
    file.touch()
    provider = LocalMusicProvider()
    assert provider.get_song(str(file)).provider == "local"
    with pytest.raises(ProviderError, match="library"):
        provider.search("Song")
