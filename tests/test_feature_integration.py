"""Live feature boundaries: player IPC, CLI output and service injection."""

import math
import os
import shutil
import struct
import sys
import time
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from myne.cli import app
from myne.config import Config
from myne.lyrics import LyricsDocument, LyricsLine
from myne.models import Track
from myne.playback.client import PlayerClient
from myne.providers.local import LocalMusicProvider
from myne.service import Application


def test_service_lyrics_can_be_requested_without_playback(tmp_path: Path) -> None:
    document = LyricsDocument("Test", "Author", "An original test line", (), "Fixture")
    lookup = Mock()
    lookup.get.return_value = document
    service = Application(
        Config(tmp_path, tmp_path / "runtime"),
        store=Mock(),
        registry=Mock(),
        player=Mock(),
        lyrics_service=lookup,
    )
    track = Track("fixture", "Test", ("Author",))
    assert service.lyrics_document(track) == document
    lookup.get.assert_called_once_with(track, refresh=False)


def test_cli_lyrics_lrc_and_json_preserve_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    document = LyricsDocument(
        "Test", "Author", "An original test line", (LyricsLine(2.5, "An original test line"),), "Fixture"
    )
    service = Mock()
    service.lyrics_document.return_value = document
    monkeypatch.setattr("myne.cli.application", lambda _ctx: service)
    result = CliRunner().invoke(app, ["lyrics", "--lrc"])
    assert result.exit_code == 0, result.output
    assert "[00:02.50]An original test line" in result.output
    assert "Fixture" in result.output
    result = CliRunner().invoke(app, ["--json", "lyrics"])
    assert json.loads(result.output)["source"] == "Fixture"


def test_standalone_visualizer_closes_analysis_after_bounded_view(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Mock()
    service.visualizer.return_value = {"track_id": "fixture", "error": None}
    service.audio_levels.return_value = {"available": True, "rms": [0.5], "peak": [0.75], "error": None}
    monkeypatch.setattr("myne.cli.application", lambda _ctx: service)
    result = CliRunner().invoke(app, ["visualize", "--seconds", "0.1"])
    assert result.exit_code == 0, result.output
    assert "LIVE AUDIO" in result.output
    assert service.visualizer.call_args_list[-1].args == (False,)
    assert service.audio_levels.called


@pytest.mark.skipif(os.name == "nt" or not shutil.which("mpv"), reason="Requires real mpv and Unix IPC")
def test_real_daemon_visualizer_tracks_pause_seek_and_stop(tmp_path: Path) -> None:
    binary = shutil.which("mpv")
    wrapper = tmp_path / "mpv-null"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os,sys\nos.execv({binary!r}, [{binary!r}, '--ao=null', *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    audio = tmp_path / "Author - Test.wav"
    rate = 16000
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(
            b"".join(
                struct.pack("<hh", int(12000 * math.sin(i / 6)), int(1200 * math.sin(i / 6)))
                for i in range(rate * 6)
            )
        )
    config = Config(tmp_path / "data", tmp_path / "run", mpv_binary=str(wrapper), provider="local")
    config.prepare()
    player = PlayerClient(config, timeout=5)
    try:
        player.ensure_running()
        player.request("play", tracks=[LocalMusicProvider().get_song(str(audio)).to_dict()])
        player.request("visualizer", enabled=True)
        deadline = time.monotonic() + 3
        levels = {}
        while time.monotonic() < deadline:
            levels = player.request("levels")
            if levels["available"]:
                break
            time.sleep(0.05)
        assert levels["available"], levels
        assert levels["rms"][0] - levels["rms"][1] == pytest.approx(1 / 3, abs=0.02)
        assert levels["track_id"] == str(audio)
        assert levels["playing"] is True
        player.request("pause", value=True)
        assert player.request("levels")["rms"] == []
        player.request("seek", seconds=1)
        player.request("pause", value=False)
        player.request("stop")
        assert player.request("levels")["track_id"] is None
        assert player.request("levels")["available"] is False
    finally:
        player.shutdown()
