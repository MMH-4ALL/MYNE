"""Shared-link import contracts using synthetic metadata; no live accounts."""

import json
from threading import Event
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from myne.errors import MyneError, ProviderError
from myne.models import Playlist, Track
from myne.playlists import PlaylistImporter, _read_url, _SameHostRedirect, match_track, playlist_link

SPOTIFY_ID = "A" * 22
SPOTIFY_LINK = f"https://open.spotify.com/playlist/{SPOTIFY_ID}"
YT_LINK = "https://www.youtube.com/playlist?list=PLsample"


def spotify_embed(rows=None):
    rows = rows if rows is not None else [{
        "uri": "spotify:track:" + "B" * 22, "title": "Glass Lines", "subtitle": "Northbound",
        "duration": 240000, "entityType": "track",
    }]
    data = {"props": {"pageProps": {"state": {"data": {"entity": {
        "type": "playlist", "name": "Night [rotation]", "trackList": rows,
    }}}}}}
    return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>").encode()


@pytest.mark.parametrize(("link", "source", "canonical"), [
    (SPOTIFY_LINK + "?si=sharing-secret", "Spotify", SPOTIFY_LINK),
    (f"spotify:playlist:{SPOTIFY_ID}", "Spotify", SPOTIFY_LINK),
    (f"https://open.spotify.com/intl-ar/playlist/{SPOTIFY_ID}", "Spotify", SPOTIFY_LINK),
    (f"https://open.spotify.com/embed/playlist/{SPOTIFY_ID}", "Spotify", SPOTIFY_LINK),
    ("https://music.youtube.com/playlist?list=PLsample&si=private", "YouTube Music", YT_LINK),
    ("https://youtu.be/abcdefghijk?list=PLsample", "YouTube", YT_LINK),
    ("https://soundcloud.com/artist/sets/night-drive?si=private", "SoundCloud",
     "https://soundcloud.com/artist/sets/night-drive"),
])
def test_canonical_links_strip_share_tracking(link, source, canonical):
    actual_source, _, url = playlist_link(link)
    assert actual_source == source
    assert url == canonical


@pytest.mark.parametrize("link", [
    "", "https://example.com/playlist/123", "file:///etc/passwd", "http://127.0.0.1/playlist",
    "https://open.spotify.com.attacker.test/playlist/" + SPOTIFY_ID,
    "https://secret@open.spotify.com/playlist/" + SPOTIFY_ID,
    "https://open.spotify.com:444/playlist/" + SPOTIFY_ID,
    "https://open.spotify.com:bad/playlist/" + SPOTIFY_ID,
    "https://open.spotify.com/track/" + SPOTIFY_ID,
    "https://spotify.link/short", "https://www.youtube.com/watch?v=abcdefghijk",
    YT_LINK + "&list=PLother", "https://soundcloud.com/artist/single-song",
    "https://open.spotify.com/playlist/" + SPOTIFY_ID + "\nsecret",
])
def test_invalid_links_do_not_start_network_work(link):
    fetch = Mock()
    with pytest.raises(MyneError):
        PlaylistImporter(Mock(), fetch=fetch).read(link)
    fetch.assert_not_called()


def test_public_spotify_import_matches_songs_and_reports_partial_view(monkeypatch):
    monkeypatch.delenv("MYNE_SPOTIFY_TOKEN", raising=False)
    provider = Mock()
    right = Track("abcdefghijk", "Glass Lines", ("Northbound",), 240)
    provider.search.return_value = [Track("bad", "A different song", ("Other",), 240), right]
    fetch = Mock(return_value=spotify_embed())
    progress = Mock()
    result = PlaylistImporter(provider, fetch=fetch).read(SPOTIFY_LINK + "?si=private", progress=progress)
    assert result.playlist.tracks == (right,)
    assert result.playlist.source == "Spotify"
    assert result.playlist.source_url == SPOTIFY_LINK
    assert "partial" in result.playlist.warnings[0]
    assert result.scanned == 1
    assert result.skipped == ()
    assert progress.call_args.args == (1, 1, "Ready to save")
    fetch.assert_called_once_with(f"https://open.spotify.com/embed/playlist/{SPOTIFY_ID}", {})
    assert "audioPreview" not in str(result)


