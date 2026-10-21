import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from myne.cli import app
from myne.config import Config
from myne.errors import MyneError
from myne.models import Track
from myne.service import Application


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MYNE_DATA_DIR", str(tmp_path / "data"))
    # Short path is required for Unix sockets.
    monkeypatch.setenv("MYNE_RUNTIME_DIR", f"/tmp/myne-test-{tmp_path.name[-15:]}")
    monkeypatch.setenv("MYNE_PROVIDER", "local")
    monkeypatch.setenv("MYNE_PLUGINS", "")
    return CliRunner()


def test_cli_persistent_queue_json_and_snapshot(isolated, tmp_path):
    audio = tmp_path / "My [bold] Song.mp3"
    audio.write_bytes(b"synthetic metadata test")
    for args in (["queue", "add", str(audio)], ["queue", "save"], ["queue", "clear"], ["queue", "restore"]):
        result = isolated.invoke(app, args)
        assert result.exit_code == 0, result.output
    result = isolated.invoke(app, ["--json", "queue", "show"])
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["title"] == "My [bold] Song"
    assert data[0]["provider"] == "local"
    result = isolated.invoke(app, ["queue", "show"])
    assert "[bold]" in result.output


def test_cli_playlist_import_list_show_and_queue(isolated, monkeypatch):
    from myne.models import Playlist
    from myne.playlists import ImportResult

    service = Application(Config.from_env())
    track = Track("abcdefghijk", "Imported song", ("Artist",))
    importer = Mock()
    importer.read.return_value = ImportResult(Playlist("Night", (track,), "Spotify",
        "https://open.spotify.com/playlist/" + "A" * 22, ("Public view may be partial.",)), 1)
    service._playlist_importer = importer
    monkeypatch.setattr("myne.cli.application", lambda _ctx: service)
    result = isolated.invoke(app, ["--json", "playlist", "import", "https://open.spotify.com/playlist/" + "A" * 22])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["playlist"]["name"] == "Night"
    assert service.queue() == []
    assert json.loads(isolated.invoke(app, ["--json", "playlist", "list"]).output) == [{"name": "Night", "tracks": 1}]
    assert json.loads(isolated.invoke(app, ["--json", "playlist", "show", "Night"]).output)[0]["id"] == track.id
    assert isolated.invoke(app, ["playlist", "queue", "Night"]).exit_code == 0
    assert service.queue() == [track]


def test_cancelled_import_does_not_save_a_collection_or_change_queue(isolated):
    from threading import Event

    from myne.models import Playlist
    from myne.playlists import ImportResult

    cancel = Event()
    importer = Mock()
    def read(*args, **kwargs):
        cancel.set()
        return ImportResult(Playlist("Cancelled", (Track("x", "Song"),)), 1)
    importer.read.side_effect = read
    service = Application(Config.from_env(), playlist_importer=importer)
    service.enqueue([Track("queued", "Queue stays")])
    with pytest.raises(MyneError, match="cancelled"):
        service.import_playlist("link", cancel=cancel)
    assert service.playlists() == []
    assert service.queue() == [Track("queued", "Queue stays")]


def test_cli_validation_and_no_player_status(isolated):
    for args in (["volume", "101"], ["queue", "remove", "0"], ["search", "a", "--limit", "0"]):
        assert isolated.invoke(app, args).exit_code == 2
    result = isolated.invoke(app, ["--json", "now"])
    assert result.exit_code == 0
    assert json.loads(result.output)["track"] is None
    result = isolated.invoke(app, ["queue", "restore", "--name", "absent"])
    assert result.exit_code == 1
    assert "No saved queue" in result.output
    assert "Traceback" not in result.output


def test_now_distinguishes_loading_from_playing_and_reports_silent_output(isolated, monkeypatch):
    service = Mock()
    service.now.return_value = {
        "track": Track("a", "Track").to_dict(), "playing": False, "paused": False,
        "muted": True, "volume": 0, "audio_output": "null", "position": 0,
    }
    monkeypatch.setattr("myne.cli.application", lambda _ctx: service)
    result = isolated.invoke(app, ["now"])
    assert result.exit_code == 0, result.output
    assert "Loading" in result.output
    assert "Playing" not in result.output
    assert "muted" in result.output
    assert "null (silent)" in result.output


