"""Update source, version, cache, preparation, and real isolated pip installation."""

import base64
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from myne.errors import MyneError
from myne.storage import Store
from myne.updates import API, Installation, UpdateManager, fetch_json, installation, run_command

COMMIT = "a" * 40


def metadata(version="0.4.1", python=">=3.12", name="myne-cli"):
    data = f'[project]\nname="{name}"\nversion="{version}"\nrequires-python="{python}"\n'
    return {"encoding": "base64", "content": base64.b64encode(data.encode()).decode()}


def manager(tmp_path, *, version="0.4.1", current="0.4.0", python=">=3.12", supported=True, **kwargs):
    fetch = Mock(side_effect=lambda url: {"object": {"type": "commit", "sha": COMMIT}}
                 if "/git/ref/" in url else metadata(version, python))
    location = Installation(supported, "Test installation" if supported else "Use your package manager", prefix=str(tmp_path))
    updater = UpdateManager(Store(tmp_path / "db"), current_version=current, fetch=fetch,
                            detect=lambda: location, **kwargs)
    return updater, fetch


@pytest.mark.parametrize(("current", "latest", "available"), [
    ("0.4.0", "0.4.1", True), ("0.9.0", "0.10.0", True), ("0.10.0", "0.9.0", False),
    ("0.4.0", "0.4.0", False), ("0.4.0", "0.5.0rc1", False), ("0.4.0", "0.5.0.dev1", False),
])
def test_version_checks_do_not_downgrade_or_offer_prereleases(tmp_path, current, latest, available):
    updater, fetch = manager(tmp_path, current=current, version=latest)
    info = updater.check()
    assert info.available is available
    assert info.can_install is available
    assert info.commit == COMMIT
    assert fetch.call_args.args[0] == f"{API}/contents/pyproject.toml?ref={COMMIT}"


def test_status_is_offline_and_successful_checks_cache_across_instances(tmp_path):
    now = [1000.0]
    updater, fetch = manager(tmp_path, clock=lambda: now[0])
    assert updater.status().latest_version is None
    fetch.assert_not_called()
    first = updater.check()
    assert fetch.call_count == 2
    second = UpdateManager(updater.store, current_version="0.4.0", fetch=fetch,
                           detect=updater.detect, clock=lambda: now[0])
    assert second.check() == first
    assert fetch.call_count == 2
    now[0] += 86401
    second.check()
    assert fetch.call_count == 4
    second.check(force=True)
    assert fetch.call_count == 6


def test_failed_automatic_check_backs_off_but_manual_check_can_retry(tmp_path):
    now = [1000.0]
    updater, fetch = manager(tmp_path, clock=lambda: now[0])
    fetch.side_effect = MyneError("offline")
    with pytest.raises(MyneError, match="offline"):
        updater.check()
    with pytest.raises(MyneError, match="retry after a day"):
        updater.check()
    assert fetch.call_count == 1
    with pytest.raises(MyneError, match="offline"):
        updater.check(force=True)
    assert fetch.call_count == 2
    now[0] += 86401
    with pytest.raises(MyneError, match="offline"):
        updater.check()
    assert fetch.call_count == 3


def test_incompatible_python_has_an_update_but_cannot_install(tmp_path):
    updater, _ = manager(tmp_path, python=">=99")
    info = updater.check()
    assert info.available
    assert not info.can_install
    assert "requires Python" in info.message
    with pytest.raises(MyneError, match="requires Python"):
        updater.install()


@pytest.mark.parametrize("bad", [
    {"sha": "main"}, {"sha": "a" * 40 + "?bad"}, {"sha": None},
])
def test_invalid_commit_cannot_become_an_install_command(tmp_path, bad):
    updater, fetch = manager(tmp_path)
    fetch.side_effect = [bad]
    with pytest.raises(MyneError, match="metadata is invalid"):
        updater.check()
    assert updater.store.get_setting("update_cache") is None