def test_matching_preserves_order_duplicates_and_reports_unmatched(monkeypatch):
    monkeypatch.delenv("MYNE_SPOTIFY_TOKEN", raising=False)
    first = {"uri": "spotify:track:" + "B" * 22, "title": "Glass Lines", "subtitle": "Northbound"}
    missing = {"uri": "spotify:track:" + "C" * 22, "title": "Missing song", "subtitle": "No one"}
    right = Track("abcdefghijk", "Glass Lines", ("Northbound",))
    provider = Mock()
    provider.search.side_effect = [[right], []]
    result = PlaylistImporter(provider, fetch=lambda *_: spotify_embed([first, missing, first])).read(SPOTIFY_LINK)
    assert result.playlist.tracks == (right, right)
    assert result.scanned == 3
    assert result.skipped == ("Missing song · No one",)
    assert "Unmatched: Missing song" in result.playlist.warnings[-1]
    assert provider.search.call_count == 2


@pytest.mark.parametrize("candidate", [
    Track("x", "Unrelated song", ("Northbound",), 240),
    Track("x", "Glass Lines", ("Someone else",), 240),
    Track("x", "Glass Lines (live)", ("Northbound",), 240),
    Track("x", "Glass Lines (cover)", ("Northbound",), 240),
    Track("x", "Glass Lines", ("Northbound",), 500),
    Track("x", "Glass Lines", ("Northbound",), 240, provider="local"),
])
def test_matching_rejects_wrong_artists_versions_and_lengths(candidate):
    assert match_track(Track("s", "Glass Lines", ("Northbound",), 240), [candidate]) is None


def test_matching_accepts_punctuation_and_official_audio_suffixes():
    source = Track("s", "GLASS — Lines", ("Northbound",), 240)
    right = Track("x", "Glass Lines (Official Audio)", ("Northbound - Topic",), 241)
    assert match_track(source, [right]) == right


def test_matching_does_not_treat_a_partial_title_as_an_exact_song():
    assert match_track(Track("source", "Numb", ("Linkin Park",), 185),
                       [Track("other", "Numb / Encore", ("Linkin Park",), 190)]) is None


def test_spotify_api_uses_current_items_endpoint_and_bounded_pagination():
    item = {"type": "track", "id": "B" * 22, "name": "Glass Lines", "artists": [{"name": "Northbound"}]}
    fetch = Mock(side_effect=[json.dumps({"name": "API playlist"}).encode(),
                              json.dumps({"items": [{"item": item}] * 50, "next": "https://untrusted.example"}).encode(),
                              json.dumps({"items": [{"track": item}], "next": None}).encode()])
    provider = Mock()
    provider.search.return_value = [Track("abcdefghijk", "Glass Lines", ("Northbound",))]
    result = PlaylistImporter(provider, fetch=fetch, spotify_token="private-token").read(SPOTIFY_LINK, limit=51)
    assert len(result.playlist.tracks) == 51
    assert result.playlist.warnings == ()
    assert provider.search.call_count == 1
    assert fetch.call_args.args[0] == f"https://api.spotify.com/v1/playlists/{SPOTIFY_ID}/items?limit=1&offset=50"
    assert fetch.call_args.args[1] == {"Authorization": "Bearer private-token"}
    assert "private-token" not in str(result)
    assert all("untrusted" not in call.args[0] for call in fetch.call_args_list)


def test_spotify_api_refuses_metadata_only_access():
    fetch = Mock(side_effect=[b'{"name":"Private"}', b'{"error":"not allowed"}'])
    with pytest.raises(ProviderError, match="ownership"):
        PlaylistImporter(Mock(), fetch=fetch, spotify_token="token").read(SPOTIFY_LINK)


@pytest.mark.parametrize("link,source", [(YT_LINK, "YouTube"),
    ("https://music.youtube.com/playlist?list=PLsample", "YouTube Music"),
    ("https://soundcloud.com/artist/sets/night", "SoundCloud")])
