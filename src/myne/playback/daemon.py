"""Private Unix-socket player daemon: durable queue, streaming and radio."""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import threading
import time
from collections import deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable

from myne.errors import MyneError, PlaybackError, StreamError
from myne.models import Track
from myne.playback.mpv import MpvEngine, private_directory

if TYPE_CHECKING:
    from myne.config import Config
    from myne.plugins import PluginRegistry
    from myne.storage import Store


class PlayerService:
    """Serialized player state, separated from transport for deterministic tests."""

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        registry: PluginRegistry | None = None,
        engine: MpvEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if store is None:
            from myne.storage import Store

            store = Store(config.database)
        if registry is None:
            from myne.container import create_registry

            registry = create_registry(config)
        self.config = config
        self.store = store
        self.registry = registry
        saved_volume = self.store.get_setting("volume", config.volume)
        self._volume = (
            saved_volume if isinstance(saved_volume, int) and 0 <= saved_volume <= 100 else config.volume
        )
        self._muted = False
        saved_repeat = self.store.get_setting("repeat", "off")
        self._repeat = saved_repeat if saved_repeat in ("off", "all", "one") else "off"
        self.engine = engine or MpvEngine(
            config.mpv_socket_path, config.mpv_binary, self._volume, audio_output=config.audio_output
        )
        self.clock = clock
        self.lock = threading.RLock()
        self.shutdown_requested = False
        self._active: Track | None = None
        self._auto = False
        self._paused = False
        self._counting = False
        self._listened = 0.0
        self._last_tick = clock()
        self._error: str | None = None
        self._failures = 0
        self._radio_seed: Track | None = None
        self._radio_last_seed: tuple[str, str] | None = None
        self._history: deque[tuple[str, str]] = deque(maxlen=250)
        # An interrupted song belongs at the front of the durable queue.
        self.store.recover_current()

    @staticmethod
    def _key(track: Track) -> tuple[str, str]:
        return track.provider, track.id

    def _account(self) -> None:
        now = self.clock()
        if self._active is not None and self._counting:
            self._listened += max(0.0, now - self._last_tick)
        self._last_tick = now

    def _finish(self, completed: bool, stop_engine: bool = True) -> None:
        self._account()
        if stop_engine:
            try:
                self.engine.stop()
            except (MyneError, OSError):
                # A disconnect can happen before tick notices it. Retire that
                # engine so stop/next/play can still finish the durable track.
                with suppress(MyneError, OSError):
                    self.engine.close()
        if self._active is not None:
            self.store.finish_current(self._listened, completed)
        self._active = None
        self._listened = 0.0
        self._counting = False
        self._paused = False

    def _refill_radio(self) -> None:
        seed = self._radio_seed
        if seed is None or len(self.store.queue()) >= 5:
            return
        if self._radio_last_seed == self._key(seed):
            return
        self._radio_last_seed = self._key(seed)
        excluded = set(self._history) | {self._key(track) for track in self.store.queue()}
        if self._active is not None:
            excluded.add(self._key(self._active))
        try:
            recommendations = self.registry.provider(seed.provider).recommendations(seed, limit=25)
            fresh = []
            for track in recommendations:
                key = self._key(track)
                if key not in excluded:
                    fresh.append(track)
                    excluded.add(key)
            self.store.enqueue(fresh)
            if not fresh and not self.store.queue():
                self._radio_seed = None
                self._error = "Radio has no new recommendations. Start another radio station."
        except Exception:
            self._radio_seed = None
            self._error = "Could not refill radio. Check your provider connection and start radio again."

    def _advance(self) -> None:
        """Bound failures per transition so a bad provider cannot spin endlessly."""
        failed: list[Track] = []
        for _ in range(3):
            self._refill_radio()
            track = self.store.claim_next()
            if track is None:
                self._auto = False
                self.store.enqueue(failed, front=True)
                if self._radio_seed is not None:
                    self._radio_seed = None
                    self._error = "Radio has no new recommendations. Start another radio station."
                return
            self._active = track
            self._listened = 0.0
            self._last_tick = self.clock()
            self._history.append(self._key(track))
            try:
                self.engine.start()
            except (MyneError, OSError):
                self._error = (
                    "The audio player could not start. Run myne doctor and check "
                    "MYNE_MPV or MYNE_AUDIO_OUTPUT."
                )
                self._finish(False, stop_engine=False)
                self._auto = False
                self.store.enqueue([*failed, track], front=True)
                return
            try:
                # Fetch radio metadata before starting audio so provider latency
                # cannot create an uncounted interval of actual listening.
                if self._radio_seed is not None:
                    self._radio_seed = track
                    self._refill_radio()
                stream = self.registry.provider(track.provider).resolve_stream(track)
                self.engine.play(stream)
                state = self.engine.snapshot()
            except Exception as exc:
                # Stream URLs and provider exception details may carry credentials.
                self._error = (
                    StreamError.MESSAGES.get(exc.reason, StreamError.MESSAGES["unavailable"])
                    if isinstance(exc, StreamError)
                    else f"Could not play {track.title}. Check the provider connection and mpv installation."
                )
                self._finish(False, stop_engine=False)
                failed.append(track)
                self._failures += 1
                if self._failures >= 3:
                    break
                continue
            self._paused = False
            self._last_tick = self.clock()
            self._counting = bool(state["playing"])
            self._error = None
            # Retry failed metadata only after the existing queue, so transient
            # failures neither discard music nor immediately spin on one song.
            self.store.enqueue(failed)
            return
        self._auto = False
        self.store.enqueue(failed, front=True)
        with suppress(MyneError, OSError):
            self.engine.stop()
        self._error = (
            "Playback stopped after three failed tracks. The queue is saved. "
            + (self._error or "Check your connection and run myne play.")
        )

    def tick(self) -> None:
        with self.lock:
            self._account()
            if self._active is None:
                return
            try:
                events = self.engine.poll_events()
                ended = next(
                    (
                        event
                        for event in events
                        if event.get("event") == "end-file" and event.get("reason") in {"eof", "error"}
                    ),
                    None,
                )
                if ended is not None:
                    ended_track = self._active
                    output_failed = (
                        ended.get("reason") == "error"
                        and ended.get("error") == "audio output initialization failed"
                    )
                    if ended.get("reason") == "error":
                        self._failures += 1
                        self._error = (
                            "mpv could not open an audio output. Check your sound device and "
                            "MYNE_AUDIO_OUTPUT, then restart with myne shutdown."
                            if output_failed
                            else "The audio stream failed. Check your connection or choose another track."
                        )
                    else:
                        self._failures = 0
                    self._finish(ended.get("reason") == "eof", stop_engine=False)
                    if output_failed:
                        # A device failure affects every song. Keep this track
                        # first instead of consuming/retrying the whole queue.
                        self._auto = False
                        if ended_track is not None:
                            self.store.enqueue([ended_track], front=True)
                        return
                    if ended.get("reason") == "eof" and ended_track is not None and self._radio_seed is None:
                        if self._repeat == "one":
                            self.store.enqueue([ended_track], front=True)
                        elif self._repeat == "all":
                            self.store.enqueue([ended_track])
                    if self._failures >= 3:
                        self._auto = False
                        if ended_track is not None:
                            self.store.enqueue([ended_track], front=True)
                        self._error = (
                            "Playback stopped after three stream failures. The remaining queue is saved."
                        )
                    elif self._auto:
                        if ended.get("reason") == "error" and ended_track is not None:
                            self.store.enqueue([ended_track])
                        self._advance()
                    return
                state = self.engine.snapshot()
                self._counting = bool(state["playing"])
                self._paused = bool(state["paused"])
                if self._listened >= 5:
                    self._failures = 0
            except (MyneError, OSError):
                interrupted = self._active
                self._finish(False, stop_engine=False)
                if interrupted is not None:
                    self.store.enqueue([interrupted], front=True)
                self._auto = False
                self._error = (
                    "The audio player disconnected. Your pending queue is saved; run myne play again."
                )

    def now(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "position": 0.0,
            "duration": None,
            "paused": False,
            "playing": False,
            "volume": self._volume,
            "muted": self._muted,
            "audio_output": None,
            "audio_device": None,
        }
        if self._active is not None:
            state.update(self.engine.snapshot())
        state.update(
            track=self._active.to_dict() if self._active else None,
            radio=self._radio_seed is not None,
            repeat=self._repeat,
            error=self._error,
        )
        return state

    @staticmethod
    def _number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PlaybackError(f"{name} must be a finite number.")
        return float(value)

    @staticmethod
    def _tracks(value: Any) -> list[Track]:
        if not isinstance(value, list) or len(value) > 1000:
            raise PlaybackError("Provide a list of at most 1,000 tracks.")
        try:
            tracks = [Track.from_dict(item) for item in value if isinstance(item, dict)]
        except (KeyError, TypeError, ValueError) as exc:
            raise PlaybackError("A track is missing valid id, title or duration metadata.") from exc
        if len(tracks) != len(value) or any(not track.id or not track.title for track in tracks):
            raise PlaybackError("Each track needs an id and title.")
        if any(
            track.duration is not None and (not math.isfinite(track.duration) or track.duration < 0)
            for track in tracks
        ):
            raise PlaybackError("Track durations must be finite, non-negative numbers.")
        return tracks

    def dispatch(self, action: str, **payload: Any) -> dict[str, Any]:
        with self.lock:
            self._account()
            if action == "ping":
                return {"version": 2}
            if action == "now":
                return self.now()
            if action in {"visualizer", "levels"}:
                if action == "visualizer":
                    enabled = payload.get("enabled", True)
                    if not isinstance(enabled, bool):
                        raise PlaybackError("Visualizer enabled must be true or false.")
                    self.engine.enable_visualizer(enabled)
                try:
                    levels = (
                        self.engine.audio_levels()
                        if self._active
                        else {"available": False, "rms": [], "peak": [], "error": None}
                    )
                except (MyneError, OSError):
                    levels = {
                        "available": False,
                        "rms": [],
                        "peak": [],
                        "error": "Audio measurements are temporarily unavailable.",
                    }
                # mpv can leave buffering or stop while measurements are read.
                # Pair them with the later transport state, and never emit
                # active measurements alongside a paused/idle status packet.
                state = self.now()
                if not state.get("playing") or state.get("paused"):
                    levels = {**levels, "available": False, "rms": [], "peak": []}
                return {
                    **levels,
                    "track_id": self._active.id if self._active else None,
                    "provider": self._active.provider if self._active else None,
                    "paused": state.get("paused", False),
                    "playing": state.get("playing", False),
                    "position": state.get("position", 0.0),
                }
            if action == "play_queued":
                index = payload.get("index")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise PlaybackError("Queue index must be a non-negative integer.")
                expected = self._tracks([payload.get("expected")])[0]
                try:
                    self.store.skip_to(index, expected)
                except MyneError as exc:
                    raise PlaybackError(str(exc)) from exc
                self._radio_seed = None
                self._finish(False)
                self._error = None
                self._failures = 0
                self._auto = True
                self._advance()
                self._check_started()
                return self.now()
            if action in {"play", "enqueue"}:
                tracks = self._tracks(payload.get("tracks"))
                if not tracks:
                    raise PlaybackError("No tracks were supplied.")
                if action == "play":
                    self._radio_seed = None
                    self._finish(False)
                    self.store.enqueue(tracks, front=True)
                    self._error = None
                    self._failures = 0
                    self._auto = True
                    self._advance()
                    self._check_started()
                else:
                    self.store.enqueue(tracks)
                return self.now()
            if action == "resume":
                self._error = None
                self._failures = 0
                self._auto = True
                if self._active is None:
                    self._advance()
                    self._check_started()
                else:
                    self.engine.pause(False)
                    self._paused = False
                    self._counting = bool(self.engine.snapshot()["playing"])
                return self.now()
            if action == "pause":
                if self._active is None:
                    raise PlaybackError("Nothing is playing.")
                value = payload.get("value", not self._paused)
                if not isinstance(value, bool):
                    raise PlaybackError("Pause value must be true or false.")
                self.engine.pause(value)
                self._paused = value
                self._counting = not value and bool(self.engine.snapshot()["playing"])
                return self.now()
            if action == "next":
                skipped = self._active
                self._finish(False)
                if skipped is not None and self._repeat == "all" and self._radio_seed is None:
                    self.store.enqueue([skipped])
                self._error = None
                self._failures = 0
                self._auto = True
                self._advance()
                self._check_started()
                return self.now()
            if action == "stop":
                self._radio_seed = None
                self._finish(False)
                self._auto = False
                return self.now()
            if action == "volume":
                if "value" in payload and "delta" in payload:
                    raise PlaybackError("Provide a volume value or a relative change, not both.")
                if "delta" in payload:
                    value = max(0.0, min(100.0, self._volume + self._number(payload["delta"], "Volume change")))
                else:
                    value = self._number(payload.get("value"), "Volume")
                if not 0 <= value <= 100:
                    raise PlaybackError("Volume must be between 0 and 100.")
                self.engine.start()
                self.engine.set_volume(round(value))
                self._volume = round(value)
                if self._volume > 0:
                    self._muted = False
                self.store.set_setting("volume", round(value))
                result = self.now()
                result["volume"] = round(value)
                return result
            if action == "mute":
                value = payload.get("value", not self._muted)
                if not isinstance(value, bool):
                    raise PlaybackError("Mute value must be true or false.")
                self.engine.start()
                self.engine.set_mute(value)
                self._muted = value
                return self.now()
            if action == "repeat":
                modes = ("off", "all", "one")
                value = payload.get("value", modes[(modes.index(self._repeat) + 1) % len(modes)])
                if value not in modes:
                    raise PlaybackError("Repeat mode must be off, all, or one.")
                self._repeat = value
                if value != "off":
                    self._radio_seed = None
                self.store.set_setting("repeat", value)
                return self.now()
            if action == "seek":
                if self._active is None:
                    raise PlaybackError("Nothing is playing.")
                self.engine.seek(self._number(payload.get("seconds"), "Seek seconds"))
                return self.now()
            if action == "radio":
                seeds = self._tracks([payload.get("seed")])
                seed = seeds[0]
                self._finish(False)
                self._radio_seed = seed
                self._repeat = "off"
                self.store.set_setting("repeat", "off")
                self._radio_last_seed = None
                self._error = None
                self._failures = 0
                self._auto = True
                self.store.enqueue([seed], front=True)
                self._advance()
                self._check_started()
                return self.now()
            if action == "radio_off":
                self._radio_seed = None
                return self.now()
            if action == "shutdown":
                self.shutdown_requested = True
                return {"shutdown": True}
            raise PlaybackError(f"Unknown player action: {action}")

    def _check_started(self) -> None:
        if self._active is None and self._error is not None:
            raise PlaybackError(self._error)

    def close(self) -> None:
        # Preserve the interrupted current song on graceful shutdown, with listening
        # time recorded once. Crash recovery handles an unclosed current row instead.
        with self.lock:
            track = self._active
            if track is not None:
                self._finish(False, stop_engine=False)
                self.store.enqueue([track], front=True)
        self.engine.close()


