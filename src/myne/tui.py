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
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import DescendantFocus, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from myne.models import Playlist, Track
from myne.motion import AudioMotion, reduced_motion_requested
from myne.playlists import ImportResult, playlist_link
from myne.preferences import Preferences
from myne.ui.record import RecordArt
from myne.ui.screens import ImportPlaylistScreen, SaveQueueScreen
from myne.ui.settings import SettingsScreen
from myne.ui.themes import THEME_NAMES, THEMES

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


def _meter(rms: float, peak: float, width: int, accent: str = "#b7ed7e") -> Text:
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
        color = "#edc17e" if index > width * 0.9 else accent
        result.append(character, style=color if character != "░" else "dim")
    return result


class DetailScreen(ModalScreen[None]):
    """Scrollable help, lyrics or listening statistics."""

    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; background: $background 85%; }
    DetailScreen > Vertical {
        width: 78; max-width: 94%; height: 85%;
        border: solid $primary; padding: 1 2; background: $surface;
    }
    DetailScreen #detail-heading { height: 2; color: $primary; text-style: bold; }
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
    ENABLE_COMMAND_PALETTE = True
    CSS_PATH = "ui/myne.tcss"
    BINDINGS = [
        Binding("ctrl+f", "search_focus", "Search", priority=True),
        Binding("ctrl+k", "command_palette", "Actions", priority=True),
        Binding("ctrl+b", "sidebar", "Sidebar", priority=True, show=False),
        Binding("ctrl+o", "import_playlist", "Import", priority=True, show=False),
        Binding("f2", "settings", "Settings", priority=True, show=False),
        Binding("f3", "listening", "Listen", priority=True, show=False),
        Binding("1", "discover", "Discover", show=False),
        Binding("2", "favorites", "Liked", show=False),
        Binding("3", "recent", "Recent", show=False),
        Binding("4", "playlists", "Playlists", show=False),
        Binding("f", "favorite", "Like", show=False),
        Binding("r", "repeat", "Repeat", show=False),
        Binding("z", "shuffle", "Shuffle", show=False),
        Binding("t", "theme", "Theme", show=False),
        Binding("comma", "seek_back", "−10s", show=False),
        Binding("full_stop", "seek_forward", "+10s", show=False),
        Binding("delete", "queue_remove", "Remove", show=False),
        Binding("alt+up", "queue_up", "Move up", show=False),
        Binding("alt+down", "queue_down", "Move down", show=False),
        Binding("a", "enqueue", "Add", show=False),
        Binding("p", "play", "Play", show=False),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "next", "Next", show=False),
        Binding("[", "volume_down", "Volume −", show=False),
        Binding("]", "volume_up", "Volume +", show=False),
        Binding("m", "mute", "Mute", show=False),
        Binding("l", "lyrics", "Lyrics"),
        Binding("v", "visualizer", "Visualizer", show=False),
        Binding("s", "stats", "Stats", show=False),
        Binding("?", "help", "Help"),
        Binding("ctrl+q", "quit", "Exit", priority=True),
    ]

    def __init__(self, application: Application, *, reduced_motion: bool | None = None) -> None:
        super().__init__()
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = THEME_NAMES[0]
        self._theme_user_changed = False
        self._preferences = Preferences()
        self._preferences_user_changed = False
        self._preference_overrides: set[str] = set()
        self._explicit_reduced_motion = reduced_motion
        self._library_loading = False
        self._view = "discover"
        self._listening = False
        self._listening_lyrics: bool | None = None
        self._search_heading = "Find your next favorite."
        self._discover_results: list[Track] = []
        self._favorites: set[tuple[str, str]] = set()
        self._playlist_names: list[str] = []
        self._sidebar_playlist_names: list[str] = []
        self._open_playlist: Playlist | None = None
        self._sidebar_override: bool | None = None
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
        with Horizontal(id="masthead"):
            yield Static("M Y N E\nYOUR MUSIC. YOUR SPACE.", id="brand", markup=False)
            with Vertical(id="header-right"):
                yield Static("READY TO LISTEN", id="session-state", markup=False)
                with Horizontal(id="header-buttons"):
                    yield Button("Library", id="sidebar-toggle", tooltip="Toggle the library sidebar · Ctrl+B")
                    yield Button("Listen", id="listening-toggle", tooltip="Focused listening view · F3")
                    yield Button("Commands", id="commands", tooltip="Find any action · Ctrl+K")
                    yield Button("Theme", id="theme", tooltip="Switch the color palette · t")
                    yield Button("Settings", id="settings", tooltip="Preferences and updates · F2")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield Static("YOUR LIBRARY", classes="sidebar-label")
                for view, label in (("discover", "Discover"), ("favorites", "Liked tracks"),
                                    ("recent", "Recent plays"), ("playlists", "All playlists")):
                    yield Button(label, id=f"rail-{view}", classes="view-button" + (" selected" if view == "discover" else ""))
                yield Static("YOUR PLAYLISTS", classes="sidebar-label")
                yield Button("+ Import link", id="import-playlist", classes="mood-button")
                yield OptionList(id="sidebar-playlists", compact=True)
                yield Static("IN THE MOOD", id="mood-label", classes="sidebar-label")
                yield Button("Focus session", id="mood-focus", classes="mood-button")
                yield Button("Night drive", id="mood-night", classes="mood-button")
                yield Static("0 liked tracks\n0 in your queue", id="library-count", markup=False)
                yield Static("Ctrl+B sidebar\nCtrl+O import link\n1–4 switch view", id="rail-tip", markup=False)
            with Vertical(id="main"):
                yield Input(placeholder="Find your next favorite · songs, artists, /mix focus", id="search")
                with Horizontal(id="view-tabs"):
                    for view, label in (("discover", "1 Discover"), ("favorites", "2 Liked"),
                                        ("recent", "3 Recent"), ("playlists", "4 Playlists")):
                        yield Button(label, id=f"tab-{view}", classes="view-button" + (" selected" if view == "discover" else ""))
                with Horizontal(id="collection-header"):
                    yield Static("◈", id="collection-mark", markup=False)
                    with Vertical(id="collection-copy"):
                        yield Static("Find your next favorite.", id="collection-title", markup=False)
                        yield Static("Search a song, artist, or mood to begin.", id="collection-meta", markup=False)
                yield Static("Search for music or open your library. Ctrl+K finds every action.", id="status", markup=False)
                with Horizontal(id="library"):
                    with Vertical(id="listening-stage", classes="pane"):
                        yield Static("THE LISTENING ROOM", id="listening-label", markup=False)
                        yield RecordArt(id="record-art")
                        yield Static("Make room for your next favorite.", id="listening-title", markup=False)
                        yield Static("Search for a song or start your queue.", id="listening-artist", markup=False)
                        yield Static("", id="listening-album", markup=False)
                        yield Static("Space pause / resume · n next · F3 back to library", id="listening-hint", markup=False)
                    with Vertical(id="results-pane", classes="pane"):
                        with Horizontal(id="playlist-tools"):
                            yield Button("‹ All", id="playlist-back", tooltip="Browse all playlists")
                            yield Button("▶ All", id="playlist-play", tooltip="Play every song in this playlist")
                            yield Button("+ All", id="playlist-queue", tooltip="Add every song to the queue")
                            yield Button("Info", id="playlist-info", tooltip="Source link and import warnings")
                            yield Button("Import", id="playlist-import", tooltip="Import a playlist link · Ctrl+O")
                        yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
                        yield Static("A song, an artist, a mood. Start with a search.", id="results-empty", classes="empty", markup=False)
                    with Vertical(id="queue-pane", classes="pane"):
                        yield DataTable(id="queue", cursor_type="row", show_header=False)
                        yield Static("Add tracks with a or + Queue.", id="queue-empty", classes="empty", markup=False)
                        with Horizontal(id="queue-tools"):
                            yield Button("↑", id="queue-up", tooltip="Move selected queued track up · Alt+Up")
                            yield Button("↓", id="queue-down", tooltip="Move selected queued track down · Alt+Down")
                            yield Button("Del", id="queue-remove", tooltip="Remove selected queued track · Delete")
                            yield Button("Shuffle", id="queue-shuffle", tooltip="Shuffle upcoming tracks · z")
                            yield Button("Save", id="queue-save", tooltip="Save this queue with a name")
                    with Vertical(id="lyrics-pane", classes="pane"):
                        with VerticalScroll(id="lyrics-scroll", can_focus=True):
                            yield Static("Play a track to load lyrics.", id="lyrics-body", markup=False)
                        yield Static("l · Show / hide lyrics", id="lyrics-source", markup=False)
        with Vertical(id="player"):
            yield Static("Your next listening session starts here.", id="now", markup=False)
            with Horizontal(id="progress-row"):
                yield ProgressBar(total=100, show_eta=False, show_percentage=False, id="progress")
                yield Static("0:00 / —:——", id="time", markup=False)
            yield Static("AUDIO LEVELS · waiting for playback…", id="audio-meter", markup=False)
            with Horizontal(id="sound-controls"):
                yield Button("−10s", id="seek-back", tooltip="Seek back 10 seconds · ,", disabled=True)
                yield Button("+10s", id="seek-forward", tooltip="Seek forward 10 seconds · .", disabled=True)
                yield Button("−", id="volume-down", tooltip="Lower volume · [")
                yield Static("Volume: —", id="volume-label", markup=False)
                yield Button("+", id="volume-up", tooltip="Raise volume · ]")
                yield Button("Mute", id="mute", tooltip="Mute / unmute · m")
                yield Button("Loop: off", id="repeat", tooltip="Repeat off / all / one · r")
                yield Static("Output: —", id="audio-output", markup=False)
            with Horizontal(id="transport"):
                yield Button("▶ Play", id="play")
                yield Button("+ Queue", id="add")
                yield Button("Ⅱ Pause", id="pause")
                yield Button("Next ›", id="next")
                yield Button("♡ Like", id="favorite", tooltip="Like the selected track, or the current song · f")
                yield Button("Stop", id="stop", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        # Textual 6's CSS parser rejects zero line-pad; the style API accepts it.
        for button in self._main_screen.query(Button):
            button.styles.line_pad = 0
            button.styles.border = "none"
        self._main_screen.query_one("#results-pane").border_title = "DISCOVER"
        self._main_screen.query_one("#queue-pane").border_title = "MY QUEUE · 0"
        self._main_screen.query_one("#player").border_title = "NOW PLAYING"
        self._main_screen.query_one("#lyrics-pane").border_title = "LYRICS · l"
        self._main_screen.query_one("#results", DataTable).add_columns("Title", "Artist", "Time")
        self._main_screen.query_one("#queue", DataTable).add_column("Up next")
        self._main_screen.query_one("#search", Input).focus()
        self.refresh_playback()
        self.set_interval(1.0, self.refresh_playback)
        self._audio_poll_timer = self.set_interval(0.12, self.sample_audio)
        self._render_timer = self.set_interval(0.12 if self._reduced_motion else 1 / 30, self.tick_playback)
        self._layout_lyrics()
        self.configure_visualizer()
        self.bootstrap_library()

    def on_resize(self, event: Resize) -> None:
        if self.screen_stack and self._main_screen.query("#lyrics-pane"):
            # App.size still describes the previous screen during this event.
            self.call_after_refresh(self._layout_lyrics)

    async def on_unmount(self) -> None:
        if self._visualizer_connected:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.service.visualizer, False), timeout=1.0)
            except Exception:
                pass

    def _layout_lyrics(self) -> None:
        sidebar = self._sidebar_override if self._sidebar_override is not None else self.size.width >= 100
        sidebar = sidebar and not self._listening
        self._main_screen.query_one("#sidebar").display = sidebar
        self._main_screen.query_one("#view-tabs").display = not sidebar and not self._listening
        self._main_screen.set_class(self.size.height < 29, "compact")
        self._main_screen.set_class(self._listening, "listening")
        self._main_screen.query_one("#search").display = not self._listening
        self._main_screen.query_one("#collection-header").display = not self._listening and self.size.height >= 29
        self._main_screen.query_one("#listening-stage").display = self._listening
        available = self.size.width - (24 if sidebar else 0)
        wide = available >= 90
        if not self._lyrics_user_toggled:
            self._lyrics_visible = wide
        compact_lyrics = self._lyrics_visible and not wide
        self._main_screen.query_one("#results-pane").display = not compact_lyrics and not self._listening
        self._main_screen.query_one("#queue-pane").display = available >= 68 and not compact_lyrics and not self._listening
        pane = self._main_screen.query_one("#lyrics-pane")
        was_visible = pane.display
        pane.display = (self._listening_lyrics if self._listening_lyrics is not None else
                        self._lyrics_visible if self._lyrics_user_toggled else self.size.width >= 100) if self._listening else self._lyrics_visible
        if self._listening and self.size.width < 100:
            self._main_screen.query_one("#listening-stage").display = not pane.display
        if self._lyrics_visible and not was_visible and not self._reduced_motion:
            pane.styles.opacity = 0.0
            pane.styles.animate("opacity", 1.0, duration=0.14, easing="out_cubic", level="basic")
        self._main_screen.query_one("#library").set_class(compact_lyrics and not self._listening, "compact-lyrics")
        self._main_screen.query_one("#listening-toggle", Button).label = "‹ Browse" if self._listening else "Listen"
        if pane.display:
            self.call_after_refresh(self._render_lyrics, force=True)
        self.call_after_refresh(self._fit_tables)

    def _fit_tables(self) -> None:
        """Reflow existing metadata after layout changes without fetching again."""
        if self._main_screen.query_one("#results-pane").display and not self._library_loading and self._view != "playlists":
            self._render_tracks(self.results, preserve_selection=True)
        if self._main_screen.query_one("#queue-pane").display:
            self._render_queue_table()

    def action_listening(self) -> None:
        self._listening = not self._listening
        if self._listening:
            self._listening_lyrics = None
        self._layout_lyrics()
        self._main_screen.query_one("#pause" if self._listening else "#results").focus()
        self._status("Listening room · F3 returns to your library · l toggles lyrics" if self._listening else "Back to your library · F3 opens the listening room")

    def _status(self, message: str, *, error: bool = False) -> None:
        widget = self._main_screen.query_one("#status", Static)
        widget.update(_plain(message))
        widget.set_class(error, "error")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.screen_stack and isinstance(self.screen, ModalScreen):
            if action == "quit" and isinstance(self.screen, SettingsScreen) and self.screen.installing_update:
                return False
            if action in {
                "search_focus", "command_palette", "sidebar", "import_playlist", "settings", "listening",
                "discover", "favorites", "recent", "playlists", "favorite", "repeat", "shuffle",
                "theme", "seek_back", "seek_forward", "queue_remove", "queue_up", "queue_down",
                "enqueue", "play", "toggle_pause", "next", "volume_down", "volume_up", "mute",
                "lyrics", "visualizer", "stats", "help",
            }:
                return False
        return super().check_action(action, parameters)

    def _apply_preferences(self, preferences: Preferences) -> None:
        self._preferences = preferences
        self.theme = preferences.theme
        self._main_screen.query_one("#record-art").refresh()
        self._listening_lyrics = None
        self._sidebar_override = {"auto": None, "show": True, "hide": False}[preferences.sidebar]
        self._lyrics_user_toggled = preferences.lyrics != "auto"
        if self._lyrics_user_toggled:
            self._lyrics_visible = preferences.lyrics == "show"
        requested_motion = (self._explicit_reduced_motion if self._explicit_reduced_motion is not None
                            else preferences.reduced_motion or reduced_motion_requested())
        if requested_motion != self._reduced_motion:
            self._reduced_motion = requested_motion
            self.animation_level = "none" if requested_motion else "full"
            self._audio_motion.reduced_motion = requested_motion
            self._audio_motion.reset()
            self._render_timer.stop()
            self._render_timer = self.set_interval(0.12 if requested_motion else 1 / 30, self.tick_playback)
        if self._visualizer_enabled != preferences.visualizer:
            self._visualizer_enabled = preferences.visualizer
            self._audio_history.clear()
            self._audio_motion.reset()
            self.configure_visualizer()
        self._main_screen.query_one("#audio-meter").display = preferences.visualizer
        self._main_screen.query_one("#player").set_class(not preferences.visualizer, "no-visualizer")
        self._audio_rendered = None
        self._layout_lyrics()
        self._render_lyrics(force=True)

    def action_settings(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        volume = self._snapshot.get("volume", self._preferences.volume)
        if isinstance(volume, (int, float)) and not isinstance(volume, bool) and math.isfinite(volume):
            volume = max(0, min(100, int(volume)))
        else:
            volume = self._preferences.volume
        self.push_screen(SettingsScreen(self.service, replace(self._preferences, volume=volume)), self._settings_saved)

    def _settings_saved(self, preferences: Preferences | None) -> None:
        if preferences is None:
            return
        start_check = preferences.auto_check_updates and not self._preferences.auto_check_updates
        self._preferences_user_changed = True
        self._theme_user_changed = True
        self._apply_preferences(preferences)
        self.refresh_playback()
        self._status("Settings saved · F2 opens preferences and updates")
        if start_check:
            self.check_updates_on_launch()

    @work(exclusive=True, group="startup-update-check", exit_on_error=False)
    async def check_updates_on_launch(self) -> None:
        try:
            info = await asyncio.to_thread(self.service.check_update, False)
            if info.available and self._preferences.auto_check_updates:
                self.notify(f"MYNE {info.latest_version} is available. Open Settings → Updates (F2).", timeout=8)
        except Exception:
            # Optional background checks never block music or replace its status.
            pass

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        commands = (
            ("Search music", "Find a song or artist", self.action_search_focus),
            ("Discover", "Return to your search results", self.action_discover),
            ("Liked tracks", "Open your saved favorites", self.action_favorites),
            ("Recently played", "Songs from your listening history", self.action_recent),
            ("Playlists", "Browse saved and imported collections", self.action_playlists),
            ("Import playlist link", "Spotify, YouTube Music, YouTube or SoundCloud", self.action_import_playlist),
            ("Toggle sidebar", "Show your library and playlists", self.action_sidebar),
            ("Settings", "Appearance, playback preferences and updates", self.action_settings),
            ("Listening room", "Vinyl view and lyrics · F3 returns to your library", self.action_listening),
            ("Like / unlike track", "Save the selected track or current song", self.action_favorite),
            ("Save queue", "Give the upcoming queue a name", self.action_save_playlist),
            ("Shuffle queue", "Randomize upcoming tracks", self.action_shuffle),
            ("Repeat mode", "Cycle off, all tracks, or one track", self.action_repeat),
            ("Change theme", "Studio, Midnight, or Ember", self.action_theme),
            ("Seek backward", "Rewind ten seconds", self.action_seek_back),
            ("Seek forward", "Skip forward ten seconds", self.action_seek_forward),
            ("Listening statistics", "Open your listening history summary", self.action_stats),
            ("Lyrics", "Show or hide live lyrics", self.action_lyrics),
            ("Visualizer", "Show or hide measured audio levels", self.action_visualizer),
            ("Help", "All keyboard controls", self.action_help),
            ("Quit", "Close the UI and keep music playing", self.action_quit),
        )
        for title, help_text, callback in commands:
            yield SystemCommand(title, help_text, callback)

    def _activate_view(self, view: str) -> None:
        if self._listening:
            self._listening = False
            self._layout_lyrics()
        self._view = view
        self._open_playlist = None
        self._library_loading = view in {"playlist", "playlists", "favorites", "recent"}
        if self._library_loading:
            self.results = []
            self._playlist_names = []
            self._selected_result = 0
            self._main_screen.query_one("#results", DataTable).clear()
            self._main_screen.query_one("#results-empty", Static).update("Opening your library…")
            self._main_screen.query_one("#results-empty").display = True
            titles = {"playlist": "PLAYLIST", "playlists": "YOUR PLAYLISTS",
                      "favorites": "LIKED TRACKS", "recent": "RECENTLY PLAYED"}
            self._main_screen.query_one("#results-pane").border_title = titles[view]
            self._main_screen.query_one("#results-pane").border_subtitle = ""
        self._main_screen.query_one("#playlist-tools").display = view in {"playlists", "playlist"}
        for button_id in ("playlist-back", "playlist-play", "playlist-queue", "playlist-info"):
            self._main_screen.query_one(f"#{button_id}", Button).display = view == "playlist"
        for button_id in ("playlist-play", "playlist-queue", "playlist-info"):
            self._main_screen.query_one(f"#{button_id}", Button).disabled = True
        for button in self._main_screen.query(".view-button"):
            selected_view = "playlists" if view == "playlist" else view
            button.set_class(button.id in {f"rail-{selected_view}", f"tab-{selected_view}"}, "selected")
        self._render_collection()

    def _render_tracks(self, tracks: list[Track], *, preserve_selection: bool = False) -> None:
        selected = self._selected_result if preserve_selection else 0
        self._library_loading = False
        self.results = list(tracks)
        self._playlist_names = []
        self._selected_result = min(selected, max(0, len(tracks) - 1))
        table = self._main_screen.query_one("#results", DataTable)
        table.clear(columns=True)
        # Reserve one column for the vertical scrollbar; never force a
        # horizontal scrollbar into the few rows available in compact mode.
        width = (table.size.width or 48) - 1
        artist_width = max(6, width // 3 - 3)
        table.add_column("Track", width=max(8, width - artist_width - 14))
        table.add_column("Artist", width=artist_width)
        table.add_column("Time", width=5)
        table.add_column("♥", width=1)
        for index, track in enumerate(tracks):
            liked = (track.provider, track.id) in self._favorites
            table.add_row(Text(_plain(track.title), no_wrap=True, overflow="ellipsis"),
                          Text(_plain(", ".join(track.artists) or "Unknown"), no_wrap=True, overflow="ellipsis"),
                          Text(_clock(track.duration)), Text("♥" if liked else "", style=self.current_theme.primary),
                          key=str(index))
        if tracks:
            table.move_cursor(row=self._selected_result)
        titles = {"discover": "DISCOVER", "favorites": "LIKED TRACKS", "recent": "RECENTLY PLAYED",
                  "playlist": _plain(self._open_playlist.name) if self._open_playlist else "PLAYLIST"}
        self._main_screen.query_one("#results-pane").border_title = Text(titles.get(self._view, "DISCOVER"))
        self._main_screen.query_one("#results-pane").border_subtitle = f"{len(tracks)} tracks · f to like"
        empty = self._main_screen.query_one("#results-empty", Static)
        empty.display = not bool(tracks)
        messages = {"favorites": "Like a track with f to save it here.",
                    "recent": "Played tracks appear after they end or stop.",
                    "discover": "Search for a song, artist, or /mix mood."}
        empty.update(messages.get(self._view, "Your library is empty."))
        self._update_library_counts()
        self._update_favorite_label()
        self._render_collection()

    def _render_collection(self) -> None:
        titles = {"discover": self._search_heading, "favorites": "The ones you love.",
                  "recent": "Back in rotation.", "playlists": "Your collections.",
                  "playlist": self._open_playlist.name if self._open_playlist else "Opening your collection…"}
        title = titles.get(self._view, "Your music.")
        if self._view == "playlists":
            meta = f"{len(self._playlist_names)} playlists · Import a link with Ctrl+O"
        elif self._library_loading:
            meta = "Opening your library…"
        elif self.results:
            durations = [track.duration for track in self.results if track.duration is not None]
            duration = f" · {_clock(sum(durations))}" if durations else ""
            source = f" · {self._open_playlist.source}" if self._open_playlist else ""
            meta = f"{len(self.results)} tracks{duration}{source} · Enter to play"
        else:
            meta = "Search a song, artist, or mood. Paste a playlist link to import."
        self._main_screen.query_one("#collection-title", Static).update(_plain(title))
        self._main_screen.query_one("#collection-meta", Static).update(_plain(meta))

    def _update_library_counts(self) -> None:
        self._main_screen.query_one("#library-count", Static).update(
            f"{len(self._favorites)} liked tracks\n{len(self.queued)} upcoming\nStored locally"
        )

    def _update_favorite_label(self) -> None:
        selected = self._selected()
        data = self._snapshot.get("track")
        track = selected[0] if selected else Track.from_dict(data) if data else None
        liked = track is not None and (track.provider, track.id) in self._favorites
        self._main_screen.query_one("#favorite", Button).label = "♥ Liked" if liked else "♡ Like"

    @work(group="library-startup", exit_on_error=False)
    async def bootstrap_library(self) -> None:
        try:
            favorites, preferences = await asyncio.to_thread(lambda: (self.service.favorites(), self.service.preferences()))
            self._favorites = {(track.provider, track.id) for track in favorites}
            if not self._preferences_user_changed:
                values = preferences.to_dict()
                for key in self._preference_overrides:
                    values[key] = getattr(self._preferences, key)
                preferences = Preferences.from_dict(values)
                if self._theme_user_changed:
                    preferences = replace(preferences, theme=self.theme)
                self._apply_preferences(preferences)
                if preferences.auto_check_updates:
                    self.check_updates_on_launch()
            if self._view == "discover":
                self._render_tracks(self._discover_results)
            self._update_library_counts()
            self.refresh_sidebar()
        except Exception as exc:
            self._status(f"Library unavailable: {exc}", error=True)

    def action_discover(self) -> None:
        self._activate_view("discover")
        self._render_tracks(self._discover_results)
        self._status("Discover · search above, or Enter to play a result.")

    def action_favorites(self) -> None:
        self._activate_view("favorites")
        self.load_library("favorites")

    def action_recent(self) -> None:
        self._activate_view("recent")
        self.load_library("recent")

    def action_playlists(self) -> None:
        self._activate_view("playlists")
        self.load_library("playlists")

    def action_sidebar(self) -> None:
        self._listening = False
        self._sidebar_override = not self._main_screen.query_one("#sidebar").display
        self._preferences = replace(self._preferences, sidebar="show" if self._sidebar_override else "hide")
        self._preference_overrides.add("sidebar")
        self._layout_lyrics()
        if self._sidebar_override:
            self._main_screen.query_one("#rail-playlists", Button).focus()
        else:
            self._main_screen.query_one("#results", DataTable).focus()

    @work(exclusive=True, group="sidebar-library", exit_on_error=False)
    async def refresh_sidebar(self) -> None:
        try:
            playlists = await asyncio.to_thread(self.service.playlists)
            self._sidebar_playlist_names = [name for name, _ in playlists]
            options = self._main_screen.query_one("#sidebar-playlists", OptionList)
            options.clear_options()
            width = max(10, (options.content_size.width or 20) - 4)
            for index, (name, count) in enumerate(playlists):
                count_label = f" · {count}"
                label = Text(_plain(name), no_wrap=True, overflow="ellipsis")
                label.truncate(max(4, width - len(count_label)), overflow="ellipsis")
                label.append(count_label)
                options.add_option(Option(label, id=str(index)))
            options.highlighted = (self._sidebar_playlist_names.index(self._open_playlist.name)
                                   if self._open_playlist and self._open_playlist.name in self._sidebar_playlist_names
                                   else 0 if playlists else None)
        except Exception:
            self._status("Could not refresh your playlists. Try opening the library again.", error=True)

    @on(OptionList.OptionSelected, "#sidebar-playlists")
    def choose_sidebar_playlist(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._sidebar_playlist_names):
            self.open_playlist(self._sidebar_playlist_names[event.option_index])

    @work(exclusive=True, group="library-view", exit_on_error=False)
    async def open_playlist(self, name: str) -> None:
        self._activate_view("playlist")
        self._status("Opening playlist…")
        try:
            playlist = await asyncio.to_thread(self.service.playlist, name)
            if self._view != "playlist":
                return
            self._open_playlist = playlist
            for button_id in ("playlist-play", "playlist-queue", "playlist-info"):
                self._main_screen.query_one(f"#{button_id}", Button).disabled = not bool(playlist.tracks)
            if name in self._sidebar_playlist_names:
                self._main_screen.query_one("#sidebar-playlists", OptionList).highlighted = self._sidebar_playlist_names.index(name)
            self._render_tracks(list(playlist.tracks))
            self._selection_source = "results"
            if not isinstance(self.screen, ModalScreen):
                self._main_screen.query_one("#results", DataTable).focus()
            notes = ""
            if any("partial playlist" in warning for warning in playlist.warnings):
                notes = " · Public view may be partial · Info shows import notes"
            elif playlist.warnings:
                notes = " · Info shows source limits and unmatched songs"
            else:
                notes = " · Enter plays · + All queues the playlist"
            self._status(f"{playlist.source} · {len(playlist.tracks)} tracks{notes}")
        except Exception as exc:
            if self._view == "playlist":
                self._library_loading = False
                self._main_screen.query_one("#results-empty", Static).update("Could not open this playlist. Choose another or try again.")
                self._status(f"Could not open playlist: {exc}", error=True)

    def action_import_playlist(self) -> None:
        self.push_screen(ImportPlaylistScreen(self.service.import_playlist), self._playlist_imported)

    def _playlist_imported(self, result: ImportResult | None) -> None:
        if result is None:
            self._status("Import cancelled.")
            return
        self.refresh_sidebar()
        self.open_playlist(result.playlist.name)

    def action_playlist_play(self) -> None:
        if self._open_playlist and self._open_playlist.tracks:
            self.mutate("Playing", self.service.play, list(self._open_playlist.tracks))

    def action_playlist_queue(self) -> None:
        if self._open_playlist and self._open_playlist.tracks:
            self.mutate("Playlist added to queue", self.service.enqueue, list(self._open_playlist.tracks))

    def action_playlist_info(self) -> None:
        if playlist := self._open_playlist:
            body = f"{playlist.source} · {len(playlist.tracks)} tracks\n"
            if playlist.source_url:
                body += f"\nSource: {playlist.source_url}\n"
            body += "\n" + "\n".join(playlist.warnings)
            if playlist.source in {"Spotify", "SoundCloud"}:
                body += "\n\nThese songs are matched YouTube versions. No audio is streamed from " + playlist.source + "."
            body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", body)
            self.push_screen(DetailScreen(_plain(playlist.name), body))

    @work(exclusive=True, group="library-view", exit_on_error=False)
    async def load_library(self, view: str) -> None:
        self._status("Opening your library…")
        try:
            if view == "playlists":
                playlists = await asyncio.to_thread(self.service.playlists)
                if self._view != view:
                    return
                self.results = []
                self._library_loading = False
                self._playlist_names = [name for name, _count in playlists]
                self._selected_result = 0
                table = self._main_screen.query_one("#results", DataTable)
                table.clear(columns=True)
                table.add_columns("Playlist", "Tracks")
                for index, (name, count) in enumerate(playlists):
                    table.add_row(Text(_plain(name)), str(count), key=str(index))
                self._main_screen.query_one("#results-pane").border_title = "YOUR PLAYLISTS"
                self._main_screen.query_one("#results-pane").border_subtitle = "Enter to open · Ctrl+O to import"
                self._render_collection()
                empty = self._main_screen.query_one("#results-empty", Static)
                empty.display = not bool(playlists)
                empty.update("Import a playlist link, or Save your upcoming queue.")
                self._status(f"{len(playlists)} playlists · Enter opens the tracks · Ctrl+O imports a link")
                self.refresh_sidebar()
            else:
                read = self.service.favorites if view == "favorites" else self.service.recent_tracks
                tracks = await asyncio.to_thread(read)
                if self._view != view:
                    return
                if view == "favorites":
                    self._favorites = {(track.provider, track.id) for track in tracks}
                self._render_tracks(tracks)
                self._status(f"{len(tracks)} tracks · Enter plays · a queues · f likes")
            self._selection_source = "results"
            if not isinstance(self.screen, ModalScreen):
                self._main_screen.query_one("#results", DataTable).focus()
        except Exception as exc:
            if self._view == view:
                self._library_loading = False
                self._main_screen.query_one("#results-empty", Static).update("Could not open your library. Try again.")
                self._status(f"Could not open library: {exc}", error=True)

    def action_favorite(self) -> None:
        tracks = self._selected()
        data = self._snapshot.get("track")
        track = tracks[0] if tracks else Track.from_dict(data) if data else None
        if track is None:
            self._status("Select a track or play a song to like it.")
            return
        self.like_track(track)

    @work(group="library-like", exit_on_error=False)
    async def like_track(self, track: Track) -> None:
        try:
            async with self._mutation_lock:
                liked = await asyncio.to_thread(self.service.toggle_favorite, track)
            key = (track.provider, track.id)
            if liked:
                self._favorites.add(key)
            else:
                self._favorites.discard(key)
            if self._view == "favorites":
                self.load_library("favorites")
            elif self._view != "playlists":
                selected = self._selected_result
                self._render_tracks(self.results)
                if self.results:
                    self._main_screen.query_one("#results", DataTable).move_cursor(row=selected)
            self._update_library_counts()
            self._status(f"{'Liked' if liked else 'Unliked'} · {track.title}")
        except Exception as exc:
            self._status(f"Could not save favorite: {exc}", error=True)

    def action_theme(self) -> None:
        self._theme_user_changed = True
        index = THEME_NAMES.index(self.theme) if self.theme in THEME_NAMES else -1
        self.theme = THEME_NAMES[(index + 1) % len(THEME_NAMES)]
        self._main_screen.query_one("#record-art").refresh()
        self._preferences = replace(self._preferences, theme=self.theme)
        self._audio_rendered = None
        self._render_lyrics(force=True)
        self._status(f"Theme · {self.theme.removeprefix('myne-').title()}")
        self.persist_theme(self.theme)

    @work(group="theme-save", exit_on_error=False)
    async def persist_theme(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.service.save_ui_theme, name)
        except Exception as exc:
            self._status(f"Could not save theme: {exc}", error=True)

    def action_save_playlist(self) -> None:
        if not self.queued:
            self._status("Add upcoming tracks before saving a queue.")
            return
        self.push_screen(SaveQueueScreen(), self._save_playlist_named)

    def _save_playlist_named(self, name: str | None) -> None:
        if name is not None:
            def save() -> int:
                return self.service.save_playlist(name)
            self.save_named_queue(name, save)

    @work(group="playlist-save", exit_on_error=False)
    async def save_named_queue(self, name: str, save: Callable[[], int]) -> None:
        try:
            async with self._mutation_lock:
                await asyncio.to_thread(save)
            self.refresh_sidebar()
            self._status(f"Saved queue · {name}")
            if self._view == "playlists":
                self.load_library("playlists")
        except Exception as exc:
            self._status(f"Could not save queue: {exc}", error=True)

    def _open_selected_playlist(self) -> None:
        if 0 <= self._selected_result < len(self._playlist_names):
            name = self._playlist_names[self._selected_result]
            self.open_playlist(name)

    def _queue_target(self) -> Track | None:
        if self._selection_source == "queue" and 0 <= self._selected_queue < len(self.queued):
            return self.queued[self._selected_queue]
        self._status("Select a track in your queue first.")
        return None

    def action_queue_remove(self) -> None:
        if track := self._queue_target():
            self.mutate("Removed from queue", self.service.queue_remove, self._selected_queue, track)

    def action_queue_up(self) -> None:
        self._move_queued(-1)

    def action_queue_down(self) -> None:
        self._move_queued(1)

    def _move_queued(self, step: int) -> None:
        if track := self._queue_target():
            self.mutate("Queue updated", self.service.queue_move, self._selected_queue, step, track)

    def action_shuffle(self) -> None:
        self.mutate("Upcoming queue shuffled", self.service.queue_shuffle)

    def action_repeat(self) -> None:
        self.mutate("Repeat updated", self.service.control, "repeat")

    def action_seek_back(self) -> None:
        self.mutate("Rewound 10 seconds", lambda: self.service.control("seek", seconds=-10))

    def action_seek_forward(self) -> None:
        self.mutate("Skipped forward 10 seconds", lambda: self.service.control("seek", seconds=10))

    def action_stop(self) -> None:
        self.mutate("Playback stopped · upcoming queue saved", self.service.control, "stop")

    @on(Input.Submitted, "#search")
    def submit_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            self._status("Type a song, artist, /mix mood, or /radio song.")
            return
        if query.startswith(("https://", "spotify:playlist:")):
            try:
                playlist_link(query)
            except Exception:
                pass
            else:
                self.push_screen(ImportPlaylistScreen(self.service.import_playlist, query), self._playlist_imported)
                return
        self.search_music(query)

    @work(exclusive=True, group="search", exit_on_error=False)
    async def search_music(self, query: str) -> None:
        """Cancelling a search discards its result without blocking the UI."""
        self._status("Searching…")
        self._search_heading = f"Results for {query}" if not query.startswith("/") else query.split(" ", 1)[-1].capitalize() + " mix"
        if not query.startswith("/radio "):
            self._activate_view("discover")
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
        self._discover_results = list(tracks)
        if self._view != "discover":
            return
        self._render_tracks(self._discover_results)
        if heading.startswith("MIX"):
            self._main_screen.query_one("#results-pane").border_title = "SMART MIX"
        self._status(
            f"{len(self.results)} tracks · Enter plays · a queues · f likes"
            if self.results else "No tracks found. Try another search."
        )
        if self.results:
            self._main_screen.query_one("#results", DataTable).focus()

    @on(DataTable.RowHighlighted)
    def highlight_track(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "results":
            self._selected_result = event.cursor_row
        elif event.data_table.id == "queue":
            self._selected_queue = event.cursor_row
        self._update_favorite_label()

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
        if self._listening:
            return []
        focused = self.focused
        source = focused.id if isinstance(focused, DataTable) else self._selection_source
        if source == "queue":
            self._selection_source = "queue"
            if 0 <= self._selected_queue < len(self.queued):
                return [self.queued[self._selected_queue]]
            return []
        self._selection_source = "results"
        if self._library_loading:
            return []
        if 0 <= self._selected_result < len(self.results):
            return [self.results[self._selected_result]]
        return []

    def action_search_focus(self) -> None:
        if self._listening:
            self._listening = False
            self._layout_lyrics()
        self._main_screen.query_one("#search", Input).focus()

    def action_play(self) -> None:
        tracks = self._selected()
        if self._view == "playlists" and self._selection_source != "queue":
            self._open_selected_playlist()
            return
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
        button_id = event.button.id or ""
        if button_id.startswith(("rail-", "tab-")):
            view = button_id.split("-", 1)[1]
            {"discover": self.action_discover, "favorites": self.action_favorites,
             "recent": self.action_recent, "playlists": self.action_playlists}[view]()
            return
        if button_id.startswith("queue-"):
            self._selection_source = "queue"
        actions: dict[str, Callable[[], Any]] = {
            "play": self.action_play,
            "add": self.action_enqueue,
            "pause": self.action_toggle_pause,
            "next": self.action_next,
            "volume-down": self.action_volume_down,
            "volume-up": self.action_volume_up,
            "mute": self.action_mute,
            "repeat": self.action_repeat,
            "seek-back": self.action_seek_back,
            "seek-forward": self.action_seek_forward,
            "favorite": self.action_favorite,
            "stop": self.action_stop,
            "commands": self.action_command_palette,
            "theme": self.action_theme,
            "settings": self.action_settings,
            "sidebar-toggle": self.action_sidebar,
            "listening-toggle": self.action_listening,
            "import-playlist": self.action_import_playlist,
            "playlist-import": self.action_import_playlist,
            "playlist-back": self.action_playlists,
            "playlist-play": self.action_playlist_play,
            "playlist-queue": self.action_playlist_queue,
            "playlist-info": self.action_playlist_info,
            "queue-up": self.action_queue_up,
            "queue-down": self.action_queue_down,
            "queue-remove": self.action_queue_remove,
            "queue-shuffle": self.action_shuffle,
            "queue-save": self.action_save_playlist,
            "mood-focus": lambda: self.search_music("/mix focus"),
            "mood-night": lambda: self.search_music("/mix night drive"),
        }
        if action := actions.get(event.button.id or ""):
            action()

    @work(group="mutations", exit_on_error=False)
    async def mutate(self, message: str, function: Callable[..., Any], *args: Any) -> None:
        try:
            async with self._mutation_lock:
                result = await asyncio.to_thread(function, *args)
            if isinstance(result, dict) and "queue_index" in result:
                self._selected_queue = int(result["queue_index"])
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
                self.queued = tracks
                self._selected_queue = min(self._selected_queue, max(0, len(tracks) - 1))
                self._render_queue_table()
            self._main_screen.query_one("#queue-empty").display = not bool(tracks)
            self._main_screen.query_one("#queue-pane").border_title = f"MY QUEUE · {len(tracks)}"
            self._main_screen.query_one("#queue-pane").border_subtitle = f"{_clock(sum(track.duration or 0 for track in tracks))} queued" if tracks else "a to add tracks"
            self._update_library_counts()
            for button_id in ("queue-up", "queue-down", "queue-remove", "queue-save"):
                self._main_screen.query_one(f"#{button_id}", Button).disabled = not bool(tracks)
            self._main_screen.query_one("#queue-shuffle", Button).disabled = len(tracks) < 2
        except Exception as exc:
            self._status(f"Playback unavailable: {exc}", error=True)
        finally:
            self._refreshing = False

    def _render_queue_table(self) -> None:
        table = self._main_screen.query_one("#queue", DataTable)
        table.clear(columns=True)
        table.add_column("Up next", width=max(8, (table.size.width or 24) - 7))
        compact = self.size.height < 29
        for index, track in enumerate(self.queued):
            label = Text(_plain(track.title), style="bold", no_wrap=True, overflow="ellipsis")
            label.append((" · " if compact else "\n") + _plain(", ".join(track.artists) or "Unknown artist"), style="not bold dim")
            table.add_row(label, label=Text(f"{index + 1:02}", style="dim"), height=1 if compact else 2, key=str(index))
        if self.queued:
            table.move_cursor(row=self._selected_queue)

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
            self._main_screen.query_one("#listening-title", Static).update(_plain(track.get("title") or "Unknown track"))
            self._main_screen.query_one("#listening-artist", Static).update(_plain(artists or "Unknown artist"))
            self._main_screen.query_one("#listening-album", Static).update(_plain(track.get("album") or ""))
        else:
            self._main_screen.query_one("#now", Static).update("Nothing playing · search to begin")
            self._main_screen.query_one("#listening-title", Static).update("Make room for your next favorite.")
            self._main_screen.query_one("#listening-artist", Static).update("Search for a song or start your queue.")
            self._main_screen.query_one("#listening-album", Static).update("")
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
        repeat = snapshot.get("repeat", "off")
        self._main_screen.query_one("#repeat", Button).label = f"Loop: {repeat}"
        state = "MUTED" if muted or volume == 0 else "PAUSED" if snapshot.get("paused") else "PLAYING" if snapshot.get("playing") else "LOADING" if track else "READY"
        self._main_screen.query_one("#session-state", Static).update(f"{state}  /  REPEAT {str(repeat).upper()}")
        for button_id in ("stop", "seek-back", "seek-forward"):
            self._main_screen.query_one(f"#{button_id}", Button).disabled = not bool(track)
        error = snapshot.get("error")
        if error and error != self._last_player_error:
            self._status(str(error), error=True)
        self._last_player_error = error
        self._update_favorite_label()

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
        self._preferences = replace(self._preferences, visualizer=self._visualizer_enabled)
        self._preference_overrides.add("visualizer")
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
            rendered.append("L " if channel == 0 else "R ", style="dim")
            rendered.append_text(_meter(value, peak, width, self.current_theme.primary))
            rendered.append(f" {value * 60 - 60:5.1f} dBFS" if value > 0 else " ≤−60 dBFS")
            rendered.append("\n")
        if len(rms) == 1:
            rendered.append("MONO · actual mpv RMS / peak\n", style="dim")
        symbols = "▁▂▃▄▅▆▇█"
        history = list(self._audio_history)[-max(1, meter.size.width - 15) :]
        rendered.append("RMS HISTORY  ", style="dim")
        rendered.append(
            "".join(symbols[min(7, round(_level(value) * 7))] for value in history), style=self.current_theme.primary
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
                text.append("› " if line_index == index else "  ", style=self.current_theme.primary)
                text.append(
                    _plain(line.text) or "♪",
                    style=(
                        f"bold {self.current_theme.primary}"
                        if line_index == index
                        else ""
                        if line_index > index
                        else "dim"
                    ),
                )
                text.append("\n")
            body.update(text)
            source.update(_plain(f"{document.source} · synced · l hides"))
            if not self._main_screen.query_one("#lyrics-pane").display:
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
        if self._lyrics_key != key or self._lyric_index != index or not self._main_screen.query_one("#lyrics-pane").display:
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
                "Ctrl+K       Search all actions in the command palette\n"
                "1 / 2 / 3 / 4  Discover / Liked / Recent / Playlists\n"
                "Ctrl+B       Show / hide the sidebar at any terminal size\n"
                "Ctrl+O       Import a playlist link\n"
                "F2           Settings and update checks\n"
                "F3           Listening room / return to library\n"
                "Enter        Search; play a selected result or queue track\n"
                "↑ / ↓        Select tracks\n"
                "Tab          Move between panes and controls\n"
                "p            Play the selected track\n"
                "a            Add the selected track to your queue\n\n"
                "f            Like / unlike a selected track or the current song\n"
                "Delete       Remove the selected queued track\n"
                "Alt+↑ / ↓    Move the selected queued track\n"
                "z            Shuffle upcoming tracks\n"
                "Save         Name the upcoming queue; Enter opens a playlist\n"
                "PLAYLISTS\n"
                "Paste a Spotify, YouTube Music, YouTube, or SoundCloud playlist link.\n"
                "Use ▶ All to play or + All to append the full playlist.\n"
                "Info shows the source link, partial imports, and unmatched songs.\n\n"
                "PLAYBACK\n"
                "Space        Start queue / pause / resume\n"
                "n            Next track\n"
                "[ / ]        Lower / raise volume (raising also unmutes)\n"
                "m            Mute / unmute\n"
                ", / .        Seek backward / forward 10 seconds\n"
                "r            Repeat off / all / one (radio turns repeat off)\n"
                "t            Switch Studio / Midnight / Ember; saved locally\n"
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
        if self._listening:
            self._listening_lyrics = not self._main_screen.query_one("#lyrics-pane").display
            self._layout_lyrics()
            self._main_screen.query_one("#lyrics-scroll" if self._listening_lyrics else "#pause").focus()
            self._status("Listening room · l toggles lyrics · F3 returns to your library")
            return
        self._lyrics_user_toggled = True
        self._lyrics_visible = not self._lyrics_visible
        self._preferences = replace(self._preferences, lyrics="show" if self._lyrics_visible else "hide")
        self._preference_overrides.add("lyrics")
        self._layout_lyrics()
        if self._lyrics_visible:
            self._main_screen.query_one("#lyrics-scroll", VerticalScroll).focus()
            self._render_lyrics(force=True)
            self._status("Live lyrics · l returns to tracks · Tab moves focus · ↑ ↓ scroll plain lyrics")
        else:
            self._main_screen.query_one("#pause" if self._listening else "#results").focus()
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
