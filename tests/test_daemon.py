"""Player orchestration uses real SQLite and injected audio/provider dependencies."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from myne.config import Config
from myne.errors import PlaybackError, ProviderError, StreamError
from myne.models import Stream, Track
from myne.playback.client import PlayerClient
from myne.playback.daemon import PlayerService
from myne.storage import Store


class FakeEngine:
    def __init__(self) -> None:
        self.paused = False
        self.playing = False
        self.position = 0.0
        self.volume = 70
        self.muted = False
        self.events: list[dict[str, Any]] = []
        self.seeks: list[float] = []
        self.streams: list[Stream] = []
        self.visualizer_enabled = False

    def start(self) -> None:
        pass

    def play(self, stream: Stream) -> None:
        self.streams.append(stream)
        self.playing = True
        self.paused = False

    def pause(self, value: bool) -> None:
        self.paused = value

    def seek(self, seconds: float) -> None:
        self.seeks.append(seconds)
        self.position += seconds

    def stop(self) -> None:
        self.playing = False

    def set_volume(self, value: int) -> None:
        self.volume = value
        if value:
            self.muted = False

    def set_mute(self, value: bool) -> None:
        self.muted = value

    def snapshot(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "duration": 200,
            "paused": self.paused,
            "volume": self.volume,
            "muted": self.muted,
            "playing": self.playing and not self.paused,
        }

    def poll_events(self) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    def close(self) -> None:
        self.playing = False

    def enable_visualizer(self, enabled: bool) -> None:
        self.visualizer_enabled = enabled

    def audio_levels(self) -> dict[str, Any]:
        if not self.visualizer_enabled or not self.playing or self.paused:
            return {"available": False, "rms": [], "peak": [], "error": None}
        return {"available": True, "rms": [0.5, 0.25], "peak": [0.8, 0.5], "error": None}


class FakeProvider:
    def __init__(self) -> None:
        self.fail = False
        self.related: list[Track] = []
        self.resolved: list[str] = []

    def resolve_stream(self, track: Track) -> Stream:
        self.resolved.append(track.id)
        if self.fail:
            raise ProviderError("sensitive https://example.test/?token=secret")
        return Stream(f"https://example.test/{track.id}")

    def recommendations(self, _track: Track, limit: int = 25) -> list[Track]:
        return self.related[:limit]


@pytest.fixture
def player(tmp_path: Path) -> tuple:
    config = Config(tmp_path / "data", tmp_path / "runtime")
    store = Store(config.database)
    engine = FakeEngine()
    provider = FakeProvider()
    clock = [0.0]
    registry = SimpleNamespace(provider=lambda _name: provider)
    service = PlayerService(config, store, registry, engine, lambda: clock[0])
    return service, store, engine, provider, clock


def test_volume_changes_restore_sound_and_mute_is_controllable(player: tuple) -> None:
    service, store, engine, _, _ = player
    service.dispatch("play", tracks=[Track("a", "Track").to_dict()])
    assert service.dispatch("mute")["muted"] is True
    assert service.dispatch("volume", delta=5)["muted"] is False
    assert engine.volume == 75
    assert store.get_setting("volume") == 75
    assert service.dispatch("volume", delta=100)["volume"] == 100
    assert service.dispatch("volume", delta=-100)["volume"] == 0
    assert service.dispatch("volume", value=70)["volume"] == 70
    assert service.dispatch("mute", value=False)["muted"] is False


@pytest.mark.parametrize("starting", [True, False])
def test_audio_measurement_uses_transport_state_after_sampling(player: tuple, monkeypatch, starting):
    service, _, engine, _, _ = player
    service.dispatch("play", tracks=[Track("a", "Track").to_dict()])
    engine.playing = not starting
    def levels():
        engine.playing = starting
        return {"available": True, "rms": [0.5], "peak": [0.7], "error": None}
    monkeypatch.setattr(engine, "audio_levels", levels)
    packet = service.dispatch("levels")
    assert packet["playing"] is starting
    assert packet["available"] is starting
    assert packet["rms"] == ([0.5] if starting else [])


@pytest.mark.parametrize("payload", [{"delta": True}, {"delta": float("nan")}, {"value": 20, "delta": 5}])
def test_volume_adjustment_rejects_invalid_requests(player: tuple, payload: dict) -> None:
    service, *_ = player
    with pytest.raises(PlaybackError):
        service.dispatch("volume", **payload)


def test_end_file_advances_durable_queue(player: tuple) -> None:
    service, store, engine, _, clock = player
    first, second = Track("a", "First"), Track("b", "Second")
    service.dispatch("play", tracks=[first.to_dict(), second.to_dict()])
    service.tick()
    clock[0] = 40
    engine.events.append({"event": "end-file", "reason": "eof"})
    service.tick()
    assert service.now()["track"]["id"] == "b"
    assert store.current() == second
    assert store.queue() == []
    assert store.stats()["listening_seconds"] == 40
    assert store.stats()["plays"] == 1


@pytest.mark.parametrize("mode,expected,pending", [("one", "a", ["b"]), ("all", "b", ["a"])])
def test_repeat_applies_only_to_natural_completion(player: tuple, mode: str, expected: str, pending: list) -> None:
    service, store, engine, _, clock = player
    first, second = Track("a", "First"), Track("b", "Second")
    service.dispatch("play", tracks=[first.to_dict(), second.to_dict()])
    service.dispatch("repeat", value=mode)
    assert store.get_setting("repeat") == mode
    clock[0] = 40
    engine.events.append({"event": "end-file", "reason": "eof"})
    service.tick()
    assert service.now()["track"]["id"] == expected
    assert [track.id for track in store.queue()] == pending
    assert store.stats()["listening_seconds"] == 40
    if mode == "one":
        assert service.dispatch("next")["track"]["id"] == "b"


def test_repeat_cycles_and_radio_disables_it(player: tuple) -> None:
    service, store, *_ = player
    assert service.dispatch("repeat")["repeat"] == "all"
    assert service.dispatch("repeat")["repeat"] == "one"
    assert service.dispatch("repeat")["repeat"] == "off"
    with pytest.raises(PlaybackError, match="Repeat mode"):
        service.dispatch("repeat", value="bad")
    service.dispatch("repeat", value="all")
    service.dispatch("radio", seed=Track("a", "Seed").to_dict())
    assert service.now()["repeat"] == "off"
    assert store.get_setting("repeat") == "off"


def test_repeat_all_keeps_a_manually_skipped_track_at_the_end(player: tuple) -> None:
    service, store, *_ = player
    first, second = Track("a", "First"), Track("b", "Second")
    service.dispatch("play", tracks=[first.to_dict(), second.to_dict()])
    service.dispatch("repeat", value="all")
    assert service.dispatch("next")["track"]["id"] == "b"
    assert store.queue() == [first]


def test_listening_time_excludes_pause_and_seek(player: tuple) -> None:
    service, store, _, _, clock = player
    service.dispatch("play", tracks=[Track("a", "Track", duration=200).to_dict()])
    service.tick()  # mpv has confirmed active audio.
    clock[0] = 10
    service.dispatch("pause", value=True)
    clock[0] = 110
    service.dispatch("seek", seconds=150)
    service.dispatch("pause", value=False)
    clock[0] = 120
    service.dispatch("stop")
    assert store.stats()["listening_seconds"] == 20


def test_stop_retains_pending_queue_and_disables_radio(player: tuple) -> None:
    service, store, _, provider, _ = player
    seed, related = Track("a", "Seed"), Track("b", "Related")
    provider.related = [seed, related, related]
    service.dispatch("radio", seed=seed.to_dict())
    assert store.queue() == [related]
    service.dispatch("stop")
    assert service.now()["track"] is None
    assert service.now()["radio"] is False
    assert store.queue() == [related]


def test_radio_deduplicates_and_plain_play_disables_it(player: tuple) -> None:
    service, store, _, provider, _ = player
    seed, related = Track("a", "Seed"), Track("b", "Related")
    provider.related = [seed, related, related]
    service.dispatch("radio", seed=seed.to_dict())
    assert store.queue() == [related]
    service.dispatch("play", tracks=[Track("c", "Chosen").to_dict()])
    assert not service.now()["radio"]
    assert store.queue() == [related]


def test_play_queued_skips_earlier_entries_without_copying_the_tail(player: tuple) -> None:
    service, store, engine, _, _ = player
    current = Track("current", "Current")
    first, selected, last = (Track(str(i), f"Queued {i}") for i in range(3))
    service.dispatch("play", tracks=[current.to_dict()])
    store.enqueue([first, selected, last])

    result = service.dispatch("play_queued", index=1, expected=selected.to_dict())

    assert result["track"]["id"] == selected.id
    assert store.current() == selected
    assert store.queue() == [last]
    engine.events.append({"event": "end-file", "reason": "eof"})
    service.tick()
    assert store.current() == last
    assert store.queue() == []


def test_stale_queue_selection_keeps_the_current_song_and_queue(player: tuple) -> None:
    service, store, _, _, _ = player
    current, selected, replacement = (Track(str(i), f"Song {i}") for i in range(3))
    service.dispatch("play", tracks=[current.to_dict()])
    store.enqueue([replacement])

    with pytest.raises(PlaybackError, match="queue changed"):
        service.dispatch("play_queued", index=0, expected=selected.to_dict())

    assert store.current() == current
    assert store.queue() == [replacement]


@pytest.mark.parametrize("index", [None, True, -1, 1.5, "1"])
def test_invalid_queue_selection_cannot_trim_the_queue(player: tuple, index: Any) -> None:
    service, store, _, _, _ = player
    track = Track("queued", "Queued")
    store.enqueue([track])
    with pytest.raises(PlaybackError, match="Queue index"):
        service.dispatch("play_queued", index=index, expected=track.to_dict())
    assert store.queue() == [track]
    assert store.current() is None


@pytest.mark.parametrize("action", ["stop", "next", "play"])
def test_commands_recover_when_mpv_disconnects_before_the_next_tick(
    player: tuple, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    service, store, engine, _, clock = player
    first, second = Track("first", "First"), Track("second", "Second")
    service.dispatch("play", tracks=[first.to_dict(), second.to_dict()])
    clock[0] = 12
    original_stop = engine.stop

    def disconnected() -> None:
        monkeypatch.setattr(engine, "stop", original_stop)
        raise PlaybackError("The mpv socket disconnected.")

    monkeypatch.setattr(engine, "stop", disconnected)
    payload = {"tracks": [Track("replacement", "Replacement").to_dict()]} if action == "play" else {}
    result = service.dispatch(action, **payload)

    assert store.stats()["listening_seconds"] == 12
    if action == "stop":
        assert result["track"] is None
        assert store.queue() == [second]
    else:
        assert result["track"]["id"] == ("second" if action == "next" else "replacement")


@pytest.mark.skipif(os.name == "nt", reason="Unix playback client")
def test_socket_creation_errors_are_reported_as_playback_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("OS socket access denied")

    monkeypatch.setattr(socket, "socket", denied)
    client = PlayerClient(Config(tmp_path / "data", tmp_path / "runtime"))
    with pytest.raises(PlaybackError, match="communicate"):
        client.request("now")
    with pytest.raises(PlaybackError, match="socket"):
        client.ensure_running()


def test_empty_radio_recommendations_exhaust_after_seed(player: tuple) -> None:
    service, _, engine, _, _ = player
    service.dispatch("radio", seed=Track("a", "Seed").to_dict())
    engine.events.append({"event": "end-file", "reason": "eof"})
    service.tick()
    assert service.now()["track"] is None
    assert service.now()["radio"] is False
    assert "no new recommendations" in service.now()["error"]


def test_player_disconnect_requeues_interrupted_track(player: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, engine, _, _ = player
    track = Track("a", "Interrupted")
    service.dispatch("play", tracks=[track.to_dict()])

    def disconnected() -> list:
        raise PlaybackError("Disconnected")

    monkeypatch.setattr(engine, "poll_events", disconnected)
    service.tick()
    assert store.queue() == [track]
    assert service.now()["track"] is None


def test_stream_resolution_failures_stop_and_keep_retryable_queue(player: tuple) -> None:
    service, store, _, provider, _ = player
    provider.fail = True
    tracks = [Track(str(index), f"Track {index}") for index in range(10)]
    with pytest.raises(PlaybackError, match="three failed"):
        service.dispatch("play", tracks=[track.to_dict() for track in tracks])
    assert len(provider.resolved) == 3
    assert store.queue() == tracks
    assert "secret" not in service.now()["error"]
    service.tick()
    assert len(provider.resolved) == 3


def test_public_stream_failure_hint_survives_background_player(player, monkeypatch):
    service, store, _, provider, _ = player
    def fail(_track):
        raise StreamError("authentication")
    monkeypatch.setattr(provider, "resolve_stream", fail)
    track = Track("blocked", "Blocked")
    with pytest.raises(PlaybackError, match="MYNE_COOKIES_FILE"):
        service.dispatch("play", tracks=[track.to_dict()])
    assert store.queue() == [track]
    assert "MYNE_COOKIES_FILE" in service.now()["error"]


def test_audio_engine_failure_stops_before_resolving_streams(player, monkeypatch):
    service, store, engine, provider, _ = player
    def fail():
        raise PlaybackError("upstream detail https://secret.example/?token=private")
    monkeypatch.setattr(engine, "start", fail)
    tracks = [Track("first", "First"), Track("second", "Second")]
    with pytest.raises(PlaybackError, match="myne doctor"):
        service.dispatch("play", tracks=[t.to_dict() for t in tracks])
    assert provider.resolved == []
    assert store.queue() == tracks
    assert "secret" not in service.now()["error"]


def test_audio_output_failure_preserves_current_track_and_stops_auto_advance(player):
    service, store, engine, provider, _ = player
    tracks = [Track("first", "First"), Track("second", "Second")]
    service.dispatch("play", tracks=[t.to_dict() for t in tracks])
    engine.events.append({"event":"end-file", "reason":"error", "error":"audio output initialization failed"})
    service.tick()
    assert store.queue() == tracks
    assert service.now()["track"] is None
    assert "MYNE_AUDIO_OUTPUT" in service.now()["error"]
    service.tick()
    assert provider.resolved == ["first"]


def test_skipped_failed_track_remains_in_queue(player: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, _, provider, _ = player
    failed, good = Track("a", "Failed"), Track("b", "Good")

    def resolve(track: Track) -> Stream:
        if track == failed:
            raise ProviderError("Unavailable")
        return Stream("https://audio.example/good")

    monkeypatch.setattr(provider, "resolve_stream", resolve)
    service.dispatch("play", tracks=[failed.to_dict(), good.to_dict()])
    assert service.now()["track"]["id"] == "b"
    assert store.queue() == [failed]


def test_async_failures_do_not_spin_forever(player: tuple) -> None:
    service, store, engine, provider, _ = player
    tracks = [Track(str(index), f"Track {index}") for index in range(10)]
    service.dispatch("play", tracks=[track.to_dict() for track in tracks])
    for _ in range(3):
        engine.events.append({"event": "end-file", "reason": "error"})
        service.tick()
    assert len(provider.resolved) == 3
    assert service.now()["track"] is None
    assert store.queue()[0] == tracks[2]
    service.tick()
    assert len(provider.resolved) == 3


def test_graceful_shutdown_preserves_current_for_resume(player: tuple) -> None:
    service, store, _, _, clock = player
    track = Track("a", "Interrupted")
    service.dispatch("play", tracks=[track.to_dict()])
    service.tick()
    clock[0] = 12
    service.dispatch("shutdown")
    service.close()
    assert store.current() is None
    assert store.queue() == [track]
    assert store.stats()["listening_seconds"] == 12


def test_recover_current_after_crash(tmp_path: Path) -> None:
    config = Config(tmp_path / "data", tmp_path / "runtime")
    store = Store(config.database)
    track = Track("a", "Interrupted")
    store.enqueue([track])
    store.claim_next()
    PlayerService(config, store, SimpleNamespace(provider=lambda _name: FakeProvider()), FakeEngine())
    assert store.current() is None
    assert store.queue() == [track]


@pytest.mark.parametrize(
    "action,payload",
    [
        ("volume", {"value": float("nan")}),
        ("volume", {"value": 101}),
        ("seek", {"seconds": float("inf")}),
        ("pause", {"value": "yes"}),
    ],
)
def test_controls_reject_invalid_values(player: tuple, action: str, payload: dict) -> None:
    service, *_ = player
    service.dispatch("play", tracks=[Track("a", "Track").to_dict()])
    with pytest.raises(PlaybackError):
        service.dispatch(action, **payload)


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
def test_client_framing_and_error_propagation(tmp_path: Path) -> None:
    config = Config(tmp_path / "data", tmp_path / "r")
    config.runtime_dir.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(config.socket_path))
        listener.listen()

        def respond() -> None:
            connection, _ = listener.accept()
            with connection:
                request = json.loads(connection.recv(4096))
                assert request == {"action": "seek", "seconds": -15}
                connection.sendall(b'{"ok":false,"error":"Nothing is playing."}\n')

        thread = threading.Thread(target=respond)
        thread.start()
        with pytest.raises(PlaybackError, match="Nothing is playing"):
            PlayerClient(config, timeout=1).request("seek", seconds=-15)
        thread.join(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="Unix daemon")
def test_background_service_survives_independent_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use a short runtime path because macOS limits Unix sockets to 104 bytes.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="myne-test-") as runtime:
        config = Config(tmp_path / "data", Path(runtime))
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
        first = PlayerClient(config, timeout=5)
        first.ensure_running()
        try:
            assert first.request("ping")["version"] == 2
            track = Track("a", "Durable")
            first.request("enqueue", tracks=[track.to_dict()])
            second = PlayerClient(config, timeout=5)
            second.ensure_running()
            assert second.request("now")["track"] is None
            assert Store(config.database).queue() == [track]
            assert config.socket_path.stat().st_mode & 0o777 == 0o600
            second.shutdown()
            deadline = time.monotonic() + 5
            while config.socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
            assert not config.socket_path.exists()
        finally:
            if config.socket_path.exists():
                first.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="Unix daemon and subprocess executable")
@pytest.mark.parametrize("select_from_queue", [False, True])
def test_background_audio_controls_with_fake_mpv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, select_from_queue: bool
) -> None:
    """Use the actual daemon, engine, IPC and local provider across process boundaries."""
    import tempfile

    executable = tmp_path / "fake-mpv"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import socket
import sys

path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--input-ipc-server="))
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen()
connection, _ = server.accept()
properties = {"mpv-version": "test", "pause": False, "core-idle": True,
              "time-pos": 0, "duration": 300, "volume": 70, "playlist/0/id": 0}
buffer = b""
while True:
    chunk = connection.recv(65536)
    if not chunk:
        break
    buffer += chunk
    while b"\\n" in buffer:
        line, buffer = buffer.split(b"\\n", 1)
        request = json.loads(line)
        command = request["command"]
        data = None
        if command[0] == "get_property":
            data = properties.get(command[1])
        elif command[0] == "set_property":
            properties[command[1]] = command[2]
        elif command[0] == "loadfile":
            properties["core-idle"] = False
            properties["playlist/0/id"] += 1
            connection.sendall(b'{"event":"file-loaded"}\\n')
        elif command[0] == "seek":
            properties["time-pos"] += command[1]
        elif command[0] == "stop":
            properties["core-idle"] = True
        response = {"request_id": request["request_id"], "error": "success", "data": data}
        connection.sendall(json.dumps(response).encode() + b"\\n")
connection.close()
server.close()
"""
    )
    executable.chmod(0o700)
    music_file = tmp_path / "Artist - Song.wav"
    music_file.write_bytes(b"RIFF")
    track = Track(str(music_file), "Song", ("Artist",), provider="local")
    with tempfile.TemporaryDirectory(prefix="myne-audio-") as runtime:
        config = Config(tmp_path / "data", Path(runtime), mpv_binary=str(executable))
        for key, value in config.to_env().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
        first = PlayerClient(config, timeout=5)
        first.ensure_running()
        try:
            assert first.request("play", tracks=[track.to_dict()])["track"]["title"] == "Song"
            second = PlayerClient(config, timeout=5)
            tail: list[Track] = []
            if select_from_queue:
                queued = []
                for title in ("Skipped", "Selected", "Last"):
                    file = tmp_path / f"Artist - {title}.wav"
                    file.write_bytes(b"RIFF")
                    queued.append(Track(str(file), title, ("Artist",), provider="local"))
                first.request("enqueue", tracks=[item.to_dict() for item in queued])
                track = queued[1]
                tail = queued[2:]
                selected = second.request("play_queued", index=1, expected=track.to_dict())
                assert selected["track"]["id"] == track.id
                assert Store(config.database).queue() == tail
            assert second.request("pause", value=True)["paused"] is True
            assert second.request("seek", seconds=35)["position"] == 35
            assert second.request("volume", value=21)["volume"] == 21
            from typer.testing import CliRunner

            from myne.cli import app

            assert CliRunner().invoke(app, ["mute"]).exit_code == 0
            assert second.request("now")["muted"] is True
            assert CliRunner().invoke(app, ["unmute"]).exit_code == 0
            assert second.request("now")["muted"] is False
            assert second.request("resume")["paused"] is False
            assert second.request("now")["track"]["provider"] == "local"
        finally:
            first.shutdown()
            deadline = time.monotonic() + 5
            while config.socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
        assert Store(config.database).queue() == [track, *tail]
        assert not config.mpv_socket_path.exists()