@pytest.mark.parametrize("bad", [
    {"encoding": "base64", "content": "not-valid-!"}, {"encoding": "raw", "content": "secret"},
    metadata(name="other-package"), metadata(version="not-a-version"), metadata(python="not-a-specifier"),
])
def test_invalid_package_metadata_never_reaches_installer(tmp_path, bad):
    updater, fetch = manager(tmp_path)
    fetch.side_effect = [{"object": {"type": "commit", "sha": COMMIT}}, bad]
    with pytest.raises(MyneError, match="metadata is invalid"):
        updater.check()


@pytest.mark.parametrize("cached", [None, [], {"commit": None}, {"commit": COMMIT, "version": []},
    {"commit": COMMIT, "version": "bad"}, {"commit": COMMIT, "version": "0.4.1", "checked_at": "bad"}])
def test_corrupt_or_old_update_cache_does_not_crash_checking(tmp_path, cached):
    updater, _ = manager(tmp_path)
    updater.store.set_setting("update_cache", cached)
    assert updater.check().latest_version == "0.4.1"


def test_unsupported_install_does_not_fetch_or_run_commands(tmp_path):
    runner = Mock()
    updater, fetch = manager(tmp_path, supported=False, runner=runner)
    with pytest.raises(MyneError, match="package manager"):
        updater.install()
    fetch.assert_not_called()
    runner.assert_not_called()


def test_install_is_a_successful_noop_when_already_current(tmp_path):
    runner = Mock()
    updater, _ = manager(tmp_path, version="0.4.0", runner=runner)
    info = updater.install()
    assert not info.available
    assert info.message == "You are up to date."
    runner.assert_not_called()


