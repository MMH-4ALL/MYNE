"""Focused modal flows around the music workspace."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


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
