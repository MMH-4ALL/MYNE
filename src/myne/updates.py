"""Official-source update checks and opt-in installation inside a virtualenv.

Checks are read-only and cache successful results for a day. Installation always
rechecks upstream, builds a wheel from an immutable official commit, validates
its package/version metadata, then invokes pip in the current environment.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from importlib.metadata import PackageNotFoundError, distribution
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from myne import __version__
from myne.errors import MyneError
from myne.storage import Store

REPOSITORY = "MMH-4ALL/MYNE"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
API = f"https://api.github.com/repos/{REPOSITORY}"
CHECK_INTERVAL = 24 * 60 * 60
Progress = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Installation:
    supported: bool
    reason: str
    executable: str = sys.executable
    prefix: str = sys.prefix


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str | None = None
    commit: str | None = None
    available: bool = False
    can_install: bool = False
    message: str = "Updates have not been checked yet."
    install_note: str = ""
    url: str = REPOSITORY_URL
    checked_at: float | None = None


def installation() -> Installation:
    try:
        dist = distribution("myne-cli")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable") is True:
            return Installation(False, "Editable checkout: use git pull, then python -m pip install -e .")
        if sys.prefix == sys.base_prefix:
            return Installation(False, "Use your package manager, or install MYNE in a virtual environment to enable updates here.")
        if not Path(str(dist.locate_file(""))).resolve().is_relative_to(Path(sys.prefix).resolve()):
            return Installation(False, "MYNE is outside this environment. Update it using its original installation method.")
        if os.name == "nt":
            return Installation(False, "Use the installation method in WSL to update MYNE.")
        if find_spec("pip") is None:
            return Installation(False, "pip is not installed in this environment. Update MYNE using its original installer.")
        if not os.access(sys.prefix, os.W_OK):
            return Installation(False, "This Python environment is not writable. Use its original installation method.")
        return Installation(True, "Updates install into this Python environment. Restart MYNE after installation.")
    except (PackageNotFoundError, OSError, ValueError, AttributeError):
        return Installation(False, "Installation could not be identified. Reinstall MYNE using its original installation method.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise MyneError("The update service redirected unexpectedly. Try again later.")


def fetch_json(url: str) -> dict[str, Any]:
    # Every call site constructs this URL from constants and a checked commit ID.
    if not url.startswith(API + "/"):
        raise MyneError("The update source is not the official MYNE repository.")
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers={
            "Accept": "application/vnd.github+json", "User-Agent": f"MYNE/{__version__}",
        }), timeout=12) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise MyneError("The update service returned too much data. Try again later.")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise MyneError("GitHub's update limit was reached. Try checking again later.") from None
        raise MyneError("The official update source is unavailable. Try again later.") from None
    except (URLError, OSError, ValueError):
        raise MyneError("Could not check for updates. Check your connection and retry.") from None


def run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Bound pip's runtime, keep output out of the TUI, and reap its process group."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            raise
        output.seek(0)
        # Only the version probe's small output is interpreted. Never display pip
        # output, which may contain credentials from the user's package indexes.
        return subprocess.CompletedProcess(command, code, output.read(4096), "")


@contextmanager
def _environment_lock(prefix: str) -> Iterator[None]:
    import fcntl

    path = Path(prefix) / ".myne-update.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MyneError("Another MYNE update is already running in this environment.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class UpdateManager:
    def __init__(self, store: Store, *, current_version: str = __version__,
                 fetch: Callable[[str], dict[str, Any]] = fetch_json,
                 detect: Callable[[], Installation] = installation,
                 runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] = run_command,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.current = Version(current_version)
        self.fetch = fetch
        self.detect = detect
        self.runner = runner
        self.clock = clock
        self._check_lock = threading.Lock()
        self._install_lock = threading.Lock()
        self._installed = False

    def _info(self, latest: str | None = None, commit: str | None = None,
              checked_at: float | None = None, requires_python: str = ">=3.12") -> UpdateInfo:
        target = Version(latest) if latest is not None else None
        available = target is not None and target > self.current and not (target.is_prerelease or target.is_devrelease)
        compatible = Version(".".join(map(str, sys.version_info[:3]))) in SpecifierSet(requires_python)
        location = self.detect()
        message = "Updates have not been checked yet."
        if target is not None:
            message = f"MYNE {target} is available." if available else (
                "This build is newer than the published main branch." if self.current > target
                else "You are up to date." if self.current == target else "No newer stable version is published."
            )
        if available and not compatible:
            message += f" It requires Python {requires_python}."
        if self._installed:
            message = "Update installed. Close MYNE, then run myne shutdown and myne ui to load the new version."
        return UpdateInfo(str(self.current), str(target) if target else None, commit,
                          bool(available), bool(available and compatible and location.supported and not self._installed),
                          message, location.reason, f"{REPOSITORY_URL}/commit/{commit}" if commit else REPOSITORY_URL,
                          checked_at)

    def status(self) -> UpdateInfo:
        cached = self.store.get_setting("update_cache", {})
        try:
            if isinstance(cached, dict) and re.fullmatch(r"[0-9a-f]{40}", cached.get("commit", "")):
                return self._info(cached["version"], cached["commit"], cached.get("checked_at"),
                                  cached.get("requires_python", ">=3.12"))
        except (KeyError, TypeError, ValueError, InvalidVersion, InvalidSpecifier):
            pass
        return self._info()

    def check(self, *, force: bool = False) -> UpdateInfo:
        with self._check_lock:
            cached = self.status()
            if not force and self._recent(cached.checked_at):
                return cached
            attempt = self.store.get_setting("update_attempt", {})
            if (not force and isinstance(attempt, dict) and attempt.get("ok") is False
                    and self._recent(attempt.get("checked_at"))):
                raise MyneError("Automatic checks will retry after a day. Use Check now to retry sooner.")
            self.store.set_setting("update_attempt", {"checked_at": self.clock(), "ok": False})
            try:
                reference = self.fetch(f"{API}/git/ref/heads/main").get("object")
                if not isinstance(reference, dict) or reference.get("type") != "commit":
                    raise ValueError
                commit = reference.get("sha")
                if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise ValueError
                content = self.fetch(f"{API}/contents/pyproject.toml?ref={commit}")
                if content.get("encoding") != "base64" or not isinstance(content.get("content"), str):
                    raise ValueError
                raw = base64.b64decode("".join(content["content"].split()), validate=True)
                project = tomllib.loads(raw.decode("utf-8"))["project"]
                if project.get("name") != "myne-cli" or not isinstance(project.get("version"), str):
                    raise ValueError
                latest = str(Version(project["version"]))
                requires = project.get("requires-python", ">=3.12")
                info = self._info(latest, commit, self.clock(), requires)
            except MyneError:
                raise
            except (KeyError, ValueError, TypeError, binascii.Error, InvalidVersion, InvalidSpecifier):
                raise MyneError("The update metadata is invalid. Nothing was installed; try again later.") from None
            self.store.set_settings({
                "update_cache": {"version": latest, "commit": commit, "checked_at": info.checked_at,
                                 "requires_python": requires},
                "update_attempt": {"checked_at": info.checked_at, "ok": True},
            })
            return info

    def _recent(self, stamp: Any) -> bool:
        return (isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp)
                and 0 <= self.clock() - stamp < CHECK_INTERVAL)

    @staticmethod
    def _verify_wheel(path: Path, version: str) -> None:
        try:
            with zipfile.ZipFile(path) as wheel:
                names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
                if len(names) != 1 or wheel.getinfo(names[0]).file_size > 1_000_000:
                    raise ValueError
                metadata = BytesParser().parsebytes(wheel.read(names[0]))
                if metadata["Name"] != "myne-cli" or Version(metadata["Version"]) != Version(version):
                    raise ValueError
        except (OSError, ValueError, TypeError, zipfile.BadZipFile, InvalidVersion):
            raise MyneError("The prepared update did not match MYNE's checked version. Nothing was installed.") from None

    def install(self, progress: Progress | None = None) -> UpdateInfo:
        if not self._install_lock.acquire(blocking=False):
            raise MyneError("A MYNE update is already running.")
        phase = "preparing"
        try:
            location = self.detect()
            if not location.supported:
                raise MyneError(location.reason)
            with _environment_lock(location.prefix):
                if progress:
                    progress("Checking the official MYNE source…")
                info = self.check(force=True)
                if not info.available:
                    return info
                if not info.can_install or not info.commit or not info.latest_version:
                    raise MyneError(info.message)
                with tempfile.TemporaryDirectory(prefix="myne-update-") as temporary:
                    destination = Path(temporary)
                    pip = [location.executable, "-m", "pip", "--disable-pip-version-check"]
                    common = ["--no-input", "--timeout", "15", "--retries", "1"]
                    if progress:
                        progress(f"Preparing MYNE {info.latest_version}…")
                    result = self.runner([*pip, "wheel", *common, "--no-deps", "--wheel-dir", temporary,
                                          f"{REPOSITORY_URL}/archive/{info.commit}.zip"], 240)
                    if result.returncode:
                        raise MyneError("Could not prepare the update. The installed MYNE package was not replaced.")
                    wheels = list(destination.glob("*.whl"))
                    if len(wheels) != 1:
                        raise MyneError("The update did not produce one MYNE package. Nothing was installed.")
                    self._verify_wheel(wheels[0], info.latest_version)
                    phase = "installing"
                    if progress:
                        progress(f"Installing MYNE {info.latest_version}… keep this window open.")
                    result = self.runner([*pip, "install", *common, "--upgrade", str(wheels[0])], 240)
                    if result.returncode:
                        raise MyneError("Installation failed. Reinstall MYNE using its original installation method.")
                    result = self.runner([location.executable, "-c",
                        "from importlib.metadata import version; print(version('myne-cli'))"], 15)
                    try:
                        verified = not result.returncode and Version(result.stdout.strip()) == Version(info.latest_version)
                    except InvalidVersion:
                        verified = False
                    if not verified:
                        raise MyneError("The installed version could not be verified. Reinstall MYNE before restarting it.")
                self._installed = True
                return self.status()
        except (OSError, subprocess.SubprocessError):
            message = ("Could not prepare the update. The installed MYNE package was not replaced."
                       if phase == "preparing" else "Update installation was interrupted. Reinstall MYNE before restarting it.")
            raise MyneError(message) from None
        finally:
            self._install_lock.release()
