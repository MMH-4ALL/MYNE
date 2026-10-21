"""Validated environment configuration and private application paths."""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from myne.errors import MyneError


@dataclass(frozen=True, slots=True)
class Config:
    data_dir: Path
    runtime_dir: Path
    mpv_binary: str = "mpv"
    provider: str = "youtube"
    auth_file: str | None = None
    cookies_file: str | None = None
    enabled_plugins: tuple[str, ...] = ()
    volume: int = 70
    library_path: str | None = None
    audio_output: str | None = None

    @property
    def database(self) -> Path:
        return self.data_dir / "myne.sqlite3"

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "player.sock"

    @property
    def mpv_socket_path(self) -> Path:
        return self.runtime_dir / "mpv.sock"

    @classmethod
    def from_env(cls) -> "Config":
        data = (
            Path(os.environ.get("MYNE_DATA_DIR") or user_data_path("myne", appauthor=False))
            .expanduser()
            .absolute()
        )
        uid = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        suffix = hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:10]
        runtime = (
            Path(os.environ.get("MYNE_RUNTIME_DIR") or Path(tempfile.gettempdir()) / f"myne-{uid}-{suffix}")
            .expanduser()
            .absolute()
        )
        try:
            volume = int(os.environ.get("MYNE_VOLUME", "70"))
        except ValueError as exc:
            raise MyneError("MYNE_VOLUME must be an integer from 0 to 100.") from exc
        if not 0 <= volume <= 100:
            raise MyneError("MYNE_VOLUME must be from 0 to 100.")
        config = cls(
            data,
            runtime,
            os.environ.get("MYNE_MPV", "mpv"),
            os.environ.get("MYNE_PROVIDER", "youtube"),
            os.environ.get("MYNE_AUTH_FILE") or None,
            os.environ.get("MYNE_COOKIES_FILE") or None,
            tuple(p.strip() for p in os.environ.get("MYNE_PLUGINS", "").split(",") if p.strip()),
            volume,
            os.environ.get("MYNE_LIBRARY") or None,
            os.environ.get("MYNE_AUDIO_OUTPUT") or None,
        )
        config.prepare()
        return config

    def prepare(self) -> None:
        for path in (self.data_dir, self.runtime_dir):
            if path.is_symlink():
                raise MyneError(f"Application directory must not be a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                raise MyneError(f"Application directory is owned by another user: {path}")
            path.chmod(0o700)
        if len(str(self.mpv_socket_path).encode()) > 103:
            raise MyneError("MYNE_RUNTIME_DIR is too long for Unix sockets; choose a shorter path.")

    def to_env(self) -> dict[str, str]:
        return {
            "MYNE_DATA_DIR": str(self.data_dir),
            "MYNE_RUNTIME_DIR": str(self.runtime_dir),
            "MYNE_MPV": self.mpv_binary,
            "MYNE_PROVIDER": self.provider,
            "MYNE_AUTH_FILE": self.auth_file or "",
            "MYNE_COOKIES_FILE": self.cookies_file or "",
            "MYNE_PLUGINS": ",".join(self.enabled_plugins),
            "MYNE_VOLUME": str(self.volume),
            "MYNE_LIBRARY": self.library_path or "",
            "MYNE_AUDIO_OUTPUT": self.audio_output or "",
        }
