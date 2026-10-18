"""Lyrics tests use synthetic text and an injected HTTP opener."""

import io
import json
import math
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from myne.lyrics import LyricsDocument, LyricsLine, LyricsService, parse_lrc
from myne.models import Track

TRACK = Track("test-id", "Synthetic Song", ("Test Artist",), 120, "Synthetic Album")


class Response(io.BytesIO):
    def __init__(self, data: object, content_type: str = "application/json") -> None:
        super().__init__(json.dumps(data).encode())
        self.headers = {"Content-Type": content_type}


def record(**extra: object) -> dict[str, object]:
    return {
        "trackName": "Synthetic Song",
        "artistName": "Test Artist",
        "albumName": "Synthetic Album",
        "duration": 120,
        "plainLyrics": "First synthetic line\nSecond synthetic line",
        "syncedLyrics": "[00:02.00]First synthetic line\n[00:07.50]Second synthetic line",
        "instrumental": False,
        **extra,
    }


def service(
    opener: Mock, tmp_path: Path | None = None, clock: Mock | None = None
) -> tuple[LyricsService, Mock]:
    provider = Mock()
    provider.lyrics.return_value = None
    lookup = Mock(return_value=provider)
    return LyricsService(
        lookup, tmp_path, opener=opener, clock=clock or Mock(return_value=1000), sleeper=Mock()
    ), provider


def missing() -> HTTPError:
    return HTTPError("https://lrclib.net/api/get", 404, "Not Found", {}, None)


def test_lrc_multiple_timestamps_variable_precision_and_metadata() -> None:
    assert parse_lrc(
        "[ar:Test Artist]\n[00:12.5][00:02.125]Repeated test line\n"
        "[00:01]Earlier line\n[00:59:99]Colon fraction\n[00:99.1]Invalid seconds\n"
        "Unstamped text\n[00:20.12]<00:20.12>Timed <00:21.00>words"
    ) == (
        LyricsLine(1, "Earlier line"),
        LyricsLine(2.125, "Repeated test line"),
        LyricsLine(12.5, "Repeated test line"),
        LyricsLine(20.12, "Timed words"),
        LyricsLine(59.99, "Colon fraction"),
    )


@pytest.mark.parametrize("offset,expected", [(500, 1.5), (-500, 2.5), (5000, 0)])
def test_lrc_global_offset_is_milliseconds_even_after_timed_lines(offset: int, expected: float) -> None:
    assert parse_lrc(f"[00:02]Test line\n[offset:{offset}]") == (LyricsLine(expected, "Test line"),)


def test_lrc_duplicate_timestamps_merge_and_preserve_blank_instrumental_gaps() -> None:
    assert parse_lrc("[00:01]One\n[00:01]One\n[00:01]Translation\n[00:03]\n[00:05]End") == (
        LyricsLine(1, "One\nTranslation"),
        LyricsLine(3, ""),
        LyricsLine(5, "End"),
    )


def test_lrc_strips_terminal_escape_sequences_and_does_not_consume_literal_timestamp() -> None:
    assert parse_lrc("\ufeff[00:01]\x1b[31mText\x1b[0m [00:03] remains\x07") == (
        LyricsLine(1, "Text [00:03] remains"),
    )


