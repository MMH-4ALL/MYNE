"""A keyboard-first Textual interface around the application service.

Provider, database and playback work stays off the event loop. Closing this
interface deliberately leaves the playback daemon and persistent queue running.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, ProgressBar, Static

from myne.models import Track

if TYPE_CHECKING:
    from myne.service import Application


def _plain(value: object) -> str:
    """Keep provider metadata literal and on one terminal line."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


def _clock(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "—:——"
    total = max(0, int(seconds))
    minutes, seconds_int = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds_int:02}" if hours else f"{minutes}:{seconds_int:02}"


def _stats_text(stats: dict[str, Any]) -> str:
    lines = [
        f"Total listening time: {_clock(float(stats.get('listening_seconds') or 0))}",
        f"Tracks played: {stats.get('plays', 0)}",
    ]
    for key, heading in (
        ("artists", "MOST PLAYED ARTISTS"),
        ("songs", "MOST PLAYED SONGS"),
        ("genres", "TOP GENRES"),
    ):
        lines.extend(("", heading))
        entries = stats.get(key) or []
        if entries:
            lines.extend(
                f"{index:2}. {_plain(name)} · {count} plays"
                for index, (name, count) in enumerate(entries, start=1)
            )
        else:
            lines.append(
                "No genre metadata available yet."
                if key == "genres"
                else "Your listening history will appear here."
            )
    return "\n".join(lines)


class DetailScreen(ModalScreen[None]):
    """Scrollable help, lyrics or listening statistics."""

    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; background: #070d12 85%; }
    DetailScreen > Vertical {
        width: 78; max-width: 94%; height: 85%;
        border: round #62d6bf; padding: 1 2; background: #111c25;
    }
    DetailScreen #detail-heading { height: 2; color: #62d6bf; text-style: bold; }
    DetailScreen VerticalScroll { height: 1fr; }
    DetailScreen #detail-body { height: auto; }
    DetailScreen Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.heading = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.heading, id="detail-heading", markup=False)
            with VerticalScroll():
                yield Static(self.body, id="detail-body", markup=False)
            yield Button("Close · Esc", id="close-detail")

    @on(Button.Pressed, "#close-detail")
    def close_detail(self) -> None:
        self.dismiss()


