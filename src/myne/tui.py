"""A keyboard-first Textual interface around the application service.

Provider, database and playback work stays off the event loop. Closing this
interface deliberately leaves the playback daemon and persistent queue running.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, ProgressBar, Static

from myne.models import Track
from myne.motion import AudioMotion, reduced_motion_requested

if TYPE_CHECKING:
    from myne.lyrics import LyricsDocument
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


def _level(value: object) -> float:
    """Keep malformed or non-finite audio telemetry out of the renderer."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 0.0


def _meter(rms: float, peak: float, width: int) -> Text:
    """Draw real RMS with a peak marker on a useful -60 to 0 dB scale."""
    # The daemon already maps -60..0 dBFS to 0..1; never logarithmize it twice.
    amount = _level(rms) * width
    filled = int(amount)
    fraction = min(7, int((amount - filled) * 8))
    peak_cell = min(width - 1, round(_level(peak) * (width - 1)))
    result = Text()
    for index in range(width):
        character = (
            "│"
            if peak > 0 and index == peak_cell
            else "█"
            if index < filled
            else "▏▎▍▌▋▊▉"[fraction - 1]
            if index == filled and fraction
            else "░"
        )
        color = "#ffa990" if index > width * 0.9 else "#62d6bf"
        result.append(character, style=color if character != "░" else "#29404e")
    return result


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
    #lyrics-pane { width: 2fr; min-width: 29; margin-left: 1; }
    #lyrics-scroll { height: 1fr; padding: 0 1; }
    #lyrics-body { height: auto; min-height: 1; }
    #lyrics-source { height: 1; color: #94a9b5; padding: 0 1; text-overflow: ellipsis; }
    #library.compact-lyrics #results-pane, #library.compact-lyrics #queue-pane { display: none; }
    #library.compact-lyrics #lyrics-pane { width: 1fr; margin-left: 0; }
    DataTable { height: 1fr; background: #0b1219; }
    DataTable > .datatable--header { background: #172630; color: #94a9b5; }
    DataTable:focus > .datatable--cursor { background: #62d6bf; color: #0b1219; }
    DataTable > .datatable--cursor { background: #29404e; }
    .empty { height: 1; color: #94a9b5; padding-left: 1; }
    #player { height: 11; margin: 0 1; border: round #29404e; padding: 0 1; }
    #player.no-visualizer { height: 8; }
    #sound-controls { height: 1; }
    #sound-controls Button { min-width: 3; width: 3; height: 1; min-height: 1; border: none; padding: 0; }
    #sound-controls #mute { width: 8; margin: 0 1; }
    #volume-label { width: 15; height: 1; text-align: center; }
    #audio-output { width: 1fr; height: 1; color: #94a9b5; text-overflow: ellipsis; }
    #audio-output.silent { color: #ffa990; }
    #audio-meter { height: 3; color: #94a9b5; }
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
        Binding("a", "enqueue", "Add", show=False),
        Binding("p", "play", "Play", show=False),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "next", "Next", show=False),
        Binding("[", "volume_down", "Volume −", show=False),
        Binding("]", "volume_up", "Volume +", show=False),
        Binding("m", "mute", "Mute", show=False),
        Binding("l", "lyrics", "Lyrics"),
        Binding("v", "visualizer", "Visualizer"),
        Binding("s", "stats", "Stats", show=False),
        Binding("?", "help", "Help"),
        Binding("ctrl+q", "quit", "Exit", priority=True),
    ]

    def __init__(self, application: Application, *, reduced_motion: bool | None = None) -> None:
        super().__init__()
        self._reduced_motion = reduced_motion_requested() if reduced_motion is None else reduced_motion
        if self._reduced_motion:
            self.animation_level = "none"
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
        self._snapshot_at = time.monotonic()
        self._visualizer_enabled = True
        self._visualizer_connected = False
        self._levels_refreshing = False
        self._visualizer_configuring = False
        self._levels: dict[str, Any] = {}
        self._audio_motion = AudioMotion(reduced_motion=self._reduced_motion)
        self._audio_rendered: Text | str | None = None
        self._time_rendered = ""
        self._audio_history: deque[float] = deque(maxlen=100)
        self._lyrics_visible = False
        self._lyrics_user_toggled = False
        self._lyrics_key: tuple[str, str] | None = None
        self._lyrics_document: LyricsDocument | None = None
        self._lyrics_pending: Track | None = None
        self._lyrics_fetching = False
        self._lyrics_error: str | None = None
        self._lyrics_loading = False
        self._lyric_index = -2
        self._last_lyric_position: float | None = None

    @property
    def _main_screen(self) -> Screen[Any]:
        return self.screen_stack[0]

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
            with Vertical(id="lyrics-pane", classes="pane"):
                with VerticalScroll(id="lyrics-scroll", can_focus=True):
                    yield Static("Play a track to load lyrics.", id="lyrics-body", markup=False)
                yield Static("l · Show / hide lyrics", id="lyrics-source", markup=False)
        with Vertical(id="player"):
            yield Static("Nothing playing · search to begin", id="now", markup=False)
            with Horizontal(id="progress-row"):
                yield ProgressBar(total=100, show_eta=False, show_percentage=False, id="progress")
                yield Static("0:00 / —:——", id="time", markup=False)
            yield Static("AUDIO LEVELS · waiting for playback…", id="audio-meter", markup=False)
            with Horizontal(id="sound-controls"):
                yield Button("−", id="volume-down", tooltip="Lower volume · [")
                yield Static("Volume: —", id="volume-label", markup=False)
                yield Button("+", id="volume-up", tooltip="Raise volume · ]")
                yield Button("Mute", id="mute", tooltip="Mute / unmute · m")
                yield Static("Output: —", id="audio-output", markup=False)
            with Horizontal(id="transport"):
                yield Button("▶ Play", id="play")
                yield Button("+ Queue", id="add")
                yield Button("Ⅱ Pause", id="pause")
                yield Button("Next ›", id="next")
        yield Footer()

    def on_mount(self) -> None:
        self._main_screen.query_one("#results-pane").border_title = "DISCOVER"
        self._main_screen.query_one("#queue-pane").border_title = "MY QUEUE · 0"
        self._main_screen.query_one("#player").border_title = "NOW PLAYING"
        self._main_screen.query_one("#lyrics-pane").border_title = "LYRICS · l"
        self._main_screen.query_one("#results", DataTable).add_columns("Title", "Artist", "Time")
        self._main_screen.query_one("#queue", DataTable).add_column("Up next")
        self._main_screen.query_one("#search", Input).focus()
        self.refresh_playback()
        self.set_interval(1.0, self.refresh_playback)
        self.set_interval(0.12, self.sample_audio)
        self.set_interval(0.12 if self._reduced_motion else 1 / 30, self.tick_playback)
        self._layout_lyrics()
        self.configure_visualizer()

    def on_resize(self, event: Resize) -> None:
        if self.screen_stack and self._main_screen.query("#lyrics-pane"):
            self._layout_lyrics()

    async def on_unmount(self) -> None:
        if self._visualizer_connected:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.service.visualizer, False), timeout=1.0)
            except Exception:
                pass

    def _layout_lyrics(self) -> None:
        wide = self.size.width >= 110
        if not self._lyrics_user_toggled:
            self._lyrics_visible = wide
        pane = self._main_screen.query_one("#lyrics-pane")
        was_visible = pane.display
        pane.display = self._lyrics_visible
        if self._lyrics_visible and not was_visible and not self._reduced_motion:
            pane.styles.opacity = 0.0
            pane.styles.animate("opacity", 1.0, duration=0.14, easing="out_cubic", level="basic")
        self._main_screen.query_one("#library").set_class(self._lyrics_visible and not wide, "compact-lyrics")
        if self._lyrics_visible:
            self.call_after_refresh(self._render_lyrics, force=True)

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self._main_screen.query_one("#status", Static)
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
        table = self._main_screen.query_one("#results", DataTable)
        table.clear()
        for index, track in enumerate(self.results):
            table.add_row(
                Text(_plain(track.title)),
                Text(_plain(", ".join(track.artists) or "Unknown artist")),
                Text(_clock(track.duration)),
                key=str(index),
            )
        # Border titles use Rich markup; untrusted moods are kept out of them.
        self._main_screen.query_one("#results-pane").border_title = (
            "SMART MIX" if heading.startswith("MIX") else "DISCOVER"
        )
        self._main_screen.query_one("#results-empty").display = not bool(self.results)
        self._main_screen.query_one("#results-empty", Static).update("No tracks found. Try another search.")
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
                return [self.queued[self._selected_queue]]
            return []
        self._selection_source = "results"
        if 0 <= self._selected_result < len(self.results):
            return [self.results[self._selected_result]]
        return []

    def action_search_focus(self) -> None:
        self._main_screen.query_one("#search", Input).focus()

    def action_play(self) -> None:
        tracks = self._selected()
        if tracks:
            if self._selection_source == "queue":
                self.mutate("Playing", self.service.play_queued, self._selected_queue, tracks[0])
            else:
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
        action = "resume" if self._snapshot.get("paused") or not self._snapshot.get("track") else "pause"
        self.mutate("Resumed" if action == "resume" else "Paused", self.service.control, action)

    def action_next(self) -> None:
        self.mutate("Skipped to next track", self.service.control, "next")

    def action_volume_down(self) -> None:
        self.mutate("Volume lowered", lambda: self.service.control("volume", delta=-5))

    def action_volume_up(self) -> None:
        self.mutate("Volume raised", lambda: self.service.control("volume", delta=5))

    def action_mute(self) -> None:
        self.mutate("Sound updated", self.service.control, "mute")

    @on(Button.Pressed)
    def press_transport(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "play": self.action_play,
            "add": self.action_enqueue,
            "pause": self.action_toggle_pause,
            "next": self.action_next,
            "volume-down": self.action_volume_down,
            "volume-up": self.action_volume_up,
            "mute": self.action_mute,
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
                if isinstance(result, dict) and "track" in result:
                    self._show_playback(result)
                    if message == "Playing" and not result.get("playing") and not result.get("paused"):
                        message = "Loading audio…"
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
                table = self._main_screen.query_one("#queue", DataTable)
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
            self._main_screen.query_one("#queue-empty").display = not bool(tracks)
            self._main_screen.query_one("#queue-pane").border_title = f"MY QUEUE · {len(tracks)}"
        except Exception as exc:
            self._status(f"Playback unavailable: {exc}", error=True)
        finally:
            self._refreshing = False

    def _read_playback(self) -> tuple[dict[str, Any], list[Track]]:
        return self.service.now(), self.service.queue()

    def _show_playback(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._snapshot_at = time.monotonic()
        track = snapshot.get("track")
        if track:
            artists = ", ".join(track.get("artists") or ())
            state = "Ⅱ" if snapshot.get("paused") else "▶" if snapshot.get("playing") else "Loading ·"
            radio = " · RADIO" if snapshot.get("radio") else ""
            self._main_screen.query_one("#now", Static).update(
                _plain(f"{state} {track.get('title', 'Unknown track')} · {artists}{radio}")
            )
        else:
            self._main_screen.query_one("#now", Static).update("Nothing playing · search to begin")
        volume = snapshot.get("volume", 70)
        muted = snapshot.get("muted", False)
        self._main_screen.query_one("#volume-label", Static).update(
            f"Muted · {volume:g}%" if muted else f"Volume: {volume:g}%"
        )
        self._main_screen.query_one("#mute", Button).label = "Unmute" if muted else "Mute"
        raw_output = snapshot.get("audio_output")
        output = raw_output if isinstance(raw_output, str) else ""
        outputs = {"pulse": "PulseAudio", "pipewire": "PipeWire", "coreaudio": "CoreAudio", "alsa": "ALSA"}
        output_label = "silent (null)" if output == "null" else outputs.get(output, output or "—")
        output_widget = self._main_screen.query_one("#audio-output", Static)
        output_widget.update(_plain(f"Output: {output_label}"))
        output_widget.set_class(output == "null" or muted or volume == 0, "silent")
        self._sync_lyrics_track(track)
        self.tick_playback()
        self._main_screen.query_one("#pause", Button).label = (
            "▶ Start" if not track else "▶ Resume" if snapshot.get("paused") else "Ⅱ Pause"
        )
        error = snapshot.get("error")
        if error and error != self._last_player_error:
            self._status(str(error), error=True)
        self._last_player_error = error

    def _playback_position(self) -> float:
        position = float(self._snapshot.get("position") or 0)
        if not math.isfinite(position):
            return 0.0
        if (
            self._snapshot.get("track")
            and not self._snapshot.get("paused")
            and self._snapshot.get("playing", True)
        ):
            # Never extrapolate past a lost daemon connection indefinitely.
            position += min(1.5, max(0.0, time.monotonic() - self._snapshot_at))
        return max(0.0, position)

    def tick_playback(self) -> None:
        """Keep the cursor smooth; the next mpv sample corrects seeks and stalls."""
        if not self._main_screen.query("#time"):
            return
        snapshot = self._snapshot
        position = self._playback_position()
        duration = snapshot.get("duration")
        duration = float(duration) if duration else None
        if duration and (not math.isfinite(duration) or duration <= 0):
            duration = None
        progress = max(0.0, min(100.0, position / duration * 100)) if duration else 0
        self._main_screen.query_one("#progress", ProgressBar).update(total=100, progress=progress)
        label = f"{_clock(position)} / {_clock(duration)}"
        if label != self._time_rendered:
            self._main_screen.query_one("#time", Static).update(label)
            self._time_rendered = label
        self._render_lyrics(position)
        self._render_audio()

    @work(group="visualizer-config", exit_on_error=False)
    async def configure_visualizer(self) -> None:
        if self._visualizer_configuring:
            return
        self._visualizer_configuring = True
        try:
            while True:
                requested = self._visualizer_enabled
                result = await asyncio.to_thread(self.service.visualizer, requested)
                self._visualizer_connected = bool(requested and not result.get("error"))
                if requested == self._visualizer_enabled:
                    if result.get("error"):
                        self._levels = {"available": False, "error": str(result["error"])}
                        self._audio_motion.reset()
                    break
        except Exception as exc:
            self._visualizer_connected = False
            self._levels = {"available": False, "error": str(exc)}
            self._audio_motion.reset()
        finally:
            self._visualizer_configuring = False
            self._render_audio()

    def action_visualizer(self) -> None:
        self._visualizer_enabled = not self._visualizer_enabled
        self._audio_history.clear()
        self._audio_motion.reset()
        self._main_screen.query_one("#audio-meter").display = self._visualizer_enabled
        self._main_screen.query_one("#player").set_class(not self._visualizer_enabled, "no-visualizer")
        self.configure_visualizer()
        self._status(
            "Audio visualizer on · actual mpv RMS / peak"
            if self._visualizer_enabled
            else "Audio visualizer off · press v to show"
        )

    @work(group="audio-levels", exit_on_error=False)
    async def sample_audio(self) -> None:
        """A single cheap daemon request; slow IPC never accumulates threads."""
        if not self._visualizer_enabled or self._levels_refreshing or not self._snapshot.get("track"):
            return
        self._levels_refreshing = True
        requested_track = self._lyrics_key
        try:
            levels = await asyncio.to_thread(self.service.audio_levels)
            if requested_track != self._lyrics_key:
                return
            if (
                levels.get("track_id") is not None
                and (levels.get("provider"), levels.get("track_id")) != requested_track
            ):
                self._levels = {}
                self._audio_motion.reset()
                return
            self._levels = levels
            track = self._snapshot.get("track") or {}
            if levels.get("track_id") == track.get("id") and levels.get("provider") == track.get("provider"):
                if levels.get("position") is not None:
                    self._snapshot["position"] = levels["position"]
                    self._snapshot_at = time.monotonic()
                self._snapshot["paused"] = levels.get("paused", self._snapshot.get("paused"))
                self._snapshot["playing"] = levels.get("playing", self._snapshot.get("playing", True))
            self._audio_motion.update(
                {
                    **levels,
                    "paused": self._snapshot.get("paused", False),
                    "playing": self._snapshot.get("playing", True),
                }
            )
            if (
                levels.get("available")
                and not self._snapshot.get("paused")
                and self._snapshot.get("playing", True)
            ):
                rms = levels.get("rms") or []
                self._audio_history.append(max((_level(value) for value in rms), default=0.0))
        except Exception as exc:
            self._levels = {"available": False, "error": str(exc)}
            self._audio_motion.reset()
        finally:
            self._levels_refreshing = False
            self._render_audio()

    def _render_audio(self) -> None:
        if not self._main_screen.query("#audio-meter"):
            return
        meter = self._main_screen.query_one("#audio-meter", Static)

        def show(value: str | Text) -> None:
            if value != self._audio_rendered:
                meter.update(value)
                self._audio_rendered = value

        if not self._visualizer_enabled:
            return
        if not self._snapshot.get("track"):
            self._audio_motion.reset()
            show("AUDIO LEVELS · waiting for playback…\nStereo RMS / peak from mpv · v toggles visualizer")
            return
        if self._snapshot.get("paused"):
            self._audio_motion.reset()
            show("AUDIO LEVELS · paused\nResume playback to continue the live meter.")
            return
        if not self._snapshot.get("playing", True):
            self._audio_motion.reset()
            show("AUDIO LEVELS · buffering\nWaiting for the audio stream to continue.")
            return
        levels = self._audio_motion.frame()
        if not levels.get("available"):
            message = self._levels.get("error") or levels.get("error")
            show(
                _plain(f"AUDIO LEVELS · {'unavailable' if message else 'connecting…'}")
                + (f"\n{_plain(message)}" if message else "\nWaiting for audio samples from mpv.")
            )
            return
        rms = list(levels.get("rms") or [0.0])
        peaks = list(levels.get("peak") or rms)
        width = max(8, min(48, meter.size.width - 17))
        rendered = Text()
        for channel in range(min(2, len(rms))):
            value = _level(rms[channel])
            peak = _level(peaks[min(channel, len(peaks) - 1)])
            rendered.append("L " if channel == 0 else "R ", style="#94a9b5")
            rendered.append_text(_meter(value, peak, width))
            rendered.append(f" {value * 60 - 60:5.1f} dBFS" if value > 0 else " ≤−60 dBFS")
            rendered.append("\n")
        if len(rms) == 1:
            rendered.append("MONO · actual mpv RMS / peak\n", style="#94a9b5")
        symbols = "▁▂▃▄▅▆▇█"
        history = list(self._audio_history)[-max(1, meter.size.width - 15) :]
        rendered.append("RMS HISTORY  ", style="#94a9b5")
        rendered.append(
            "".join(symbols[min(7, round(_level(value) * 7))] for value in history), style="#62d6bf"
        )
        show(rendered)

    def _sync_lyrics_track(self, track_data: dict[str, Any] | None) -> None:
        track = Track.from_dict(track_data) if track_data else None
        key = (track.provider, track.id) if track else None
        if key == self._lyrics_key:
            return
        self._lyrics_key = key
        self._lyrics_document = None
        self._lyrics_error = None
        self._lyrics_loading = track is not None
        self._lyric_index = -2
        self._lyrics_pending = track
        self._audio_history.clear()
        self._levels = {}
        self._audio_motion.reset()
        self._last_lyric_position = None
        self._main_screen.query_one("#lyrics-scroll", VerticalScroll).scroll_to(y=0, animate=False)
        self._render_lyrics(force=True)
        if track:
            self.fetch_lyrics()
            if self._visualizer_enabled and not self._visualizer_connected:
                self.configure_visualizer()

    @work(group="lyrics-fetch", exit_on_error=False)
    async def fetch_lyrics(self) -> None:
        """One provider request at a time; only the newest track can publish."""
        if self._lyrics_fetching:
            return
        self._lyrics_fetching = True
        try:
            while self._lyrics_pending is not None:
                track = self._lyrics_pending
                self._lyrics_pending = None
                key = (track.provider, track.id)
                document = None
                error = None
                try:
                    document = await asyncio.to_thread(self.service.lyrics_document, track)
                except Exception as exc:
                    error = str(exc)
                if self._lyrics_key == key:
                    self._lyrics_document = document
                    self._lyrics_loading = False
                    self._lyrics_error = error
                    self._render_lyrics(force=True)
        finally:
            self._lyrics_fetching = False

    def _render_lyrics(self, position: float | None = None, *, force: bool = False) -> None:
        if not self._main_screen.query("#lyrics-body"):
            return
        document = self._lyrics_document
        position = self._playback_position() if position is None else position
        previous_position = self._last_lyric_position
        self._last_lyric_position = position
        current = document.current_index(position) if document and document.synced else None
        index = current if current is not None else -1
        previous_index = self._lyric_index
        if not force and index == self._lyric_index:
            return
        self._lyric_index = index
        body = self._main_screen.query_one("#lyrics-body", Static)
        source = self._main_screen.query_one("#lyrics-source", Static)
        if self._lyrics_loading:
            body.update("Finding lyrics for this track…")
            source.update("Lyrics · loading")
        elif self._lyrics_error:
            body.update(f"Lyrics unavailable right now.\n\n{_plain(self._lyrics_error)}")
            source.update("l · Hide lyrics · retry on next track")
        elif document is None:
            body.update(
                "No lyrics found for this track." if self._lyrics_key else "Play a track to load lyrics."
            )
            source.update("l · Show / hide lyrics")
        elif document.instrumental:
            body.update("♫  Instrumental\n\nThis track has no sung lyrics.")
            source.update(_plain(document.source))
        elif document.synced:
            text = Text()
            for line_index, line in enumerate(document.synced):
                text.append("› " if line_index == index else "  ", style="#62d6bf")
                text.append(
                    _plain(line.text) or "♪",
                    style=(
                        "bold #62d6bf"
                        if line_index == index
                        else "#e7eef3"
                        if line_index > index
                        else "#94a9b5"
                    ),
                )
                text.append("\n")
            body.update(text)
            source.update(_plain(f"{document.source} · synced · l hides"))
            if not self._lyrics_visible:
                return
            scroll = self._main_screen.query_one("#lyrics-scroll", VerticalScroll)
            width = max(1, body.size.width or scroll.size.width - 2)
            row_offset = sum(
                len(Text(f"  {_plain(line.text) or '♪'}").wrap(self.console, width))
                for line in document.synced[: max(0, index)]
            )
            # Layout must measure the new wrapped content before clamping scroll.
            animate = bool(
                not self._reduced_motion
                and not force
                and previous_index >= 0
                and index == previous_index + 1
                and not self._snapshot.get("paused")
                and previous_position is not None
                and 0 <= position - previous_position < 0.8
            )
            self.call_after_refresh(
                self._scroll_lyrics_to,
                max(0, row_offset - scroll.size.height // 2),
                self._lyrics_key,
                index,
                animate,
            )
        else:
            plain = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", document.plain)
            body.update(plain or "No lyrics found for this track.")
            source.update(_plain(f"{document.source} · plain lyrics · scroll ↑ ↓"))

    def _scroll_lyrics_to(self, target: int, key: tuple[str, str] | None, index: int, animate: bool) -> None:
        if self._lyrics_key != key or self._lyric_index != index or not self._lyrics_visible:
            return
        self._main_screen.query_one("#lyrics-scroll", VerticalScroll).scroll_to(
            y=target,
            animate=animate,
            duration=0.22,
            easing="out_cubic",
            force=True,
            immediate=True,
        )

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
                "Space        Start queue / pause / resume\n"
                "n            Next track\n"
                "[ / ]        Lower / raise volume (raising also unmutes)\n"
                "m            Mute / unmute\n"
                "l            Show / hide live lyrics; timed lines follow playback\n"
                "v            Show / hide the real stereo RMS / peak visualizer\n"
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
        self._lyrics_user_toggled = True
        self._lyrics_visible = not self._lyrics_visible
        self._layout_lyrics()
        if self._lyrics_visible:
            self._main_screen.query_one("#lyrics-scroll", VerticalScroll).focus()
            self._render_lyrics(force=True)
            self._status("Live lyrics · l returns to tracks · Tab moves focus · ↑ ↓ scroll plain lyrics")
        else:
            self._main_screen.query_one("#results", DataTable).focus()
            self._status("Lyrics hidden · press l to show")

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
