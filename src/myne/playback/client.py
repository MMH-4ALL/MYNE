"""Client for Myne's private background player service."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from myne.errors import PlaybackError
from myne.playback.mpv import private_directory

if TYPE_CHECKING:
    from myne.config import Config


class PlayerClient:
    def __init__(self, config: Config, timeout: float = 90.0) -> None:
        self.config = config
        self.timeout = timeout

    def request(self, action: str, **payload: Any) -> dict[str, Any]:
        if os.name == "nt":
            raise PlaybackError("Myne playback requires Linux or macOS; use WSL on Windows.")
        connection: socket.socket | None = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout)
            connection.connect(str(self.config.socket_path))
            connection.sendall(json.dumps({"action": action, **payload}, allow_nan=False).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    raise PlaybackError("The player service closed the connection. Run myne play again.")
                data += chunk
                if len(data) > 4_000_000:
                    raise PlaybackError("The player service response is too large.")
            result = json.loads(data.split(b"\n", 1)[0])
            if not isinstance(result, dict):
                raise PlaybackError("The player service returned an invalid response.")
            if not result.get("ok"):
                raise PlaybackError(str(result.get("error", "The player service rejected the command.")))
            payload = result.get("data", {})
            if not isinstance(payload, dict):
                raise PlaybackError("The player service returned invalid command data.")
            return payload
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise PlaybackError("The player is not running. Start playback with myne play <song>.") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise PlaybackError(
                "The player service timed out. Check your connection or run myne shutdown."
            ) from exc
        except (OSError, ValueError) as exc:
            raise PlaybackError("Could not communicate with the player service.") from exc
        finally:
            if connection is not None:
                connection.close()

    def ensure_running(self) -> None:
        if os.name == "nt":
            raise PlaybackError("Myne playback requires Linux or macOS; use WSL on Windows.")
        import fcntl

        private_directory(self.config.runtime_dir)
        # Serialize launchers, including independent shells starting simultaneously.
        lock_path = self.config.runtime_dir / "startup.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as startup_lock:
            fcntl.flock(startup_lock, fcntl.LOCK_EX)
            if self._reachable():
                return
            environment = os.environ.copy()
            environment.update(self.config.to_env())
            process = subprocess.Popen(
                [sys.executable, "-m", "myne.playback.daemon"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise PlaybackError(
                        "The player service could not start. Run myne doctor to inspect your setup."
                    )
                if self._reachable():
                    return
                time.sleep(0.05)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            raise PlaybackError("The player service did not start in time. Run myne doctor.")

    def _reachable(self) -> bool:
        """Probe transport without waiting behind provider work in a healthy daemon."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.5)
                connection.connect(str(self.config.socket_path))
                return True
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
            return False
        except OSError as exc:
            raise PlaybackError("Could not open the player socket. Check runtime directory permissions.") from exc

    def shutdown(self) -> None:
        self.request("shutdown")