def wheel(path: Path, version="0.4.1", name="myne-cli") -> Path:
    target = path / f"myne_cli-{version}-py3-none-any.whl"
    dist = f"myne_cli-{version}.dist-info"
    files = {
        "myne/__init__.py": f'__version__="{version}"\n',
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: myne-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{dist}/RECORD"] = "".join(f"{key},,\n" for key in [*files, f"{dist}/RECORD"])
    with zipfile.ZipFile(target, "w") as archive:
        for key, value in files.items():
            archive.writestr(key, value)
    return target


@pytest.mark.parametrize("bad", ["wrong-name", "wrong-version"])
def test_prepared_wheel_is_verified_before_any_install(tmp_path, bad):
    calls = []
    def run(command, timeout):
        calls.append(command)
        destination = Path(command[command.index("--wheel-dir") + 1])
        wheel(destination, name="other" if bad == "wrong-name" else "myne-cli",
              version="9.9.9" if bad == "wrong-version" else "0.4.1")
        return subprocess.CompletedProcess(command, 0, "", "")
    updater, _ = manager(tmp_path, runner=run)
    with pytest.raises(MyneError, match="did not match"):
        updater.install()
    assert len(calls) == 1
    assert "install" not in calls[0]
    assert calls[0][-1].endswith(f"/{COMMIT}.zip")


def test_install_always_rechecks_and_ignores_a_tampered_cached_commit(tmp_path):
    calls = []
    def run(command, timeout):
        calls.append(command)
        if "wheel" in command:
            wheel(Path(command[command.index("--wheel-dir") + 1]))
        return subprocess.CompletedProcess(command, 0, "0.4.1\n" if "-c" in command else "", "")
    updater, fetch = manager(tmp_path, runner=run)
    updater.store.set_setting("update_cache", {"version": "99.0", "commit": "b" * 40, "checked_at": 1000})
    result = updater.install()
    assert fetch.call_count == 2
    assert calls[0][-1].endswith(f"/{COMMIT}.zip")
    assert "installed" in result.message
    assert not result.can_install


def test_failed_preparation_and_timeout_never_run_install(tmp_path):
    for failure in [subprocess.CompletedProcess([], 1, "token=private", ""),
                    subprocess.TimeoutExpired("secret command", 1)]:
        runner = Mock(side_effect=failure if isinstance(failure, Exception) else None,
                      return_value=failure)
        updater, _ = manager(tmp_path, runner=runner)
        with pytest.raises(MyneError, match="prepare") as error:
            updater.install()
        assert "private" not in str(error.value)
        assert "secret" not in str(error.value)
        assert runner.call_count == 1


def test_concurrent_updates_are_rejected_without_a_second_installer(tmp_path):
    gate, started = threading.Event(), threading.Event()
    def run(command, timeout):
        if "wheel" in command:
            started.set()
            gate.wait(3)
            wheel(Path(command[command.index("--wheel-dir") + 1]))
        return subprocess.CompletedProcess(command, 0, "0.4.1\n" if "-c" in command else "", "")
    updater, _ = manager(tmp_path, runner=run)
    errors = []
    def first():
        try:
            updater.install()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert started.wait(1)
        with pytest.raises(MyneError, match="already running"):
            updater.install()
    finally:
        gate.set()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == []


def test_real_isolated_pip_update_replaces_package_and_verifies_new_version(tmp_path):
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True, timeout=60)
    python = str(environment / "bin" / "python")
    first = wheel(tmp_path, "0.4.0")
    subprocess.run([python, "-m", "pip", "install", "--no-index", str(first)],
                   check=True, capture_output=True, timeout=30)
    candidate = wheel(tmp_path, "0.4.1")
    def run(command, timeout):
        if "wheel" in command:
            destination = Path(command[command.index("--wheel-dir") + 1])
            shutil.copyfile(candidate, destination / candidate.name)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "install" in command:
            command.append("--no-index")
        return run_command(command, timeout)
    updater, _ = manager(tmp_path, runner=run)
    updater.detect = lambda: Installation(True, "Isolated test environment", python, str(environment))
    assert "installed" in updater.install().message
    result = subprocess.run([python, "-c", "import myne; print(myne.__version__)"],
                            check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == "0.4.1"


def test_editable_and_system_installations_are_detected_without_network(monkeypatch, tmp_path):
    dist = Mock()
    dist.read_text.return_value = '{"dir_info":{"editable":true}}'
    monkeypatch.setattr("myne.updates.distribution", lambda _: dist)
    assert not installation().supported
    assert "Editable" in installation().reason
    dist.read_text.return_value = "{}"
    monkeypatch.setattr("myne.updates.sys.prefix", sys.base_prefix)
    assert not installation().supported
    assert "package manager" in installation().reason


def test_unofficial_update_url_is_rejected_before_network():
    with pytest.raises(MyneError, match="official"):
        fetch_json("https://untrusted.example/package")


@pytest.mark.parametrize("failure", [HTTPError("https://secret.example", 429, "token=private", {}, None),
    HTTPError("https://secret.example", 404, "token=private", {}, None), URLError("token=private")])
def test_update_network_errors_do_not_expose_upstream_credentials(monkeypatch, failure):
    opener = Mock()
    opener.open.side_effect = failure
    monkeypatch.setattr("myne.updates.build_opener", lambda *_: opener)
    with pytest.raises(MyneError) as error:
        fetch_json(f"{API}/git/ref/heads/main")
    assert "token=" not in str(error.value)
    assert "secret.example" not in str(error.value)


def test_a_virtualenv_without_pip_gets_manual_update_instructions(monkeypatch, tmp_path):
    dist = Mock()
    dist.read_text.return_value = "{}"
    dist.locate_file.return_value = tmp_path
    monkeypatch.setattr("myne.updates.distribution", lambda _: dist)
    monkeypatch.setattr("myne.updates.sys.prefix", str(tmp_path))
    monkeypatch.setattr("myne.updates.find_spec", lambda _: None)
    assert not installation().supported
    assert "pip is not installed" in installation().reason


def test_process_timeout_is_bounded_and_does_not_echo_command_output():
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", "import time; time.sleep(10)"], 0)