def test_cli_ai_needs_explicit_configuration(isolated, monkeypatch):
    monkeypatch.delenv("MYNE_MIX_ENDPOINT", raising=False)
    result = isolated.invoke(app, ["mix", "focus", "--ai"])
    assert result.exit_code == 1
    assert "MYNE_MIX_ENDPOINT" in result.output


def test_cli_settings_persist_and_validate_before_saving(isolated):
    result = isolated.invoke(app, ["settings", "set", "theme", "myne-ember"])
    assert result.exit_code == 0, result.output
    assert isolated.invoke(app, ["settings", "set", "auto_check_updates", "on"]).exit_code == 0
    result = isolated.invoke(app, ["--json", "settings", "show"])
    assert json.loads(result.output)["theme"] == "myne-ember"
    assert json.loads(result.output)["auto_check_updates"] is True
    for args in [["volume", "101"], ["visualizer", "anything"], ["unknown", "true"]]:
        result = isolated.invoke(app, ["--json", "settings", "set", *args])
        assert result.exit_code == 1
        assert "error" in json.loads(result.output)


def test_cli_update_defaults_to_check_and_install_is_explicit(isolated, monkeypatch):
    from myne.updates import UpdateInfo

    service = Mock()
    service.check_update.return_value = UpdateInfo("0.4.0", message="Up to date")
    service.install_update.return_value = UpdateInfo("0.4.0", message="Installed; restart")
    monkeypatch.setattr("myne.cli.application", lambda _ctx: service)
    result = isolated.invoke(app, ["--json", "update"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["message"] == "Up to date"
    service.install_update.assert_not_called()
    assert isolated.invoke(app, ["update", "--install"]).exit_code == 0
    service.install_update.assert_called_once()


def test_service_injects_provider_and_player(isolated, tmp_path):
    from myne.plugins import PluginRegistry
    from myne.providers.local import LocalMusicProvider

    class Player:
        started = False

        def ensure_running(self):
            self.started = True

        def request(self, action, **payload):
            return {"action": action, **payload}

    config = Config.from_env()
    registry = PluginRegistry()
    registry.register_provider("local", lambda: LocalMusicProvider(tmp_path))
    player = Player()
    service = Application(config, registry=registry, player=player)
    track = Track("one", "One", provider="local")
    assert service.play([track])["tracks"] == [track.to_dict()]
    assert player.started
    assert service.enqueue([track]) == {"added": 1}
    assert service.queue() == [track]
    with pytest.raises(MyneError):
        service.play([])


def test_global_data_dir_isolated_from_config(isolated, tmp_path):
    import os

    before = os.environ["MYNE_DATA_DIR"]
    alt = tmp_path / "alternate"
    assert isolated.invoke(app, ["--data-dir", str(alt), "queue", "show"]).exit_code == 0
    assert (alt / "myne.sqlite3").exists()
    assert os.environ["MYNE_DATA_DIR"] == before


def test_config_roundtrip_and_volume_validation(isolated, monkeypatch):
    config = Config.from_env()
    for key, value in replace(config, volume=42, audio_output="pulse").to_env().items():
        monkeypatch.setenv(key, value)
    assert Config.from_env().volume == 42
    assert Config.from_env().audio_output == "pulse"
    monkeypatch.setenv("MYNE_VOLUME", "loud")
    with pytest.raises(MyneError):
        Config.from_env()


def test_corrupt_database_has_sanitized_json_error(isolated, monkeypatch, tmp_path):
    data = tmp_path / "corrupt"
    data.mkdir()
    (data / "myne.sqlite3").write_bytes(b"not a sqlite database")
    monkeypatch.setenv("MYNE_DATA_DIR", str(data))
    result = isolated.invoke(app, ["--json", "queue", "show"])
    assert result.exit_code == 1
    assert "error" in json.loads(result.stdout)
    assert "Traceback" not in result.output
