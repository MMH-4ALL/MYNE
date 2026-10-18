"""Regressions for profile isolation, safe errors and query disambiguation."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from myne.cli import app
from myne.config import Config
from myne.errors import MyneError, ProviderError
from myne.models import Track
from myne.plugins import PluginRegistry
from myne.providers.base import MusicProvider
from myne.providers.local import LocalMusicProvider
from myne.service import Application


def test_environment_data_profiles_have_independent_player_namespaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MYNE_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MYNE_DATA_DIR", str(tmp_path / "first"))
    first = Config.from_env()
    monkeypatch.setenv("MYNE_DATA_DIR", str(tmp_path / "second"))
    second = Config.from_env()
    assert first.database != second.database
    assert first.socket_path != second.socket_path


def test_canonical_data_identity_shares_one_runtime_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "real"
    parent.mkdir()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(parent, target_is_directory=True)
    monkeypatch.delenv("MYNE_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MYNE_DATA_DIR", str(parent / "data"))
    first = Config.from_env()
    monkeypatch.setenv("MYNE_DATA_DIR", str(alias / "data"))
    second = Config.from_env()
    assert first.database.resolve() == second.database.resolve()
    assert first.socket_path == second.socket_path


@pytest.mark.parametrize("setting", ["MYNE_DATA_DIR", "MYNE_RUNTIME_DIR"])
def test_config_rejects_final_component_symlinks_before_resolving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    setting: str,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("MYNE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MYNE_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv(setting, str(alias))
    with pytest.raises(MyneError, match="symlink"):
        Config.from_env()
    assert target.stat().st_mode & 0o777 == 0o755


def test_json_error_is_machine_readable_and_has_failure_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("myne.cli.application", Mock(side_effect=MyneError("Missing queue")))
    result = CliRunner().invoke(app, ["--json", "queue", "restore"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "Missing queue"}
    assert result.stderr == ""


def test_human_error_goes_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("myne.cli.application", Mock(side_effect=MyneError("Missing queue")))
    result = CliRunner().invoke(app, ["queue", "restore"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Missing queue" in result.stderr


def application_with_provider(tmp_path: Path, provider: Mock) -> Application:
    registry = Mock(spec=PluginRegistry)
    registry.provider.return_value = provider
    return Application(
        Config(tmp_path / "data", tmp_path / "runtime"),
        store=Mock(),
        registry=registry,
        player=Mock(),
    )


def test_eleven_character_song_title_falls_back_to_search(tmp_path: Path) -> None:
    provider = Mock(spec=MusicProvider)
    provider.get_song.side_effect = ProviderError("This is a song title, not a valid video.")
    expected = Track("real-video", "Radioactive")
    provider.search.return_value = [expected]
    service = application_with_provider(tmp_path, provider)
    assert service.resolve("Radioactive") == expected
    provider.search.assert_called_once_with("Radioactive", 1)


def test_long_text_query_does_not_fail_local_file_stat(tmp_path: Path) -> None:
    provider = Mock(spec=MusicProvider)
    expected = Track("real-video", "A match")
    provider.search.return_value = [expected]
    service = application_with_provider(tmp_path, provider)
    query = "long song name " * 30
    assert service.resolve(query) == expected
    provider.search.assert_called_once_with(query.strip(), 1)


def test_local_library_play_query_resolves_a_search_match(tmp_path: Path) -> None:
    file = tmp_path / "Artist - Piano song.wav"
    file.touch()
    registry = PluginRegistry()
    registry.register_provider("local", lambda: LocalMusicProvider(tmp_path))
    service = Application(
        Config(tmp_path / "data", tmp_path / "runtime", provider="local"),
        store=Mock(),
        registry=registry,
        player=Mock(),
    )
    assert service.resolve("piano").id == str(file)