class MyneApp(App[None]):
    """Full-screen client; all music behavior belongs to ``Application``."""

    TITLE = "Myne CLI"
    SUB_TITLE = "My Music. My Queue. My Experience."
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #0b1219; color: #e7eef3; }
    #brand { height: 2; padding: 0 1; color: #62d6bf; text-style: bold; }
    #search { margin: 0 1; height: 3; border: tall #29404e; }
    #search:focus { border: tall #62d6bf; }
    #status { height: 1; margin: 0 2; color: #94a9b5; text-overflow: ellipsis; }
    #status.error { color: #ffa990; }
    #library { height: 1fr; min-height: 4; margin: 0 1; }
    .pane { border: round #29404e; padding: 0; }
    #results-pane { width: 2fr; min-width: 32; margin-right: 1; }
    #queue-pane { width: 1fr; min-width: 23; }
    DataTable { height: 1fr; background: #0b1219; }
    DataTable > .datatable--header { background: #172630; color: #94a9b5; }
    DataTable:focus > .datatable--cursor { background: #62d6bf; color: #0b1219; }
    DataTable > .datatable--cursor { background: #29404e; }
    .empty { height: 1; color: #94a9b5; padding-left: 1; }
    #player { height: 7; margin: 0 1; border: round #29404e; padding: 0 1; }
    #now { height: 1; text-style: bold; text-overflow: ellipsis; }
    #progress-row { height: 1; }
    #progress { width: 1fr; height: 1; padding: 0 1 0 0; }
    #time { width: 19; height: 1; text-align: right; color: #94a9b5; }
    #transport { height: 3; align-horizontal: center; }
    #transport Button { min-width: 9; width: 1fr; height: 3; margin: 0 1 0 0; }
    #transport #play { background: #1f524b; }
    Footer { background: #172630; }
    """
    BINDINGS = [
        Binding("ctrl+f", "search_focus", "Search", priority=True),
        Binding("a", "enqueue", "Add"),
        Binding("p", "play", "Play"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "next", "Next"),
        Binding("l", "lyrics", "Lyrics", show=False),
        Binding("s", "stats", "Stats", show=False),
        Binding("?", "help", "Help"),
        Binding("ctrl+q", "quit", "Exit", priority=True),
    ]

    def __init__(self, application: Application) -> None:
        super().__init__()
        self.service = application
        self.results: list[Track] = []
        self.queued: list[Track] = []
        self._snapshot: dict[str, Any] = {}
        self._refreshing = False
        self._mutation_lock = asyncio.Lock()
        self._selected_result = 0
        self._selected_queue = 0
        self._selection_source = "results"
        self._last_player_error: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("MYNE CLI\nMy Music. My Queue. My Experience.", id="brand", markup=False)
        yield Input(placeholder="Search songs · /mix focus · /radio Numb", id="search")
        yield Static("Search for your next track. Press ? for shortcuts.", id="status", markup=False)
        with Horizontal(id="library"):
            with Vertical(id="results-pane", classes="pane"):
                yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                yield Static(
                    "Your next favorite starts here.", id="results-empty", classes="empty", markup=False
                )
            with Vertical(id="queue-pane", classes="pane"):
                yield DataTable(id="queue", cursor_type="row")
                yield Static("Queue is empty.", id="queue-empty", classes="empty", markup=False)
        with Vertical(id="player"):
            yield Static("Nothing playing · search to begin", id="now", markup=False)
            with Horizontal(id="progress-row"):
                yield ProgressBar(total=100, show_eta=False, show_percentage=False, id="progress")
                yield Static("0:00 / —:——", id="time", markup=False)
            with Horizontal(id="transport"):
                yield Button("▶ Play", id="play")
                yield Button("+ Queue", id="add")
                yield Button("Ⅱ Pause", id="pause")
                yield Button("Next ›", id="next")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#results-pane").border_title = "DISCOVER"
        self.query_one("#queue-pane").border_title = "MY QUEUE · 0"
        self.query_one("#player").border_title = "NOW PLAYING"
        self.query_one("#results", DataTable).add_columns("Title", "Artist", "Time")
        self.query_one("#queue", DataTable).add_column("Up next")
        self.query_one("#search", Input).focus()
        self.refresh_playback()
        self.set_interval(1.0, self.refresh_playback)

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self.query_one("#status", Static)
        widget.update(_plain(message))
        widget.set_class(error, "error")

    @on(Input.Submitted, "#search")
    def submit_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self._status("Type a song, artist, /mix mood, or /radio song.")
            return
        self.search_music(query)

    @work(exclusive=True, group="search", exit_on_error=False)
    async def search_music(self, query: str) -> None:
        """Cancelling a search discards its result without blocking the UI."""
        self._status("Searching…")
        try:
            if query.startswith("/radio "):
                seed = query.removeprefix("/radio ").strip()
                if not seed:
                    self._status("Use /radio followed by a song or artist.", error=True)
                    return
                async with self._mutation_lock:
                    await asyncio.to_thread(self.service.radio, seed)
                self._status(f"Radio started: {seed}")
                self.refresh_playback()
                return
            if query.startswith(("/mix ", "/mix-ai ")):
                command, mood = query.split(" ", 1)
                tracks = await asyncio.to_thread(
                    self.service.mix, mood.strip(), limit=20, ai=command == "/mix-ai"
                )
                heading = f"MIX · {mood.strip()}"
            else:
                tracks = await asyncio.to_thread(self.service.search, query, limit=20)
                heading = "DISCOVER"
        except Exception as exc:
            self._status(f"Search failed: {exc}", error=True)
            return
        self.results = list(tracks)
        self._selected_result = 0
        self._selection_source = "results"
        table = self.query_one("#results", DataTable)
        table.clear()
        for index, track in enumerate(self.results):
            table.add_row(
                Text(_plain(track.title)),
                Text(_plain(", ".join(track.artists) or "Unknown artist")),
                Text(_clock(track.duration)),
                key=str(index),
            )
        # Border titles use Rich markup; untrusted moods are kept out of them.
        self.query_one("#results-pane").border_title = (
            "SMART MIX" if heading.startswith("MIX") else "DISCOVER"
        )
        self.query_one("#results-empty").display = not bool(self.results)
        self.query_one("#results-empty", Static).update("No tracks found. Try another search.")
        self._status(
            f"{len(self.results)} tracks · Enter plays · a adds to queue"
            if self.results
            else "No tracks found. Try another search."
        )
        if self.results:
            table.focus()

    @on(DataTable.RowHighlighted)
    def highlight_track(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "results":
            self._selected_result = event.cursor_row
        elif event.data_table.id == "queue":
            self._selected_queue = event.cursor_row

    @on(DescendantFocus)
    def remember_selection_source(self, event: DescendantFocus) -> None:
        # A transport button takes focus from its source pane before it fires.
        if isinstance(event.widget, DataTable) and event.widget.id in {"results", "queue"}:
            self._selection_source = event.widget.id

    @on(DataTable.RowSelected)
    def select_track(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self._selection_source = event.data_table.id or "results"
        self.action_play()

    def _selected(self) -> list[Track]:
        focused = self.focused
        source = focused.id if isinstance(focused, DataTable) else self._selection_source
        if source == "queue":
            self._selection_source = "queue"
            if 0 <= self._selected_queue < len(self.queued):
                # Keep the tail of the queue when starting a later queued track.
                return self.queued[self._selected_queue :]
            return []
        self._selection_source = "results"
        if 0 <= self._selected_result < len(self.results):
            return [self.results[self._selected_result]]
        return []

    def action_search_focus(self) -> None:
        self.query_one("#search", Input).focus()

    def action_play(self) -> None:
        tracks = self._selected()
        if tracks:
            self.mutate("Playing", self.service.play, tracks)
        else:
            self._status("Search and select a track first.")

    def action_enqueue(self) -> None:
        tracks = self._selected()
        if tracks:
            self.mutate("Added to queue", self.service.enqueue, tracks[:1])
        else:
            self._status("Search and select a track first.")

    def action_toggle_pause(self) -> None:
        action = "resume" if self._snapshot.get("paused") else "pause"
        self.mutate("Resumed" if action == "resume" else "Paused", self.service.control, action)

    def action_next(self) -> None:
        self.mutate("Skipped to next track", self.service.control, "next")

    @on(Button.Pressed)
    def press_transport(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "play": self.action_play,
            "add": self.action_enqueue,
            "pause": self.action_toggle_pause,
            "next": self.action_next,
        }
        if action := actions.get(event.button.id or ""):
            action()

    @work(group="mutations", exit_on_error=False)
    async def mutate(self, message: str, function: Callable[..., Any], *args: Any) -> None:
        try:
            async with self._mutation_lock:
                result = await asyncio.to_thread(function, *args)
            if isinstance(result, dict) and result.get("error"):
                self._status(str(result["error"]), error=True)
            else:
                self._status(message)
        except Exception as exc:
            self._status(f"Playback unavailable: {exc}", error=True)
        self.refresh_playback()

    @work(group="refresh", exit_on_error=False)
    async def refresh_playback(self) -> None:
        # A slow IPC timeout must not create another blocked thread each second.
        if self._refreshing:
            return
        self._refreshing = True
        try:
            snapshot, queue = await asyncio.to_thread(self._read_playback)
            self._snapshot = snapshot
            self._show_playback(snapshot)
            tracks = list(queue)
            if tracks != self.queued:
                table = self.query_one("#queue", DataTable)
                self.queued = tracks
                table.clear()
                for index, track in enumerate(tracks):
                    table.add_row(
                        Text(_plain(f"{index + 1}. {track.title} · {', '.join(track.artists)}")),
                        key=str(index),
                    )
                self._selected_queue = min(self._selected_queue, max(0, len(tracks) - 1))
                if tracks:
                    table.move_cursor(row=self._selected_queue)
            self.query_one("#queue-empty").display = not bool(tracks)
            self.query_one("#queue-pane").border_title = f"MY QUEUE · {len(tracks)}"
        except Exception as exc:
            self._status(f"Playback unavailable: {exc}", error=True)
        finally:
            self._refreshing = False

    def _read_playback(self) -> tuple[dict[str, Any], list[Track]]:
        return self.service.now(), self.service.queue()

    def _show_playback(self, snapshot: dict[str, Any]) -> None:
        track = snapshot.get("track")
        if track:
            artists = ", ".join(track.get("artists") or ())
            state = "Ⅱ" if snapshot.get("paused") else "▶"
            radio = " · RADIO" if snapshot.get("radio") else ""
            self.query_one("#now", Static).update(
                _plain(f"{state} {track.get('title', 'Unknown track')} · {artists}{radio}")
            )
        else:
            self.query_one("#now", Static).update("Nothing playing · search to begin")
        position = float(snapshot.get("position") or 0)
        duration = snapshot.get("duration")
        duration = float(duration) if duration else None
        if duration and (not math.isfinite(duration) or duration <= 0):
            duration = None
        progress = max(0.0, min(100.0, position / duration * 100)) if duration else 0
        self.query_one("#progress", ProgressBar).update(total=100, progress=progress)
        self.query_one("#time", Static).update(f"{_clock(position)} / {_clock(duration)}")
        self.query_one("#pause", Button).label = "▶ Resume" if snapshot.get("paused") else "Ⅱ Pause"
        error = snapshot.get("error")
        if error and error != self._last_player_error:
            self._status(str(error), error=True)
        self._last_player_error = error

    def action_help(self) -> None:
        self.push_screen(
            DetailScreen(
                "MYNE CLI · YOUR MUSIC, YOUR WAY",
                "My Music. My Queue. My Experience.\n\n"
                "DISCOVER\n"
                "Ctrl+F       Focus the search box\n"
                "Enter        Search; play a selected result or queue track\n"
                "↑ / ↓        Select tracks\n"
                "Tab          Move between panes and controls\n"
                "p            Play the selected track\n"
                "a            Add the selected track to your queue\n\n"
                "PLAYBACK\n"
                "Space        Pause / resume\n"
                "n            Next track\n"
                "l            Lyrics for the current track\n"
                "s            Listening statistics\n\n"
                "SEARCH COMMANDS\n"
                "/mix focus       Build a mood mix\n"
                "/mix-ai focus    Use the configured AI mix plugin\n"
                "/radio Numb      Start an endless radio queue\n\n"
                "Shortcuts for letters apply outside the search box.\n"
                "Ctrl+Q exits the interface; playback continues in the daemon.\n"
                "Use myne stop to stop playback.",
            )
        )

    def action_lyrics(self) -> None:
        self.show_detail("Lyrics", self.service.lyrics)

    def action_stats(self) -> None:
        self.show_detail("Listening statistics", self.service.stats)

    @work(exclusive=True, group="detail", exit_on_error=False)
    async def show_detail(self, title: str, function: Callable[[], Any]) -> None:
        self._status(f"Loading {title.lower()}…")
        try:
            data = await asyncio.to_thread(function)
            if isinstance(data, dict):
                body = _stats_text(data)
            else:
                body = str(data) if data else "Nothing here yet. Play a track to get started."
            # Lyrics retain newlines, but never interpret provider-supplied markup.
            body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", body)
            self.push_screen(DetailScreen(title, body))
            self._status("Press Esc to return to your music.")
        except Exception as exc:
            self._status(f"Could not load {title.lower()}: {exc}", error=True)


def launch_ui(app: Application) -> None:
    """Launch the full-screen terminal interface."""
    MyneApp(app).run()
