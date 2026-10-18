"""Headless interaction tests for the real Textual interface."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from myne.lyrics import LyricsDocument, LyricsLine
from myne.models import Track
from myne.tui import DetailScreen, MyneApp, _clock, _meter


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
        self.current, *self.up_next = tracks
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
            "volume": 75,
            "radio": False,
            "error": None,
        }

    def control(self, action: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("control", action))
        self.paused = action == "pause"
        return self.now()

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
            assert app.query_one("#player").region.height == 7
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
