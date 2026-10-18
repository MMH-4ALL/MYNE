"""A small mpv JSON IPC driver. Audio is streamed; URLs never touch the shell."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import stat
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from myne.errors import PlaybackError
from myne.models import Stream


def private_directory(path: Path) -> None:
    """Create an owner-only IPC directory and refuse links or foreign owners."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PlaybackError(f"Unsafe playback runtime directory: {path}")
    path.chmod(0o700)


class MpvEngine:
    """Own an idle mpv process and match replies while preserving async events."""

    def __init__(
        self,
        socket_path: Path,
        binary: str = "mpv",
        volume: int = 70,
        timeout: float = 5.0,
    ) -> None:
        self.socket_path = socket_path
        self.binary = binary
        self.volume = volume
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._lifeline: socket.socket | None = None
        self._buffer = b""
        self._events: deque[dict[str, Any]] = deque()
        self._request_id = 0
        self._entry_id: int | None = None
        self._lock = threading.RLock()

    def start(self) -> None:
        if os.name == "nt":
            raise PlaybackError("Myne playback requires Linux or macOS; use WSL on Windows.")
        with self._lock:
            if self._socket is not None and self.process is not None and self.process.poll() is None:
                return
            if self._socket is not None or self.process is not None:
                self.close()
            if shutil.which(self.binary) is None:
                raise PlaybackError("mpv is missing. Install mpv and run myne doctor.")
            private_directory(self.socket_path.parent)
            self.socket_path.unlink(missing_ok=True)
            self._lifeline, child_lifeline = socket.socketpair()
            try:
                try:
                    self.process = subprocess.Popen(
                        [
                            self.binary,
                            "--no-config",
                            "--idle=yes",
                            "--no-video",
                            "--no-terminal",
                            "--really-quiet",
                            "--ytdl=no",
                            "--cache-on-disk=no",
                            f"--volume={self.volume}",
                            f"--input-ipc-server={self.socket_path}",
                            f"--input-ipc-client=fd://{child_lifeline.fileno()}",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        pass_fds=(child_lifeline.fileno(),),
                    )
                finally:
                    child_lifeline.close()
                # mpv exits when this inherited client disconnects, even if the
                # service is killed abruptly. Disable unused events on the guard.
                self._lifeline.sendall(b'{"command":["disable_event","all"]}\n')
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise PlaybackError(
                            "mpv exited during startup. Check your mpv installation and audio output."
                        )
                    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        connection.connect(str(self.socket_path))
                    except (FileNotFoundError, ConnectionRefusedError):
                        connection.close()
                        time.sleep(0.025)
                    else:
                        self._socket = connection
                        self.socket_path.chmod(0o600)
                        self.command("get_property", "mpv-version")
                        return
                raise PlaybackError("mpv did not open its IPC socket in time.")
            except (OSError, PlaybackError) as exc:
                self.close()
                if isinstance(exc, PlaybackError):
                    raise
                raise PlaybackError(
                    "Could not start mpv. Check its executable and runtime directory."
                ) from exc

    def _message(self, deadline: float) -> dict[str, Any]:
        if self._socket is None:
            raise PlaybackError("mpv is not running.")
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaybackError("mpv IPC timed out.")
            self._socket.settimeout(remaining)
            try:
                chunk = self._socket.recv(65536)
            except (OSError, TimeoutError) as exc:
                raise PlaybackError("Lost the connection to mpv. Try myne play again.") from exc
            if not chunk:
                raise PlaybackError("mpv closed its IPC connection. Try myne play again.")
            self._buffer += chunk
            if len(self._buffer) > 2_000_000:
                raise PlaybackError("mpv returned an oversized IPC message.")
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise PlaybackError("mpv returned invalid JSON.") from exc
        if not isinstance(message, dict):
            raise PlaybackError("mpv returned an invalid IPC message.")
        return message

    def command(self, *arguments: Any, allow_unavailable: bool = False) -> Any:
        with self._lock:
            if self._socket is None:
                raise PlaybackError("mpv is not running.")
            self._request_id += 1
            request_id = self._request_id
            payload = json.dumps({"command": list(arguments), "request_id": request_id}).encode() + b"\n"
            try:
                self._socket.sendall(payload)
            except OSError as exc:
                raise PlaybackError("Could not send a command to mpv.") from exc
            deadline = time.monotonic() + self.timeout
            while True:
                message = self._message(deadline)
                if "event" in message:
                    self._events.append(message)
                if message.get("request_id") != request_id:
                    continue
                error = message.get("error", "success")
                if error != "success":
                    if allow_unavailable and error in {"property unavailable", "property not found"}:
                        return None
                    raise PlaybackError(f"mpv command failed: {error}")
                return message.get("data")

    def poll_events(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._socket is not None:
                while b"\n" in self._buffer or select.select([self._socket], [], [], 0)[0]:
                    message = self._message(time.monotonic() + self.timeout)
                    if "event" in message:
                        self._events.append(message)
            events = list(self._events)
            self._events.clear()
            return [
                event
                for event in events
                if not (
                    event.get("event") == "end-file"
                    and self._entry_id is not None
                    and event.get("playlist_entry_id") is not None
                    and event["playlist_entry_id"] != self._entry_id
                )
            ]

    def play(self, stream: Stream) -> None:
        self.start()
        # Clear headers between providers so credentials never leak to another origin.
        headers = [f"{name}: {value}" for name, value in stream.headers.items()]
        self.command("set_property", "http-header-fields", headers)
        self.command("set_property", "pause", False)
        self.command("loadfile", stream.url, "replace")
        self._entry_id = self.command("get_property", "playlist/0/id", allow_unavailable=True)

    def pause(self, value: bool) -> None:
        self.command("set_property", "pause", value)

    def seek(self, seconds: float) -> None:
        self.command("seek", seconds, "relative+exact")

    def set_volume(self, value: int) -> None:
        self.volume = value
        self.command("set_property", "volume", value)

    def stop(self) -> None:
        if self._socket is not None:
            self.command("stop")
            self.poll_events()
            self._entry_id = None

    def snapshot(self) -> dict[str, Any]:
        if self._socket is None:
            return {
                "position": 0.0,
                "duration": None,
                "paused": False,
                "volume": self.volume,
                "playing": False,
            }
        position = self.command("get_property", "time-pos", allow_unavailable=True)
        duration = self.command("get_property", "duration", allow_unavailable=True)
        paused = self.command("get_property", "pause", allow_unavailable=True)
        idle = self.command("get_property", "core-idle", allow_unavailable=True)
        volume = self.command("get_property", "volume", allow_unavailable=True)
        return {
            "position": float(position or 0),
            "duration": duration,
            "paused": bool(paused),
            "volume": self.volume if volume is None else float(volume),
            "playing": idle is False and paused is False,
        }

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            if self._lifeline is not None:
                self._lifeline.close()
                self._lifeline = None
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                self.process = None
            self._buffer = b""
            self._events.clear()
            self._entry_id = None
            self.socket_path.unlink(missing_ok=True)