def test_document_round_trip_and_current_line_follow_backward_seek_and_pause() -> None:
    doc = LyricsDocument(
        "Title", "Artist", "First\nNext", (LyricsLine(2, "First"), LyricsLine(5, "Next")), "Test"
    )
    restored = LyricsDocument.from_dict(doc.to_dict())
    assert restored == doc
    assert [doc.current_index(t) for t in [0, 2, 5, 500, 3, 3, -1, math.nan, math.inf]] == [
        None,
        0,
        1,
        1,
        0,
        0,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize("timestamp", [-1, math.nan, math.inf, True, "5", 10**1000])
def test_deserialization_rejects_invalid_timestamps(timestamp: object) -> None:
    with pytest.raises(ValueError):
        LyricsDocument.from_dict({"synced": [{"time": timestamp, "text": "Test"}]})


def test_exact_lookup_uses_signature_user_agent_timeout_and_returns_synced() -> None:
    opener = Mock(return_value=Response(record()))
    lyrics, provider = service(opener)
    doc = lyrics.get(TRACK)
    assert doc is not None and doc.source == "LRCLIB"
    assert doc.current_index(7.5) == 1
    assert len(doc.synced) == 2
    request = opener.call_args.args[0]
    assert parse_qs(urlparse(request.full_url).query) == {
        "track_name": ["Synthetic Song"],
        "artist_name": ["Test Artist"],
        "album_name": ["Synthetic Album"],
        "duration": ["120"],
    }
    assert "Myne CLI" in request.get_header("User-agent")
    assert opener.call_args.kwargs["timeout"] <= 8
    provider.lyrics.assert_not_called()


def test_search_uses_artist_and_title_then_rejects_wrong_artist_title_and_recording_duration() -> None:
    opener = Mock(
        side_effect=[
            missing(),
            Response(
                [
                    record(artistName="Unrelated Artist"),
                    record(trackName="Other Song"),
                    record(duration=200),
                    record(duration=119),
                ]
            ),
        ]
    )
    lyrics, _ = service(opener)
    doc = lyrics.get(TRACK)
    assert doc is not None and len(doc.synced) == 2
    request = opener.call_args.args[0]
    assert urlparse(request.full_url).path == "/api/search"
    assert parse_qs(urlparse(request.full_url).query) == {
        "track_name": ["Synthetic Song"],
        "artist_name": ["Test Artist"],
    }


def test_search_prefers_synced_version_to_plain_version_and_drops_album_filter() -> None:
    opener = Mock(
        side_effect=[
            Response(record(syncedLyrics=None)),
            Response(
                [
                    record(syncedLyrics=None),
                    record(albumName="Another Album"),
                ]
            ),
        ]
    )
    lyrics, _ = service(opener)
    result = lyrics.get(TRACK)
    assert result is not None and result.synced
    assert "album_name" not in opener.call_args.args[0].full_url


def test_plain_exact_result_survives_search_failure() -> None:
    opener = Mock(side_effect=[Response(record(syncedLyrics=None)), OSError("private diagnostic")])
    lyrics, provider = service(opener)
    result = lyrics.get(TRACK)
    assert result is not None and result.plain.startswith("First") and not result.synced
    provider.lyrics.assert_not_called()


def test_instrumental_is_distinct_from_a_missing_result() -> None:
    opener = Mock(return_value=Response(record(instrumental=True, syncedLyrics=None, plainLyrics=None)))
    lyrics, provider = service(opener)
    doc = lyrics.get(TRACK)
    assert doc is not None and doc.instrumental and not doc.plain and not doc.synced
    assert opener.call_count == 1
    provider.lyrics.assert_not_called()


@pytest.mark.parametrize("failure", [OSError("token=private"), ValueError("broken json")])
def test_network_failure_uses_provider_plain_fallback_without_exposing_error(failure: Exception) -> None:
    opener = Mock(side_effect=failure)
    lyrics, provider = service(opener)
    provider.lyrics.return_value = "A provider's synthetic test line."
    doc = lyrics.get(TRACK)
    assert doc is not None and doc.source == "YouTube Music"
    assert "private" not in json.dumps(doc.to_dict())
    assert opener.call_count == 1


def test_no_artist_skips_network_and_uses_provider() -> None:
    opener = Mock()
    lyrics, provider = service(opener)
    provider.lyrics.return_value = "Plain test line"
    assert lyrics.get(Track("id", "Unknown artist")) is not None
    opener.assert_not_called()


def test_local_filename_metadata_and_youtube_presentation_tags() -> None:
    opener = Mock(side_effect=[Response(record()), Response(record())])
    lyrics, _ = service(opener)
    assert lyrics.get(Track("/tmp/file.wav", "Test Artist - Synthetic Song", provider="local")) is not None
    assert lyrics.get(Track("video", "Synthetic Song (Official Audio)", ("Test Artist - Topic",))) is not None


def test_no_lyrics_negative_result_is_cached_and_refresh_bypasses_it() -> None:
    opener = Mock(side_effect=[missing(), Response([]), Response(record())])
    lyrics, _ = service(opener)
    assert lyrics.get(TRACK) is None
    assert lyrics.get(TRACK) is None
    assert opener.call_count == 2
    assert lyrics.get(TRACK, refresh=True) is not None
    assert opener.call_count == 3


def test_positive_cache_survives_new_service_instance_is_private_and_expires(tmp_path: Path) -> None:
    clock = Mock(return_value=1000)
    opener = Mock(side_effect=[Response(record()), Response(record())])
    lyrics, _ = service(opener, tmp_path, clock)
    doc = lyrics.get(TRACK)
    assert lyrics.get(TRACK) == doc
    cached_file = next((tmp_path / "lyrics").glob("*.json"))
    assert stat.S_IMODE(cached_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(cached_file.parent.stat().st_mode) == 0o700
    offline = Mock(side_effect=AssertionError("Should use cached lyrics"))
    second, _ = service(offline, tmp_path, clock)
    assert second.get(TRACK) == doc
    offline.assert_not_called()
    clock.return_value = 1000 + 7 * 86400 + 1
    assert lyrics.get(TRACK) is not None
    assert opener.call_count == 2


def test_missing_results_expire_after_fifteen_minutes_and_failures_retry_sooner() -> None:
    clock = Mock(return_value=1000)
    opener = Mock(side_effect=[missing(), Response([]), OSError(), Response(record())])
    lyrics, _ = service(opener, clock=clock)
    assert lyrics.get(TRACK) is None
    clock.return_value = 1899
    assert lyrics.get(TRACK) is None
    assert opener.call_count == 2
    clock.return_value = 1901
    assert lyrics.get(TRACK) is None
    assert opener.call_count == 3
    clock.return_value = 1962
    assert lyrics.get(TRACK) is not None


def test_rate_limit_honors_retry_after_across_tracks_even_with_refresh() -> None:
    clock = Mock(return_value=1000)
    throttled = HTTPError("https://lrclib.net", 429, "Rate limited", {"Retry-After": "120"}, None)
    opener = Mock(side_effect=[throttled, Response(record())])
    lyrics, _ = service(opener, clock=clock)
    assert lyrics.get(TRACK) is None
    clock.return_value = 1119
    assert lyrics.get(Track("another", TRACK.title, TRACK.artists), refresh=True) is None
    assert opener.call_count == 1
    clock.return_value = 1140
    assert lyrics.get(TRACK, refresh=True) is not None


def test_oversized_and_non_json_response_fall_back_without_crashing() -> None:
    for response in [Response("x" * 1_048_577), Response("<html>error</html>", "text/html")]:
        lyrics, provider = service(Mock(return_value=response))
        provider.lyrics.return_value = "A synthetic fallback line"
        doc = lyrics.get(TRACK)
        assert doc is not None and doc.source == "YouTube Music"


def test_corrupt_disk_cache_does_not_prevent_fetching(tmp_path: Path) -> None:
    lyrics, _ = service(Mock(return_value=Response(record())), tmp_path)
    assert lyrics.get(TRACK) is not None
    next((tmp_path / "lyrics").glob("*.json")).write_text('{"document":')
    opener = Mock(return_value=Response(record()))
    another, _ = service(opener, tmp_path)
    assert another.get(TRACK) is not None
    assert opener.call_count == 1


def test_unwritable_cache_is_not_a_playback_failure(tmp_path: Path) -> None:
    cache_file = tmp_path / "not-a-directory"
    cache_file.touch()
    lyrics, _ = service(Mock(return_value=Response(record())), cache_file)
    assert lyrics.get(TRACK) is not None


def test_concurrent_lookups_only_fetch_once() -> None:
    opener = Mock(return_value=Response(record()))
    lyrics, _ = service(opener)
    with ThreadPoolExecutor(max_workers=5) as pool:
        docs = list(pool.map(lyrics.get, [TRACK] * 8))
    assert all(doc is not None and doc == docs[0] for doc in docs)
    assert opener.call_count == 1


def test_provider_failure_is_sanitized_and_not_propagated() -> None:
    lyrics, provider = service(Mock(side_effect=OSError("secret")))
    provider.lyrics.side_effect = RuntimeError("Authorization: private")
    assert lyrics.get(TRACK) is None


def test_malformed_record_duration_and_offset_are_bounded() -> None:
    opener = Mock(side_effect=[Response(record(duration=10**1000)), Response([])])
    lyrics, _ = service(opener)
    assert lyrics.get(TRACK) is None
    assert parse_lrc("[offset:" + "9" * 5000 + "]\n[00:01]Test") == (LyricsLine(1, "Test"),)


def test_memory_cache_is_bounded_and_search_only_examines_twenty_candidates() -> None:
    opener = Mock(side_effect=[missing(), Response([record(trackName="Wrong")] * 20 + [record()])])
    lyrics, _ = service(opener)
    assert lyrics.get(TRACK) is None
    provider_track = Track("id", "No artist")
    for index in range(140):
        lyrics.get(Track(str(index), provider_track.title))
    assert len(lyrics._memory) == 128
