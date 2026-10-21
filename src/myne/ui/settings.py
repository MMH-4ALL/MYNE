"""Keyboard-accessible preferences and update controls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TabbedContent, TabPane

from myne import __version__
from myne.errors import MyneError
from myne.preferences import Preferences
from myne.updates import UpdateInfo

if TYPE_CHECKING:
    from myne.service import Application


class SettingsScreen(ModalScreen[Preferences | None]):
    BINDINGS = [("escape", "cancel", "Close")]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 85%; }
    SettingsScreen > #settings-dialog {
        width: 78; max-width: 96%; height: 28; max-height: 96%; min-height: 18;
        padding: 0 1; border: solid $primary; background: $surface;
    }
    SettingsScreen #settings-heading { height: 1; color: $primary; text-style: bold; }
    SettingsScreen TabbedContent, SettingsScreen ContentSwitcher { height: 1fr; }
    SettingsScreen .settings-page { padding: 0 1; }
    SettingsScreen .setting-row { height: 3; }
    SettingsScreen .setting-label { width: 25; padding-top: 1; }
    SettingsScreen Select { width: 1fr; }
    SettingsScreen Input { width: 12; border: solid $panel; }
    SettingsScreen Input:focus { border: solid $primary; }
    SettingsScreen .settings-help { height: auto; color: $text-muted; }
    SettingsScreen #update-summary, SettingsScreen #update-progress { height: auto; max-height: 4; }
    SettingsScreen #update-progress { color: $primary; }
    SettingsScreen #update-buttons { height: 3; }
    SettingsScreen #update-buttons Button { min-width: 10; margin-right: 1; }
    SettingsScreen #settings-message { height: 1; color: $text-muted; }
    SettingsScreen #settings-footer { height: 3; align-horizontal: right; }
    SettingsScreen Button { border: none; height: 3; margin-left: 1; }
    """

    def __init__(self, service: Application, preferences: Preferences) -> None:
        super().__init__()
        self.service = service
        self.preferences = preferences
        self._saving_preferences = False
        self._checking_updates = False
        self.installing_update = False
        self._update_info = UpdateInfo(__version__)
        self._update_revision = 0

    def compose(self) -> ComposeResult:
        preferences = self.preferences
        with Vertical(id="settings-dialog"):
            yield Static("MAKE MYNE YOURS", id="settings-heading")
            with TabbedContent(id="settings-tabs"):
                with TabPane("Appearance", id="appearance-tab"):
                    with VerticalScroll(classes="settings-page"):
                        with Horizontal(classes="setting-row"):
                            yield Label("Theme", classes="setting-label")
                            yield Select([(name, f"myne-{name.lower()}") for name in ("Studio", "Midnight", "Ember")],
                                         value=preferences.theme, allow_blank=False, id="setting-theme")
                        with Horizontal(classes="setting-row"):
                            yield Label("Sidebar", classes="setting-label")
                            yield Select([("Automatic", "auto"), ("Always show", "show"), ("Hide", "hide")],
                                         value=preferences.sidebar, allow_blank=False, id="setting-sidebar")
                        with Horizontal(classes="setting-row"):
                            yield Label("Lyrics", classes="setting-label")
                            yield Select([("Automatic", "auto"), ("Show", "show"), ("Hide", "hide")],
                                         value=preferences.lyrics, allow_blank=False, id="setting-lyrics")
                        with Horizontal(classes="setting-row"):
                            yield Label("Audio visualizer", classes="setting-label")
                            yield Switch(preferences.visualizer, id="setting-visualizer")
                        with Horizontal(classes="setting-row"):
                            yield Label("Reduce motion", classes="setting-label")
                            yield Switch(preferences.reduced_motion, id="setting-motion")
                        yield Static("Terminal or environment requests for reduced motion stay respected.", classes="settings-help")
                with TabPane("Playback", id="playback-tab"):
                    with VerticalScroll(classes="settings-page"):
                        with Horizontal(classes="setting-row"):
                            yield Label("Saved volume (0–100)", classes="setting-label")
                            yield Input(str(preferences.volume), type="integer", max_length=3, id="setting-volume")
                        yield Static("Saved for future playback and applied to a reachable player. "
                                     "Saving settings never starts the player.", classes="settings-help")
                        yield Static("Volume, mute, repeat, seeking, and Stop remain available in the player dock.", classes="settings-help")
                with TabPane("Updates", id="updates-tab"):
                    with VerticalScroll(classes="settings-page"):
                        with Horizontal(classes="setting-row"):
                            yield Label("Check on launch", classes="setting-label")
                            yield Switch(preferences.auto_check_updates, id="setting-auto-update")
                        yield Static("Checks on launch at most once a day. Installs use the button.", classes="settings-help")
                        yield Static(f"Running MYNE {__version__}\nUpdates have not been checked yet.",
                                     id="update-summary", markup=False)
                        with Horizontal(id="update-buttons"):
                            yield Button("Check now", id="check-update")
                            yield Button("Install update", id="install-update", variant="primary", disabled=True)
                        yield Static("", id="update-progress", markup=False)
                        yield Static("Official source: github.com/MMH-4ALL/MYNE · main", classes="settings-help", markup=False)
                        yield Static("Identifying this installation…", id="update-install-note", classes="settings-help", markup=False)
            yield Static("Changes apply when saved. Esc closes without saving.", id="settings-message", markup=False)
            with Horizontal(id="settings-footer"):
                yield Button("Close", id="settings-cancel")
                yield Button("Save settings", id="settings-save", variant="primary")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.styles.line_pad = 0
            button.styles.border = "none"
        self.load_update_status()

    def action_cancel(self) -> None:
        if self.installing_update or self._saving_preferences:
            self.query_one("#settings-message", Static).update("Please wait for the current operation to finish.")
            return
        self.dismiss(None)

    @on(Button.Pressed, "#settings-cancel")
    def close_settings(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#settings-save")
    def save_settings(self) -> None:
        if self._saving_preferences or self.installing_update:
            return
        try:
            raw_volume = self.query_one("#setting-volume", Input).value.strip()
            if not raw_volume.isascii() or not raw_volume.isdigit():
                raise MyneError("Volume must be an integer from 0 to 100.")
            preferences = Preferences.from_dict({
                "theme": self.query_one("#setting-theme", Select).value,
                "sidebar": self.query_one("#setting-sidebar", Select).value,
                "lyrics": self.query_one("#setting-lyrics", Select).value,
                "visualizer": self.query_one("#setting-visualizer", Switch).value,
                "reduced_motion": self.query_one("#setting-motion", Switch).value,
                "auto_check_updates": self.query_one("#setting-auto-update", Switch).value,
                "volume": int(raw_volume),
            }, strict=True)
        except MyneError as exc:
            self.query_one("#settings-message", Static).update(str(exc))
            return
        self._saving_preferences = True
        self._sync_buttons()
        self.persist_preferences(preferences)

    @work(group="settings-save", exit_on_error=False)
    async def persist_preferences(self, preferences: Preferences) -> None:
        try:
            saved = await asyncio.to_thread(self.service.save_preferences, preferences.to_dict())
            self.dismiss(saved)
        except Exception as exc:
            message = str(exc) if isinstance(exc, MyneError) else "Settings could not be saved. Try again."
            self.query_one("#settings-message", Static).update(message)
        finally:
            self._saving_preferences = False
            self._sync_buttons()

    def _sync_buttons(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#settings-save", Button).disabled = self.installing_update or self._saving_preferences
        self.query_one("#settings-cancel", Button).disabled = self.installing_update or self._saving_preferences
        self.query_one("#check-update", Button).disabled = self.installing_update or self._checking_updates
        self.query_one("#install-update", Button).disabled = (
            self.installing_update or self._checking_updates or not self._update_info.can_install
        )

    def _show_update_info(self, info: UpdateInfo) -> None:
        self._update_info = info
        latest = info.latest_version or "not checked"
        self.query_one("#update-summary", Static).update(f"Running: {info.current_version}    Latest: {latest}\n{info.message}")
        self.query_one("#update-install-note", Static).update(info.install_note)
        self._sync_buttons()

    @work(group="settings-update-status", exit_on_error=False)
    async def load_update_status(self) -> None:
        revision = self._update_revision
        try:
            info = await asyncio.to_thread(self.service.update_status)
            if revision == self._update_revision and not self._checking_updates and not self.installing_update:
                self._show_update_info(info)
        except Exception:
            self.query_one("#update-install-note", Static).update("Could not identify the installation. Try Check now.")

    @on(Button.Pressed, "#check-update")
    def check_updates(self) -> None:
        if self._checking_updates or self.installing_update:
            return
        self._checking_updates = True
        self._update_revision += 1
        self._sync_buttons()
        self.run_update_check()

    @work(group="settings-update-check", exit_on_error=False)
    async def run_update_check(self) -> None:
        self.query_one("#update-progress", Static).update("Checking the official MYNE source…")
        try:
            self._show_update_info(await asyncio.to_thread(self.service.check_update, True))
            self.query_one("#update-progress", Static).update("")
        except Exception as exc:
            self._update_info = UpdateInfo(__version__)
            message = str(exc) if isinstance(exc, MyneError) else "Could not check for updates. Try again."
            self.query_one("#update-progress", Static).update(message)
        finally:
            self._checking_updates = False
            self._sync_buttons()

    @on(Button.Pressed, "#install-update")
    def install_updates(self) -> None:
        if self._checking_updates or self.installing_update or not self._update_info.can_install:
            return
        self.installing_update = True
        self._sync_buttons()
        self.run_update_install()

    @work(group="settings-update-install", exit_on_error=False)
    async def run_update_install(self) -> None:
        def progress(message: str) -> None:
            try:
                self.app.call_from_thread(self._show_install_progress, message)
            except RuntimeError:
                pass
        try:
            info = await asyncio.to_thread(self.service.install_update, progress)
            self._show_update_info(info)
            self.query_one("#update-progress", Static).update(info.message)
        except Exception as exc:
            message = str(exc) if isinstance(exc, MyneError) else "Update failed. Reinstall MYNE using its original installation method."
            self.query_one("#update-progress", Static).update(message)
        finally:
            self.installing_update = False
            self._sync_buttons()

    def _show_install_progress(self, message: str) -> None:
        self.query_one("#update-progress", Static).update(message)
