"""Focused modal flows around the music workspace."""

import asyncio
import re
from collections.abc import Callable
from threading import Event

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ProgressBar, Static

from myne.errors import MyneError
from myne.playlists import ImportResult, playlist_link


class SaveQueueScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    SaveQueueScreen { align: center middle; background: $background 85%; }
    SaveQueueScreen > Vertical {
        width: 62; max-width: 92%; height: auto; padding: 1 2;
        background: $surface; border: solid $primary;
    }
    SaveQueueScreen #save-heading { height: 2; color: $primary; text-style: bold; }
    SaveQueueScreen #save-hint { height: 2; color: $text-muted; }
    SaveQueueScreen #save-error { height: 1; color: $error; }
    SaveQueueScreen Horizontal { height: 3; align-horizontal: right; }
    SaveQueueScreen Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("SAVE YOUR QUEUE", id="save-heading")
            yield Static("Name the upcoming tracks. The current song is excluded.", id="save-hint")
            yield Input(placeholder="e.g. Late night rotation", max_length=80, id="playlist-name")
            yield Static("", id="save-error")
            with Horizontal():
                yield Button("Cancel", id="cancel-save")
                yield Button("Save queue", id="confirm-save", variant="primary")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.styles.line_pad = 0
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#confirm-save")
    def save(self) -> None:
        name = self.query_one(Input).value.strip()
        if not name:
            self.query_one("#save-error", Static).update("Enter a name for this queue.")
            return
        self.dismiss(name)

    @on(Button.Pressed, "#cancel-save")
    def cancel(self) -> None:
        self.dismiss(None)


class ImportPlaylistScreen(ModalScreen[ImportResult | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ImportPlaylistScreen { align: center middle; background: $background 85%; }
    ImportPlaylistScreen > Vertical {
        width: 76; max-width: 96%; height: auto; max-height: 96%; padding: 1 2;
        background: $surface; border: solid $primary; overflow-y: auto;
    }
    ImportPlaylistScreen #import-heading { height: 1; color: $primary; text-style: bold; }
    ImportPlaylistScreen .import-hint { height: auto; color: $text-muted; }
    ImportPlaylistScreen #import-status { height: auto; min-height: 1; max-height: 3; }
    ImportPlaylistScreen #import-progress { height: 1; }
    ImportPlaylistScreen Input { border: solid $panel; padding: 0 1; background: $background; }
    ImportPlaylistScreen Input:focus { border: solid $primary; }
    ImportPlaylistScreen Horizontal { height: 3; align-horizontal: right; }
    ImportPlaylistScreen Button { margin-left: 1; border: none; }
    """

    def __init__(self, import_playlist: Callable[..., ImportResult], link: str = "") -> None:
        super().__init__()
        self.import_playlist = import_playlist
        self.link = link
        self._import_cancel = Event()
        self._import_running = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("BRING YOUR PLAYLISTS", id="import-heading")
            yield Static("Spotify · YouTube Music · YouTube · SoundCloud", classes="import-hint")
            yield Input(self.link, placeholder="Paste a full playlist link", max_length=2048, id="import-link")
            yield Input(placeholder="Playlist name (optional)", max_length=80, id="import-name")
            yield Static("Songs are matched for YouTube playback. Spotify's public view may be partial. "
                         "Your current queue keeps playing.", classes="import-hint")
            yield Static("Ready when you are.", id="import-status", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="import-progress")
            with Horizontal():
                yield Button("Cancel", id="cancel-import")
                yield Button("Import playlist", id="confirm-import", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#import-link", Input).focus()

    def on_unmount(self) -> None:
        self._import_cancel.set()

    def action_cancel(self) -> None:
        if self._import_running:
            self._import_cancel.set()
            self.query_one("#import-status", Static).update("Cancelling after the current request…")
            self.query_one("#cancel-import", Button).disabled = True
        else:
            self.dismiss(None)

    @on(Button.Pressed, "#cancel-import")
    def cancel_import(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    @on(Button.Pressed, "#confirm-import")
    def confirm_import(self) -> None:
        if self._import_running:
            return
        link = self.query_one("#import-link", Input).value.strip()
        try:
            playlist_link(link)
        except MyneError as exc:
            self.query_one("#import-status", Static).update(str(exc))
            return
        self._import_running = True
        self._import_cancel.clear()
        for widget in self.query(Input):
            widget.disabled = True
        self.query_one("#confirm-import", Button).disabled = True
        self.run_import(link, self.query_one("#import-name", Input).value.strip() or None)

    def _show_progress(self, done: int, total: int, title: str) -> None:
        if self._import_cancel.is_set():
            return
        title = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", title)
        self.query_one("#import-status", Static).update(
            f"Matching {done} / {total} · {title}" if total else title
        )
        self.query_one("#import-progress", ProgressBar).update(total=total or None, progress=done)

    @work(group="playlist-import", exit_on_error=False)
    async def run_import(self, link: str, name: str | None) -> None:
        def progress(done: int, total: int, title: str) -> None:
            try:
                self.app.call_from_thread(self._show_progress, done, total, title)
            except RuntimeError:
                self._import_cancel.set()
        try:
            result = await asyncio.to_thread(self.import_playlist, link, name, progress=progress, cancel=self._import_cancel)
            self.dismiss(result)
        except Exception as exc:
            if self._import_cancel.is_set():
                self.dismiss(None)
                return
            message = str(exc) if isinstance(exc, MyneError) else "Import failed. Check your connection and try again."
            self.query_one("#import-status", Static).update(message)
            self._import_running = False
            for widget in self.query(Input):
                widget.disabled = False
            self.query_one("#confirm-import", Button).disabled = False
            self.query_one("#import-progress", ProgressBar).update(total=100, progress=0)
