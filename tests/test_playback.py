"""Exercise real socket framing and subprocess cleanup without network or audio."""

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from myne.errors import PlaybackError
from myne.models import Stream
from myne.playback.mpv import MpvEngine, private_directory


@pytest.mark.skipif(os.name == "nt", reason="Unix IPC")
def test_ipc_matches_request_id_and_preserves_interleaved_events(tmp_path: Path) -> None:
    engine = MpvEngine(tmp_path / "mpv.sock", timeout=1)
    client, server = socket.socketpair()
    engine._socket = client

    def respond() -> None:
        request = json.loads(server.recv(4096))
        messages = [
            {"request_id": -1, "error": "success", "data": "unrelated"},
            {"event": "end-file", "reason": "eof"},
            {"request_id": request["request_id"], "error": "success", "data": 42},
        ]
        # Fragment the response across reads and place several lines in one read.
        response = b"".join(json.dumps(message).encode() + b"\n" for message in messages)
        server.sendall(response[:7])
        server.sendall(response[7:])

    thread = threading.Thread(target=respond)
    thread.start()
    try:
        assert engine.command("get_property", "volume") == 42
        assert engine.poll_events() == [{"event": "end-file", "reason": "eof"}]
    finally:
        thread.join(timeout=2)
        server.close()
        engine.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix IPC")
def test_startup_timeout_terminates_child(tmp_path: Path) -> None:
    executable = tmp_path / "sleeping-mpv"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    engine = MpvEngine(tmp_path / "mpv.sock", str(executable), timeout=0.1)
    with pytest.raises(PlaybackError, match="did not open"):
        engine.start()
    assert engine.process is None
    assert engine._socket is None
    assert not engine.socket_path.exists()


def test_missing_mpv_is_actionable(tmp_path: Path) -> None:
    engine = MpvEngine(tmp_path / "mpv.sock", str(tmp_path / "missing"))
    with pytest.raises(PlaybackError, match="Install mpv"):
        engine.start()


@pytest.mark.skipif(os.name == "nt", reason="Unix permissions")
def test_private_runtime_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(PlaybackError, match="Unsafe"):
        private_directory(linked)


def test_play_replaces_http_headers_between_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = MpvEngine(tmp_path / "mpv.sock")
    calls: list[tuple] = []
    monkeypatch.setattr(engine, "start", lambda: None)
    monkeypatch.setattr(engine, "command", lambda *args, **_kwargs: calls.append(args))
    engine.play(Stream("https://audio.example/a", {"Authorization": "secret"}))
    engine.play(Stream("file:///music/song.flac"))
    assert calls[0] == ("set_property", "http-header-fields", ["Authorization: secret"])
    assert calls[4] == ("set_property", "http-header-fields", [])
    assert calls[-2] == ("loadfile", "file:///music/song.flac", "replace")


def test_old_playlist_end_event_cannot_finish_new_track(tmp_path: Path) -> None:
    engine = MpvEngine(tmp_path / "mpv.sock")
    engine._entry_id = 9
    engine._events.extend(
        [
            {"event": "end-file", "reason": "eof", "playlist_entry_id": 8},
            {"event": "end-file", "reason": "eof", "playlist_entry_id": 9},
        ]
    )
    assert engine.poll_events() == [{"event": "end-file", "reason": "eof", "playlist_entry_id": 9}]