def serve(config: Config) -> None:
    if os.name == "nt":
        raise PlaybackError("Myne playback requires Linux or macOS; use WSL on Windows.")
    import fcntl

    private_directory(config.runtime_dir)
    descriptor = os.open(config.runtime_dir / "daemon.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as daemon_lock:
        try:
            fcntl.flock(daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # Another process already owns this runtime.
        service = PlayerService(config)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        config.socket_path.unlink(missing_ok=True)
        try:
            server.bind(str(config.socket_path))
            config.socket_path.chmod(0o600)
            server.listen(16)
            server.settimeout(0.25)

            def stop(_signum: int, _frame: Any) -> None:
                service.shutdown_requested = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while not service.shutdown_requested:
                service.tick()
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    data = b""
                    try:
                        while b"\n" not in data:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            data += chunk
                            if len(data) > 2_000_000:
                                raise PlaybackError("Player request is too large.")
                        request = json.loads(data.split(b"\n", 1)[0])
                        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
                            raise PlaybackError("A player action is required.")
                        action = request.pop("action")
                        response = {"ok": True, "data": service.dispatch(action, **request)}
                    except (MyneError, ValueError, TypeError, OSError) as exc:
                        message = str(exc) if isinstance(exc, MyneError) else "Invalid player request."
                        response = {"ok": False, "error": message}
                    except Exception:
                        # Provider plugins must not crash the shared playback service or leak secrets.
                        response = {
                            "ok": False,
                            "error": "The player could not complete this request. Run myne doctor.",
                        }
                    with suppress(OSError):
                        connection.sendall(json.dumps(response, allow_nan=False).encode() + b"\n")
        finally:
            service.close()
            server.close()
            config.socket_path.unlink(missing_ok=True)


def main() -> None:
    from myne.config import Config

    try:
        serve(Config.from_env())
    except (MyneError, OSError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
