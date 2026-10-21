from pathlib import Path
from unittest.mock import Mock

import pytest

from myne.config import Config
from myne.errors import MyneError, PlaybackError
from myne.preferences import Preferences
from myne.service import Application
from myne.storage import Store


def test_preferences_migrate_previous_theme_and_volume_and_persist_together(tmp_path: Path):
    store = Store(tmp_path / "data" / "db")
    store.set_setting("ui_theme", "myne-ember")
    store.set_setting("volume", 28)
    player = Mock()
    service = Application(Config(tmp_path / "data", tmp_path / "run"), store=store, player=player)
    assert service.preferences() == Preferences(theme="myne-ember", volume=28)
    saved = Preferences(theme="myne-midnight", sidebar="hide", lyrics="show", visualizer=False,
                        reduced_motion=True, auto_check_updates=True, volume=42)
    assert service.save_preferences(saved.to_dict()) == saved
    reopened = Application(service.config, store=Store(store.database), player=player)
    assert reopened.preferences() == saved
    assert store.get_setting("ui_theme") == "myne-midnight"
    assert store.get_setting("volume") == 42
    player.ensure_running.assert_not_called()
    player.request.assert_not_called()


@pytest.mark.parametrize("values", [
    {"volume": True}, {"volume": -1}, {"volume": 101}, {"volume": float("nan")},
    {"volume": "70"}, {"sidebar": "maybe"}, {"lyrics": "sometimes"}, {"theme": "untrusted"},
    {"auto_check_updates": "true"}, {"reduced_motion": 1}, {"visualizer": None}, {"unknown": True},
])
def test_invalid_preference_saves_are_atomic(tmp_path, values):
    service = Application(Config(tmp_path / "data", tmp_path / "run"))
    before = service.preferences()
    with pytest.raises(MyneError):
        service.save_preferences({**before.to_dict(), **values})
    assert service.preferences() == before
    assert service.store.get_setting("preferences") is None


def test_invalid_old_values_fall_back_without_breaking_startup():
    assert Preferences.from_dict({"theme": [], "volume": False, "visualizer": "yes"}) == Preferences()
    assert Preferences.from_dict(None) == Preferences()


def test_settings_volume_reaches_running_player_and_idle_status_uses_saved_volume(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "run")
    config.prepare()
    player = Mock()
    player.request.side_effect = PlaybackError("not running")
    service = Application(config, player=player)
    config.socket_path.touch()
    service.save_preferences(Preferences(volume=31).to_dict())
    player.request.assert_called_once_with("volume", value=31)
    player.ensure_running.assert_not_called()
    assert service.now()["volume"] == 31


def test_setting_batch_cannot_partially_save_invalid_json(tmp_path):
    store = Store(tmp_path / "db")
    store.set_setting("first", "original")
    with pytest.raises(ValueError):
        store.set_settings({"first": "changed", "second": float("nan")})
    assert store.get_setting("first") == "original"
    assert store.get_setting("second") is None