def test_extractor_metadata_only_import_limits_and_closes(link, source):
    extractor = Mock()
    rows = [{"id": "abcdefghijk", "title": "Glass Lines", "artist": "Northbound", "duration": 240}] * 3
    extractor.extract_info.return_value = {"title": "Collection", "entries": iter(rows)}
    factory = Mock(return_value=extractor)
    provider = Mock()
    provider.get_playlist.side_effect = ProviderError("Use fallback")
    provider.search.return_value = [Track("abcdefghijk", "Glass Lines", ("Northbound",), 240)]
    result = PlaylistImporter(provider, extractor_factory=factory).read(link, limit=2)
    assert len(result.playlist.tracks) == 2
    assert result.playlist.source == source
    assert "first 2" in result.playlist.warnings[0]
    assert factory.call_args.args[0]["skip_download"]
    assert factory.call_args.args[0]["extract_flat"] == ("in_playlist" if source.startswith("YouTube") else False)
    assert extractor.extract_info.call_args.kwargs == {"download": False}
    extractor.close.assert_called_once()
    if source.startswith("YouTube"):
        provider.search.assert_not_called()
    else:
        provider.search.assert_called_once()


def test_youtube_import_uses_metadata_api_before_extractor():
    provider = Mock()
    tracks = (Track("abcdefghijk", "Song"), Track("abcdefghijk", "Song"))
    provider.get_playlist.return_value = Playlist("My playlist", tracks, "YouTube Music")
    factory = Mock()
    result = PlaylistImporter(provider, extractor_factory=factory).read(YT_LINK, limit=10)
    assert result.playlist.tracks == tracks
    assert result.playlist.source == "YouTube"
    provider.get_playlist.assert_called_once_with("PLsample", 10)
    provider.search.assert_not_called()
    factory.assert_not_called()


def test_import_cancellation_stops_between_matches(monkeypatch):
    monkeypatch.delenv("MYNE_SPOTIFY_TOKEN", raising=False)
    cancelled = Event()
    provider = Mock()
    def search(*args, **kwargs):
        cancelled.set()
        return [Track("abcdefghijk", "Glass Lines", ("Northbound",))]
    provider.search.side_effect = search
    with pytest.raises(MyneError, match="cancelled"):
        PlaylistImporter(provider, fetch=lambda *_: spotify_embed()).read(SPOTIFY_LINK, cancel=cancelled)


def test_provider_failure_is_not_saved_as_unmatched_songs(monkeypatch):
    monkeypatch.delenv("MYNE_SPOTIFY_TOKEN", raising=False)
    provider = Mock()
    provider.search.side_effect = ProviderError("token=private")
    with pytest.raises(ProviderError, match="matching failed") as error:
        PlaylistImporter(provider, fetch=lambda *_: spotify_embed()).read(SPOTIFY_LINK)
    assert "private" not in str(error.value)


def test_malformed_page_errors_are_safe(monkeypatch):
    monkeypatch.delenv("MYNE_SPOTIFY_TOKEN", raising=False)
    with pytest.raises(ProviderError, match="metadata could not be read") as error:
        PlaylistImporter(Mock(), fetch=lambda *_: b"Authorization: secret").read(SPOTIFY_LINK)
    assert "secret" not in str(error.value)


def test_cross_host_redirects_do_not_forward_bearer_tokens():
    request = Request("https://api.spotify.com/v1/playlists/test", headers={"Authorization": "Bearer secret"})
    with pytest.raises(ProviderError, match="unexpected address"):
        _SameHostRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.example/data")


@pytest.mark.parametrize("code,expected", [(401, "private"), (403, "refused"), (429, "busy"), (404, "link")])
def test_http_failures_do_not_leak_upstream_tokens(monkeypatch, code, expected):
    opener = Mock()
    opener.open.side_effect = HTTPError("https://secret.example?token=private", code, "secret", {}, None)
    monkeypatch.setattr("myne.playlists.build_opener", lambda *_: opener)
    with pytest.raises(ProviderError, match=expected) as error:
        _read_url(SPOTIFY_LINK, {})
    assert "token=" not in str(error.value)


@pytest.mark.parametrize("limit", [0, 501, True, 1.5])
def test_import_limit_is_checked_before_fetching(limit):
    fetch = Mock()
    with pytest.raises(MyneError):
        PlaylistImporter(Mock(), fetch=fetch).read(SPOTIFY_LINK, limit=limit)
    fetch.assert_not_called()
