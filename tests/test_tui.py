"""Headless interaction tests for the real Textual interface."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from myne.models import Track
from myne.tui import DetailScreen, MyneApp, _clock


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
            "position": 12.5 if self.current else 0,
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
            app.action_lyrics()
            await settle(app)
            assert isinstance(app.screen, DetailScreen)
            assert "[literal brackets]" in str(app.screen.query_one("#detail-body", Static).render())
            await pilot.press("escape")
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
