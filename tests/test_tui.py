"""Headless interaction tests for the real Textual interface."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import DataTable, Input, Select, Static, Switch, TabbedContent

from myne.errors import MyneError
from myne.lyrics import LyricsDocument, LyricsLine
from myne.models import Playlist, Track
from myne.playlists import ImportResult
from myne.preferences import Preferences
from myne.tui import DetailScreen, MyneApp, _clock, _meter
from myne.updates import UpdateInfo


class FakeApplication:
    def __init__(self) -> None:
        self.tracks = [
            Track("first", "Numb [live]", ("Linkin Park",), 185),
            Track("second", "In the End", ("Linkin Park",), 216),
        ]
        self.up_next: list[Track] = []
        self.current: Track | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.paused = False
        self.search_gate: threading.Event | None = None
        self.search_started = threading.Event()
        self.position = 12.5
        self.playing = True
        self.volume = 75
        self.muted = False
        self.audio_output: str | None = "pulse"
        self.repeat = "off"
        self.liked: list[Track] = []
        self.recent: list[Track] = []
        self.saved: dict[str, list[Track]] = {}
        self.saved_sources: dict[str, Playlist] = {}
        self.import_gate: threading.Event | None = None
        self.import_started = threading.Event()
        self.import_error: str | None = None
        self.theme_name = "myne-studio"
        self.user_preferences = Preferences()
        self.update_info = UpdateInfo("0.4.0", install_note="Test installation")
        self.update_error: str | None = None
        self.install_gate: threading.Event | None = None
        self.install_started = threading.Event()
        self.playlist_gate: threading.Event | None = None
        self.playlist_started = threading.Event()
        self.document: LyricsDocument | None = LyricsDocument(
            "Test song", "Test artist", "A lyric with [literal brackets].\nAnother line.", (), "Test lyrics"
        )
        self.lyrics_error: str | None = None
        self.lyrics_gate: threading.Event | None = None
        self.lyrics_started = threading.Event()
        self.levels_gate: threading.Event | None = None
        self.levels_started = threading.Event()
        self.levels_calls = 0
        self.levels: dict[str, Any] = {"available": True, "rms": [0.1, 0.2], "peak": [0.3, 0.4]}

    def search(self, query: str, limit: int = 20) -> list[Track]:
        self.calls.append(("search", query, limit))
        self.search_started.set()
        if self.search_gate is not None:
            if not self.search_gate.wait(3):
                raise RuntimeError("test search timeout")
        if query == "offline":
            raise RuntimeError("Provider offline [retry]")
        return self.tracks

    def play(self, tracks: list[Track]) -> dict[str, Any]:
        self.calls.append(("play", tracks))
        self.current = tracks[0]
        self.up_next = [*tracks[1:], *self.up_next]
        return self.now()

    def play_queued(self, index: int, expected: Track) -> dict[str, Any]:
        self.calls.append(("play_queued", index, expected))
        assert self.up_next[index] == expected
        self.current = self.up_next[index]
        self.up_next = self.up_next[index + 1 :]
        return self.now()

    def enqueue(self, tracks: list[Track]) -> dict[str, Any]:
        self.calls.append(("enqueue", tracks))
        self.up_next.extend(tracks)
        return {}

    def queue(self) -> list[Track]:
        return list(self.up_next)

    def now(self) -> dict[str, Any]:
        return {
            "track": self.current.to_dict() if self.current else None,
            "paused": self.paused,
            "position": self.position if self.current else 0,
            "playing": bool(self.current) and self.playing,
            "duration": self.current.duration if self.current else None,
            "volume": self.volume,
            "muted": self.muted,
            "audio_output": self.audio_output,
            "radio": False,
            "repeat": self.repeat,
            "error": None,
        }

    def control(self, action: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("control", action))
        if action in {"pause", "resume"}:
            self.paused = action == "pause"
        elif action == "volume":
            self.volume = max(0, min(100, payload.get("value", self.volume + payload.get("delta", 0))))
            if self.volume:
                self.muted = False
        elif action == "mute":
            self.muted = payload.get("value", not self.muted)
        elif action == "repeat":
            modes = ("off", "all", "one")
            self.repeat = modes[(modes.index(self.repeat) + 1) % 3]
        elif action == "seek":
            self.position += payload["seconds"]
        elif action == "stop":
            self.current = None
        return self.now()

    def favorites(self) -> list[Track]:
        return list(self.liked)

    def toggle_favorite(self, track: Track) -> bool:
        if track in self.liked:
            self.liked.remove(track)
            return False
        self.liked.insert(0, track)
        return True

    def recent_tracks(self) -> list[Track]:
        return list(self.recent)

    def ui_theme(self) -> str:
        return self.theme_name

    def save_ui_theme(self, name: str) -> None:
        self.theme_name = name

    def preferences(self) -> Preferences:
        return replace(self.user_preferences, theme=self.theme_name, volume=self.volume)

    def save_preferences(self, values) -> Preferences:
        self.user_preferences = Preferences.from_dict(values, strict=True)
        self.theme_name = self.user_preferences.theme
        self.volume = self.user_preferences.volume
        return self.user_preferences

    def update_status(self) -> UpdateInfo:
        return self.update_info

    def check_update(self, force=True) -> UpdateInfo:
        self.calls.append(("check_update", force))
        if self.update_error:
            raise MyneError(self.update_error)
        self.update_info = UpdateInfo("0.4.0", "0.4.1", "a" * 40, True, True, "MYNE 0.4.1 is available.", "Test installation")
        return self.update_info

    def install_update(self, progress=None) -> UpdateInfo:
        self.calls.append(("install_update",))
        self.install_started.set()
        if progress:
            progress("Preparing update…")
        if self.install_gate:
            self.install_gate.wait(3)
        if self.update_error:
            raise MyneError(self.update_error)
        self.update_info = replace(self.update_info, can_install=False, message="Update installed. Restart MYNE.")
        return self.update_info

    def playlists(self) -> list[tuple[str, int]]:
        return [(name, len(tracks)) for name, tracks in sorted(self.saved.items())]

    def save_playlist(self, name: str) -> int:
        self.saved[name] = list(self.up_next)
        return len(self.up_next)

    def restore_playlist(self, name: str) -> dict[str, Any]:
        self.up_next = list(self.saved[name])
        return {"restored": len(self.up_next)}

    def playlist(self, name: str) -> Playlist:
        self.playlist_started.set()
        if self.playlist_gate:
            self.playlist_gate.wait(3)
        return self.saved_sources.get(name, Playlist(name, tuple(self.saved[name]), "Saved queue"))

    def import_playlist(self, link, name=None, *, progress=None, cancel=None) -> ImportResult:
        self.calls.append(("import_playlist", link, name))
        self.import_started.set()
        if progress:
            progress(1, 2, "Matching sample song")
        if self.import_gate is not None:
            self.import_gate.wait(3)
        if cancel and cancel.is_set():
            raise MyneError("Import cancelled.")
        if self.import_error:
            raise MyneError(self.import_error)
        playlist = Playlist(name or "Imported songs", tuple(self.tracks), "Spotify", link,
                            ("Spotify public view may be partial.",))
        self.saved[playlist.name] = list(playlist.tracks)
        self.saved_sources[playlist.name] = playlist
        return ImportResult(playlist, len(self.tracks))

    def queue_remove(self, index: int, expected: Track) -> dict[str, Any]:
        assert self.up_next[index] == expected
        self.up_next.pop(index)
        return {"removed": 1}

    def queue_move(self, index: int, step: int, expected: Track) -> dict[str, Any]:
        assert self.up_next[index] == expected
        target = max(0, min(len(self.up_next) - 1, index + step))
        self.up_next[index], self.up_next[target] = self.up_next[target], self.up_next[index]
        return {"queue_index": target}

    def queue_shuffle(self) -> dict[str, Any]:
        self.up_next.reverse()
        return {"shuffled": len(self.up_next)}

    def mix(self, mood: str, limit: int = 20, ai: bool = False) -> list[Track]:
        self.calls.append(("mix", mood, limit, ai))
        return self.tracks

    def radio(self, query: str) -> dict[str, Any]:
        self.calls.append(("radio", query))
        return {}

    def lyrics(self) -> str:
        return "A lyric with [literal brackets].\nAnother line."

    def lyrics_document(self, track: Track | None = None, refresh: bool = False) -> LyricsDocument | None:
        self.calls.append(("lyrics_document", track))
        self.lyrics_started.set()
        document = self.document
        if self.lyrics_gate is not None:
            if not self.lyrics_gate.wait(3):
                raise RuntimeError("test lyrics timeout")
        if self.lyrics_error:
            raise RuntimeError(self.lyrics_error)
        return document

    def visualizer(self, enabled: bool = True) -> dict[str, Any]:
        self.calls.append(("visualizer", enabled))
        return {"enabled": enabled}

    def audio_levels(self) -> dict[str, Any]:
        self.levels_calls += 1
        self.levels_started.set()
        if self.levels_gate is not None:
            if not self.levels_gate.wait(3):
                raise RuntimeError("test levels timeout")
        return dict(self.levels)

    def stats(self) -> dict[str, Any]:
        return {
            "listening_seconds": 123,
            "plays": 2,
            "artists": [("Linkin Park", 2)],
            "songs": [("Numb", 2)],
            "genres": [],
        }


async def settle(app: MyneApp) -> None:
    await app.workers.wait_for_complete()
    await asyncio.sleep(0.03)


def test_search_select_enqueue_and_play() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#search", Input).value = "Linkin Park"
            await pilot.press("enter")
            await settle(app)
            assert app.query_one("#results", DataTable).row_count == 2
            assert app.focused is app.query_one("#results")
            await pilot.press("down", "a")
            await settle(app)
            assert service.up_next == [service.tracks[1]]
            await pilot.press("enter")
            await settle(app)
            assert service.current == service.tracks[1]
            app.refresh_playback()
            await settle(app)
            assert "In the End" in str(app.query_one("#now", Static).render())
            await pilot.press("space")
            await settle(app)
            assert service.paused is True
            app.refresh_playback()
            await settle(app)
            await pilot.press("space")
            await settle(app)
            assert service.paused is False

    asyncio.run(scenario())


def test_playing_a_queued_selection_keeps_only_the_existing_tail() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        tail = Track("last", "Last track")
        service.up_next = [*service.tracks, tail]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.query_one("#queue", DataTable).focus()
            await pilot.press("down", "enter")
            await settle(app)
            assert ("play_queued", 1, service.tracks[1]) in service.calls
            assert service.current == service.tracks[1]
            assert service.up_next == [tail]

    asyncio.run(scenario())


def test_space_starts_the_saved_queue_when_the_player_is_idle() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.up_next = list(service.tracks)
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.query_one("#queue", DataTable).focus()
            await pilot.press("space")
            await settle(app)
            assert ("control", "resume") in service.calls
            assert ("control", "pause") not in service.calls

    asyncio.run(scenario())


def test_sound_controls_recover_zero_volume_and_toggle_mute() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.volume = 0
        service.muted = True
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            assert "Muted" in str(app.query_one("#volume-label", Static).render())
            app.query_one("#results", DataTable).focus()
            await pilot.press("]")
            await settle(app)
            assert service.volume == 5
            assert service.muted is False
            assert "5%" in str(app.query_one("#volume-label", Static).render())
            await pilot.click("#mute")
            await settle(app)
            assert service.muted is True
            await pilot.press("m")
            await settle(app)
            assert service.muted is False

    asyncio.run(scenario())


def test_loading_and_silent_output_are_visible() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.playing = False
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)):
            await settle(app)
            assert "Loading" in str(app.query_one("#now", Static).render())
            service.playing = True
            service.audio_output = "null"
            app._show_playback(service.now())
            assert "silent" in str(app.query_one("#audio-output", Static).render()).lower()

    asyncio.run(scenario())


def test_provider_error_keeps_interface_usable_and_metadata_literal() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.tracks[0] = Track("odd", "[link=bad]literal[/link]", ("[red]Artist",))
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            app.search_music("first")
            await settle(app)
            title = app.query_one("#results", DataTable).get_row_at(0)[0]
            assert isinstance(title, Text)
            assert title.plain == "[link=bad]literal[/link]"
            assert not title.spans
            app.search_music("offline")
            await settle(app)
            status = app.query_one("#status", Static)
            assert "Provider offline [retry]" in str(status.render())
            assert status.has_class("error")
            await pilot.press("ctrl+f")
            app.query_one("#search", Input).value = "try again"
            await pilot.press("enter")
            await settle(app)
            assert app.query_one("#results", DataTable).row_count == 2
            assert not status.has_class("error")

    asyncio.run(scenario())


def test_layout_and_help_fit_standard_terminal() -> None:
    async def scenario() -> None:
        app = MyneApp(FakeApplication())  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            assert app.query_one("#results").region.width >= 30
            assert app.query_one("#queue").region.width >= 20
            player = app.query_one("#player").region
            assert player.bottom <= 23
            assert app.query_one("#next").region.bottom <= player.bottom
            assert app.query_one("#search").region.y >= 0
            app.action_help()
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert "My Music. My Queue. My Experience." in str(
                app.screen.query_one("#detail-body", Static).render()
            )
            await pilot.press("escape")
            assert not isinstance(app.screen, DetailScreen)

    asyncio.run(scenario())


def test_mix_radio_and_lyrics_actions() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            app.search_music("/mix focus")
            await settle(app)
            assert ("mix", "focus", 20, False) in service.calls
            app.search_music("/mix-ai night drive")
            await settle(app)
            assert ("mix", "night drive", 20, True) in service.calls
            app.search_music("/radio Numb")
            await settle(app)
            assert ("radio", "Numb") in service.calls
            service.current = service.tracks[0]
            app.refresh_playback()
            await settle(app)
            app.action_lyrics()
            await settle(app)
            assert app.query_one("#lyrics-pane").display
            assert "[literal brackets]" in str(app.query_one("#lyrics-body", Static).render())
            assert (
                not app.query_one("#results-pane").display or app.query_one("#results-pane").region.width == 0
            )
            await pilot.press("l")
            assert not app.query_one("#lyrics-pane").display
            app.action_stats()
            await settle(app)
            content = str(app.screen.query_one("#detail-body", Static).render())
            assert "MOST PLAYED ARTISTS" in content
            assert "Linkin Park · 2 plays" in content
            assert "No genre metadata available yet." in content
            await pilot.press("escape")

    asyncio.run(scenario())


def test_slow_search_does_not_block_keyboard() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.search_gate = threading.Event()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            app.search_music("slow")
            try:
                assert await asyncio.to_thread(service.search_started.wait, 1)
                await pilot.press("ctrl+f", "n", "e", "w")
                assert app.query_one("#search", Input).value == "new"
            finally:
                service.search_gate.set()
            await settle(app)

    asyncio.run(scenario())


def test_duration_format_handles_unknown_and_long_tracks() -> None:
    assert _clock(None) == "—:——"
    assert _clock(float("nan")) == "—:——"
    assert _clock(-5) == "0:00"
    assert _clock(216) == "3:36"
    assert _clock(3672) == "1:01:12"


def test_synced_lyrics_follow_clock_pause_and_backwards_seek() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.paused = True
        service.document = LyricsDocument(
            "Test song",
            "Test artist",
            "",
            (
                LyricsLine(1.0, "First original sample line"),
                LyricsLine(10.0, "Second original sample line"),
                LyricsLine(20.0, "Third original sample line"),
            ),
            "LRCLIB (sample)",
        )
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(120, 36)):
            await settle(app)
            assert app.query_one("#lyrics-pane").display
            assert app._lyric_index == 1
            rendered = app.query_one("#lyrics-body", Static).render()
            assert "› Second original sample line" in str(rendered)
            assert "LRCLIB (sample) · synced" in str(app.query_one("#lyrics-source", Static).render())
            service.position = 21
            app.refresh_playback()
            await settle(app)
            assert app._lyric_index == 2
            service.position = 2
            app.refresh_playback()
            await settle(app)
            assert app._lyric_index == 0
            app._snapshot.update(position=9.5, paused=False, playing=True)
            app._snapshot_at = time.monotonic() - 0.8
            app.tick_playback()
            assert app._lyric_index == 1
            app._snapshot["paused"] = True
            app.tick_playback()
            assert app._lyric_index == 0
            app._snapshot.update(position=0.0, paused=True)
            app.tick_playback()
            assert app._lyric_index == -1

    asyncio.run(scenario())


def test_slow_lyrics_cannot_replace_new_track_and_keep_keyboard_responsive() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.lyrics_gate = threading.Event()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                assert await asyncio.to_thread(service.lyrics_started.wait, 1)
                service.current = service.tracks[1]
                service.document = LyricsDocument("New", "Artist", "New track lyrics", (), "New source")
                app._show_playback(service.now())
                await pilot.press("ctrl+f", "s", "t", "i", "l", "l")
                assert app.query_one("#search", Input).value == "still"
                assert "Finding lyrics" in str(app.query_one("#lyrics-body", Static).render())
            finally:
                service.lyrics_gate.set()
            await settle(app)
            assert "New track lyrics" in str(app.query_one("#lyrics-body", Static).render())
            assert "literal brackets" not in str(app.query_one("#lyrics-body", Static).render())
            assert len([call for call in service.calls if call[0] == "lyrics_document"]) == 2

    asyncio.run(scenario())


def test_lyrics_errors_missing_and_instrumental_are_explicit() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.lyrics_error = "Lyrics server offline [retry]"
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)):
            await settle(app)
            app.action_lyrics()
            rendered = str(app.query_one("#lyrics-body", Static).render())
            assert "Lyrics unavailable right now" in rendered
            assert "[retry]" in rendered
            service.lyrics_error = None
            service.document = None
            service.current = service.tracks[1]
            app._show_playback(service.now())
            await settle(app)
            assert "No lyrics found" in str(app.query_one("#lyrics-body", Static).render())
            service.document = LyricsDocument("Song", "Artist", "", (), "LRCLIB", instrumental=True)
            service.current = service.tracks[0]
            app._show_playback(service.now())
            await settle(app)
            assert "Instrumental" in str(app.query_one("#lyrics-body", Static).render())
            service.current = None
            app._show_playback(service.now())
            assert "Play a track" in str(app.query_one("#lyrics-body", Static).render())

    asyncio.run(scenario())


def test_real_level_meter_pause_unavailable_and_toggle() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.sample_audio()
            await settle(app)
            meter = app.query_one("#audio-meter", Static)
            assert "RMS HISTORY" in str(meter.render())
            assert "-54.0 dBFS" in str(meter.render())
            assert list(app._audio_history)[-1] == 0.2
            service.paused = True
            app._show_playback(service.now())
            assert "paused" in str(meter.render())
            count = len(app._audio_history)
            app.sample_audio()
            await settle(app)
            assert len(app._audio_history) == count
            service.paused = False
            service.levels = {"available": False, "error": "mpv audio filter unsupported"}
            app._show_playback(service.now())
            app.sample_audio()
            await settle(app)
            assert "unavailable" in str(meter.render())
            assert "unsupported" in str(meter.render())
            assert len(app._audio_history) == count
            await pilot.press("ctrl+f")
            app.query_one("#results", DataTable).focus()
            await pilot.press("v")
            await settle(app)
            assert not meter.display
            assert app.query_one("#player").region.height == 8
            assert ("visualizer", False) in service.calls
            await pilot.press("v")
            await settle(app)
            assert meter.display
            assert app.query_one("#player").region.height == 10
            assert ("visualizer", True) in service.calls

    asyncio.run(scenario())


def test_slow_audio_sampling_has_one_request_and_does_not_block_keyboard() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.levels_gate = threading.Event()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                assert await asyncio.to_thread(service.levels_started.wait, 1)
                for _ in range(20):
                    app.sample_audio()
                await pilot.press("ctrl+f", "s", "e", "a", "r", "c", "h")
                assert app.query_one("#search", Input).value == "search"
                assert service.levels_calls == 1
            finally:
                service.levels_gate.set()
            await settle(app)
            assert "RMS HISTORY" in str(app.query_one("#audio-meter", Static).render())

    asyncio.run(scenario())


def test_meter_does_not_logarithmize_normalized_decibels_twice() -> None:
    assert _meter(0.5, 0.0, 20).plain == "█" * 10 + "░" * 10
    assert _meter(0.0, 0.0, 20).plain == "░" * 20


def test_synced_lyrics_scroll_counts_wrapped_rows_in_compact_view() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.position = 100.0
        service.paused = True
        service.document = LyricsDocument(
            "Test song",
            "Test artist",
            "",
            tuple(
                LyricsLine(index * 10.0, f"Original sample line {index}. " + "A long sample phrase. " * 10)
                for index in range(20)
            ),
            "Sample lyrics",
        )
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.action_lyrics()
            await pilot.pause()
            assert app._lyric_index == 10
            # Ten logical rows become at least thirty wrapped terminal rows.
            assert app.query_one("#lyrics-scroll").scroll_y >= 25
            assert app.query_one("#player").region.bottom <= 23
            await pilot.press("l")
            await pilot.pause()
            assert app.query_one("#results").region.width >= 30
            assert app.query_one("#queue").region.width >= 20

    asyncio.run(scenario())


def test_subcell_meter_preserves_small_measured_changes() -> None:
    assert _meter(0.5125, 0.0, 20).plain == "█" * 10 + "▎" + "░" * 9
    assert len(_meter(0.999, 1.0, 20).plain) == 20


def test_animation_frames_do_not_repeat_telemetry_or_fabricate_history() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        app = MyneApp(service, reduced_motion=False)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)):
            await settle(app)
            app._audio_poll_timer.pause()
            await app.workers.wait_for_complete()
            count = service.levels_calls
            history = list(app._audio_history)
            for _ in range(60):
                app.tick_playback()
            assert service.levels_calls == count
            assert list(app._audio_history) == history
            service.paused = True
            app._show_playback(service.now())
            assert app._audio_motion.frame()["available"] is False
            assert "paused" in str(app.query_one("#audio-meter", Static).render())

    asyncio.run(scenario())


def test_lyrics_ease_on_line_advance_but_snap_after_seek() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        service.position = 1.8
        service.paused = True
        service.document = LyricsDocument(
            "Test",
            "Author",
            "",
            (
                LyricsLine(1.0, "Original first line"),
                LyricsLine(2.0, "Original second line"),
                LyricsLine(20.0, "Original third line"),
            ),
            "Fixture",
        )
        app = MyneApp(service, reduced_motion=False)  # type: ignore[arg-type]
        app.animation_level = "full"
        async with app.run_test(size=(120, 36)) as pilot:
            await settle(app)
            scroll = app.query_one("#lyrics-scroll")
            calls = []
            scroll.scroll_to = lambda **kwargs: calls.append(kwargs)
            app._snapshot.update(position=1.9, paused=False, playing=True)
            app._snapshot_at = time.monotonic()
            app.tick_playback()
            app._snapshot.update(position=2.01)
            app._snapshot_at = time.monotonic()
            app.tick_playback()
            await pilot.pause(0.04)
            assert calls[-1]["animate"] is True
            assert calls[-1]["duration"] == 0.22
            app._snapshot.update(position=20.1)
            app._snapshot_at = time.monotonic()
            app.tick_playback()
            await pilot.pause(0.04)
            assert calls[-1]["animate"] is False

    asyncio.run(scenario())


def test_reduced_motion_disables_envelopes_and_textual_effects(monkeypatch) -> None:
    monkeypatch.setenv("MYNE_REDUCED_MOTION", "1")
    app = MyneApp(FakeApplication())  # type: ignore[arg-type]
    assert app.animation_level == "none"
    assert app._audio_motion.reduced_motion is True


def test_liked_library_recent_history_and_theme_survive_reopening() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.recent = [service.tracks[1]]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            app.search_music("Linkin Park")
            await settle(app)
            await pilot.press("f")
            await settle(app)
            assert service.liked == [service.tracks[0]]
            await pilot.click("#tab-favorites")
            await settle(app)
            assert app.results == service.liked
            await pilot.click("#tab-recent")
            await settle(app)
            assert app.results == service.recent
            await pilot.click("#theme")
            await settle(app)
            assert service.theme_name == "myne-midnight"
        reopened = MyneApp(service)  # type: ignore[arg-type]
        async with reopened.run_test(size=(80, 24)):
            await settle(reopened)
            assert reopened.theme == "myne-midnight"
            reopened.action_favorites()
            await settle(reopened)
            assert reopened.results == service.liked
    asyncio.run(scenario())


def test_queue_toolbar_edits_saves_and_opens_named_playlist_without_changing_queue() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        first, second = service.tracks
        service.up_next = [first, second, Track("last", "Last")]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.query_one("#queue", DataTable).focus()
            await pilot.press("alt+down")
            await settle(app)
            assert service.up_next[:2] == [second, first]
            await pilot.click("#queue-remove")
            await settle(app)
            assert service.up_next == [second, Track("last", "Last")]
            await pilot.click("#queue-save")
            app.screen.query_one("#playlist-name", Input).value = "Evening [live]"
            await pilot.press("enter")
            await settle(app)
            assert service.saved["Evening [live]"] == service.up_next
            service.up_next = []
            await pilot.click("#tab-playlists")
            await settle(app)
            await pilot.press("enter")
            await settle(app)
            assert app._open_playlist.name == "Evening [live]"
            assert app.results == service.saved["Evening [live]"]
            assert service.up_next == []
            await pilot.click("#playlist-queue")
            await settle(app)
            assert service.up_next == service.saved["Evening [live]"]
            assert service.current is None
    asyncio.run(scenario())


def test_command_palette_opens_and_executes_library_action() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.liked = [service.tracks[0]]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app)
            await pilot.press("ctrl+k")
            app.screen.query_one(Input).value = "Liked tracks"
            await pilot.pause(0.3)
            await pilot.press("enter")
            await settle(app)
            assert app._view == "favorites"
            assert app.results == service.liked
    asyncio.run(scenario())


def test_seek_repeat_and_stop_controls_change_player_state() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = service.tracks[0]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            await pilot.click("#seek-forward")
            await settle(app)
            assert service.position == 22.5
            await pilot.click("#repeat")
            await settle(app)
            assert app._snapshot["repeat"] == "all"
            await pilot.click("#stop")
            await settle(app)
            assert service.current is None
            assert app.query_one("#seek-forward").disabled
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (144, 42)])
def test_studio_layout_keeps_player_visible_at_terminal_sizes(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = MyneApp(FakeApplication())  # type: ignore[arg-type]
        async with app.run_test(size=size):
            await settle(app)
            assert app.query_one("#player").region.bottom <= size[1] - 1
            assert app.query_one("#stop").region.right <= size[0]
            assert app.query_one("#sidebar").display is (size[0] >= 100)
            assert app.query_one("#results").region.width >= 30
    asyncio.run(scenario())


def test_sidebar_can_toggle_on_small_terminal_and_open_saved_playlists() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.saved = {"My [playlist]": service.tracks}
        service.up_next = [Track("queued", "Keep queued")]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            assert not app.query_one("#sidebar").display
            await pilot.press("ctrl+b")
            assert app.query_one("#sidebar").display
            assert not app.query_one("#queue-pane").display
            assert app.query_one("#results").region.right <= 80
            sidebar = app.query_one("#sidebar-playlists")
            sidebar.focus()
            await pilot.press("enter")
            await settle(app)
            assert app._open_playlist.name == "My [playlist]"
            assert app.results == service.tracks
            assert service.up_next == [Track("queued", "Keep queued")]
            await pilot.press("ctrl+b")
            assert app.query_one("#queue-pane").display
            assert not app.query_one("#sidebar").display
            await pilot.click("#playlist-queue")
            await settle(app)
            assert service.up_next == [Track("queued", "Keep queued"), *service.tracks]
            await pilot.click("#playlist-play")
            await settle(app)
            assert service.current == service.tracks[0]
    asyncio.run(scenario())


def test_import_modal_saves_playlist_opens_tracks_and_keeps_playing_song() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.current = Track("current", "Keep playing")
        service.up_next = [Track("queued", "Keep queued")]
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app)
            await pilot.press("ctrl+o")
            app.screen.query_one("#import-link", Input).value = "https://open.spotify.com/playlist/" + "A" * 22
            app.screen.query_one("#import-name", Input).value = "Night [drive]"
            await pilot.click("#confirm-import")
            await settle(app)
            assert app._open_playlist.name == "Night [drive]"
            assert app.results == service.tracks
            assert app._sidebar_playlist_names == ["Night [drive]"]
            assert service.current.title == "Keep playing"
            assert service.up_next == [Track("queued", "Keep queued")]
            app.action_playlist_info()
            assert "https://open.spotify.com/playlist/" in app.screen.body
            assert "partial" in app.screen.body
    asyncio.run(scenario())


def test_pasting_playlist_link_in_search_opens_import_flow() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            link = "https://music.youtube.com/playlist?list=PLsample"
            app.query_one("#search", Input).value = link
            await pilot.press("enter")
            assert app.screen.query_one("#import-link", Input).value == link
            assert not any(call[0] == "search" for call in service.calls)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app._main_screen
    asyncio.run(scenario())


def test_import_errors_keep_modal_usable_and_do_not_mutate_playlists() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.import_error = "Private playlist. Use a public link."
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.action_import_playlist()
            await pilot.pause()
            app.screen.query_one("#import-link", Input).value = "https://example.com/invalid"
            await pilot.click("#confirm-import")
            assert not service.import_started.is_set()
            app.screen.query_one("#import-link", Input).value = "https://open.spotify.com/playlist/" + "A" * 22
            app.screen.query_one("#import-link", Input).focus()
            await pilot.press("enter")
            await settle(app)
            assert "Private playlist" in str(app.screen.query_one("#import-status", Static).render())
            assert not app.screen.query_one("#import-link", Input).disabled
            assert not service.saved
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app._main_screen
    asyncio.run(scenario())


def test_import_can_cancel_while_matching_without_saving() -> None:
    async def scenario() -> None:
        service = FakeApplication()
        service.import_gate = threading.Event()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.action_import_playlist()
            await pilot.pause()
            app.screen.query_one("#import-link", Input).value = "https://soundcloud.com/artist/sets/night"
            await pilot.click("#confirm-import")
            await asyncio.to_thread(service.import_started.wait, 1)
            assert "Matching" in str(app.screen.query_one("#import-status", Static).render())
            await pilot.press("escape")
            service.import_gate.set()
            await settle(app)
            assert app.screen is app._main_screen
            assert not service.saved
    asyncio.run(scenario())


def test_opening_a_different_playlist_clears_old_play_targets_while_loading():
    async def scenario():
        service = FakeApplication()
        service.saved = {"First": [service.tracks[0]], "Second": [service.tracks[1]]}
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(100, 30)):
            await settle(app)
            app.open_playlist("First")
            await settle(app)
            service.playlist_gate = threading.Event()
            service.playlist_started.clear()
            app.open_playlist("Second")
            try:
                assert await asyncio.to_thread(service.playlist_started.wait, 1)
                assert app._open_playlist is None
                assert app.results == []
                assert app.query_one("#playlist-play").disabled
                app.action_playlist_play()
                app.action_playlist_queue()
                assert not any(call[0] in {"play", "enqueue"} for call in service.calls)
            finally:
                service.playlist_gate.set()
            await settle(app)
            assert app.results == [service.tracks[1]]
            assert not app.query_one("#playlist-play").disabled
    asyncio.run(scenario())


def test_compact_lyrics_hide_queue_and_restore_both_track_panes():
    async def scenario():
        app = MyneApp(FakeApplication())  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            app.action_lyrics()
            await pilot.pause()
            assert not app.query_one("#results-pane").display
            assert not app.query_one("#queue-pane").display
            assert app.query_one("#lyrics-pane").region.right <= 80
            app.action_lyrics()
            await pilot.pause()
            assert app.query_one("#results-pane").display
            assert app.query_one("#queue-pane").display
    asyncio.run(scenario())


def test_settings_save_applies_and_survives_reopening(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("MYNE_REDUCED_MOTION", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    async def scenario():
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            await pilot.press("f2")
            await settle(app)
            screen = app.screen
            screen.query_one("#setting-theme", Select).value = "myne-midnight"
            screen.query_one("#setting-sidebar", Select).value = "show"
            screen.query_one("#setting-lyrics", Select).value = "hide"
            screen.query_one("#setting-visualizer", Switch).value = False
            screen.query_one("#setting-motion", Switch).value = True
            screen.query_one("#setting-volume", Input).value = "38"
            await pilot.click("#settings-save")
            await settle(app)
            assert app.screen is app._main_screen
            assert app.theme == "myne-midnight"
            assert app.query_one("#sidebar").display
            assert not app.query_one("#lyrics-pane").display
            assert not app.query_one("#audio-meter").display
            assert app._audio_motion.reduced_motion
            assert service.volume == 38
        reopened = MyneApp(service)  # type: ignore[arg-type]
        async with reopened.run_test(size=(80, 24)):
            await settle(reopened)
            assert reopened.theme == "myne-midnight"
            assert reopened.query_one("#sidebar").display
            assert not reopened._visualizer_enabled
            assert reopened._reduced_motion
    asyncio.run(scenario())


def test_settings_cancel_does_not_apply_edits_or_trigger_shortcuts():
    async def scenario():
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            await pilot.press("f2")
            await settle(app)
            screen = app.screen
            screen.query_one("#setting-theme", Select).value = "myne-ember"
            await pilot.press("ctrl+b", "ctrl+o", "f2")
            assert app.screen is screen
            assert len(app.screen_stack) == 2
            assert app.theme == "myne-studio"
            # Native Tab navigation must still work inside the modal.
            focused = app.focused
            await pilot.press("tab")
            assert app.focused is not focused
            await pilot.click("#settings-cancel")
            await pilot.pause()
            assert app.screen is app._main_screen
            assert service.theme_name == "myne-studio"
    asyncio.run(scenario())


def test_invalid_settings_volume_keeps_form_open():
    async def scenario():
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            await pilot.press("f2")
            await settle(app)
            app.screen.query_one("#setting-volume", Input).value = "999"
            await pilot.click("#settings-save")
            await settle(app)
            assert app.screen is not app._main_screen
            assert "0 to 100" in str(app.screen.query_one("#settings-message", Static).render())
            assert service.volume == 75
    asyncio.run(scenario())


def test_settings_manual_update_shows_progress_prevents_duplicate_install_and_reports_restart():
    async def scenario():
        service = FakeApplication()
        service.install_gate = threading.Event()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            await pilot.press("f2")
            await settle(app)
            app.screen.query_one("#settings-tabs", TabbedContent).active = "updates-tab"
            await pilot.pause()
            await pilot.click("#check-update")
            await settle(app)
            assert not app.screen.query_one("#install-update").disabled
            await pilot.click("#install-update")
            try:
                assert await asyncio.to_thread(service.install_started.wait, 1)
                assert app.screen.query_one("#install-update").disabled
                await pilot.press("escape", "ctrl+q")
                assert app.screen is not app._main_screen
                assert service.calls.count(("install_update",)) == 1
            finally:
                service.install_gate.set()
            await settle(app)
            assert "Restart MYNE" in str(app.screen.query_one("#update-progress", Static).render())
            assert app.screen.query_one("#install-update").disabled
    asyncio.run(scenario())


def test_update_failure_remains_visible_and_form_usable():
    async def scenario():
        service = FakeApplication()
        service.update_error = "Could not check for updates. Check your connection and retry."
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app)
            await pilot.press("f2")
            await settle(app)
            app.screen.query_one("#settings-tabs", TabbedContent).active = "updates-tab"
            await pilot.pause()
            await pilot.click("#check-update")
            await settle(app)
            assert "connection" in str(app.screen.query_one("#update-progress", Static).render())
            assert not app.screen.query_one("#check-update").disabled
            assert app.screen.query_one("#install-update").disabled
            assert not app.screen.query_one("#settings-save").disabled
    asyncio.run(scenario())


def test_automatic_update_check_is_opt_in_and_does_not_install():
    async def scenario():
        service = FakeApplication()
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)):
            await settle(app)
            assert not any(call[0] == "check_update" for call in service.calls)
        service.user_preferences = Preferences(auto_check_updates=True)
        reopened = MyneApp(service)  # type: ignore[arg-type]
        async with reopened.run_test(size=(80, 24)):
            await settle(reopened)
            assert ("check_update", False) in service.calls
            assert not any(call[0] == "install_update" for call in service.calls)
    asyncio.run(scenario())


def test_slow_preferences_do_not_overwrite_a_sidebar_choice_made_during_startup():
    async def scenario():
        service = FakeApplication()
        gate, started = threading.Event(), threading.Event()
        def preferences():
            started.set()
            gate.wait(3)
            return Preferences(theme="myne-ember")
        service.preferences = preferences
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                assert await asyncio.to_thread(started.wait, 1)
                await pilot.press("ctrl+b")
                assert app.query_one("#sidebar").display
            finally:
                gate.set()
            await settle(app)
            assert app.query_one("#sidebar").display
            assert app.theme == "myne-ember"
    asyncio.run(scenario())


def test_listening_room_controls_current_song_and_restores_library_selection():
    async def scenario():
        service = FakeApplication()
        service.current = service.tracks[0]
        app = MyneApp(service, reduced_motion=True)  # type: ignore[arg-type]
        async with app.run_test(size=(144, 42)) as pilot:
            await settle(app)
            app.search_music("test")
            await settle(app)
            table = app.query_one("#results", DataTable)
            table.move_cursor(row=1)
            table.focus()
            await pilot.pause()
            await pilot.press("f3")
            assert app.query_one("#listening-stage").display
            assert not app.query_one("#sidebar").display
            assert not app.query_one("#results-pane").display
            assert app.query_one("#lyrics-pane").display
            assert "Numb" in str(app.query_one("#listening-title", Static).render())
            await pilot.press("f", "space")
            await settle(app)
            assert service.liked == [service.tracks[0]]
            assert service.paused
            await pilot.press("f3")
            await pilot.pause()
            assert app.query_one("#sidebar").display
            assert app.query_one("#results-pane").display
            assert app._selected_result == 1
            assert table.cursor_row == 1
            assert service.current == service.tracks[0]
    asyncio.run(scenario())


def test_compact_listening_lyrics_do_not_change_saved_browse_preferences():
    async def scenario():
        service = FakeApplication()
        service.current = service.tracks[0]
        app = MyneApp(service, reduced_motion=True)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            before = app._preferences
            await pilot.press("f3")
            assert app.query_one("#listening-stage").display
            assert not app.query_one("#lyrics-pane").display
            await pilot.press("l")
            assert app.query_one("#lyrics-pane").display
            assert not app.query_one("#listening-stage").display
            assert app._preferences == before
            await pilot.press("f3")
            await pilot.pause()
            assert app.query_one("#results-pane").display
            assert not app.query_one("#lyrics-pane").display
            assert app.query_one("#player").region.bottom <= 23
            assert app.query_one("#stop").region.right <= 80
            await pilot.press("f3", "ctrl+f")
            assert not app._listening
            assert app.focused is app.query_one("#search")
    asyncio.run(scenario())


def test_listening_resize_reflows_without_refetch_or_playback_changes():
    async def scenario():
        service = FakeApplication()
        service.current = service.tracks[0]
        service.up_next = service.tracks * 4
        app = MyneApp(service, reduced_motion=True)  # type: ignore[arg-type]
        async with app.run_test(size=(144, 42)) as pilot:
            await settle(app)
            app.search_music("test")
            await settle(app)
            searches = [call for call in service.calls if call[0] == "search"]
            await pilot.press("f3")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#listening-stage").display
            assert not app.query_one("#lyrics-pane").display
            assert app.query_one("#stop").region.bottom <= 23
            await pilot.resize_terminal(144, 42)
            await pilot.pause()
            assert app.query_one("#lyrics-pane").display
            await pilot.press("f3")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.query_one("#results-pane").display
            assert not app.query_one("#results", DataTable).show_horizontal_scrollbar
            assert [call for call in service.calls if call[0] == "search"] == searches
            assert service.up_next == service.tracks * 4
            assert service.current == service.tracks[0]
    asyncio.run(scenario())


def test_settings_modal_blocks_listening_shortcut_and_collection_metadata_is_literal():
    async def scenario():
        service = FakeApplication()
        name = "[red]Literal\ncollection"
        service.saved[name] = service.tracks
        app = MyneApp(service)  # type: ignore[arg-type]
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app)
            app.open_playlist(name)
            await settle(app)
            title = app.query_one("#collection-title", Static)
            assert "[red]Literal collection" in str(title.render())
            assert "2 tracks" in str(app.query_one("#collection-meta", Static).render())
            await pilot.press("f2")
            await pilot.pause()
            screen = app.screen
            await pilot.press("f3")
            assert app.screen is screen
            assert not app._listening
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("f3")
            assert app._listening
    asyncio.run(scenario())
