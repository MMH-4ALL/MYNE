"""The standalone visualizer renders independently of bounded IPC sampling."""

import json
from collections import deque
from concurrent.futures import Future
from typing import Any, Callable
from unittest.mock import Mock

import pytest
from rich.text import Text
from typer.testing import CliRunner

from myne import cli


class ImmediateExecutor:
    def __init__(self) -> None:
        self.submitted = 0
        self.shutdown = Mock()

    def submit(self, read: Callable[[], dict[str, Any]]) -> Future[dict[str, Any]]:
        self.submitted += 1
        future: Future[dict[str, Any]] = Future()
        try:
            future.set_result(read())
        except Exception as exc:
            future.set_exception(exc)
        return future


class Clock:
    def __init__(self) -> None:
        self.now = 10.0
        self.sleeps: list[float] = []

    def sleep(self, duration: float) -> None:
        assert duration > 0, "The render loop must yield instead of busy spinning."
        self.sleeps.append(duration)
        self.now += duration


class RecordingLive:
    def __init__(self) -> None:
        self.frames: list[Text] = []
        self.options: dict[str, Any] = {}

    def __call__(self, **options: Any) -> "RecordingLive":
        self.options = options
        return self

    def __enter__(self) -> "RecordingLive":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def update(self, frame: Text, *, refresh: bool) -> None:
        assert refresh
        self.frames.append(frame)


@pytest.fixture
def render_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Mock, Clock, RecordingLive, ImmediateExecutor]:
    service = Mock()
    service.visualizer.return_value = {"track_id": "test", "error": None}
    service.audio_levels.return_value = {
        "available": True,
        "rms": [0.4, 0.6],
        "peak": [0.6, 0.8],
        "error": None,
    }
    clock = Clock()
    live = RecordingLive()
    executor = ImmediateExecutor()
    monkeypatch.setattr(cli, "application", lambda _ctx: service)
    monkeypatch.setattr(cli, "ThreadPoolExecutor", lambda **_options: executor)
    monkeypatch.setattr(cli, "Live", live)
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(cli.time, "sleep", clock.sleep)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("MYNE_REDUCED_MOTION", raising=False)
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    return service, clock, live, executor


@pytest.mark.parametrize("fps", [30, 60])
def test_drawing_does_not_increase_telemetry_rate(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor], fps: int
) -> None:
    service, clock, live, executor = render_environment
    sampled_at: list[float] = []

    def measure() -> dict[str, Any]:
        sampled_at.append(clock.now)
        return dict(service.audio_levels.return_value)

    service.audio_levels.side_effect = measure
    result = CliRunner().invoke(cli.app, ["visualize", "--seconds", "1", "--fps", str(fps)])
    assert result.exit_code == 0, result.output
    assert fps - 1 <= len(live.frames) <= fps + 1
    # Both endpoints can be sampled when the simulated one second boundary
    # rounds down; the interval is the actual rate guarantee.
    assert 8 <= service.audio_levels.call_count <= 11
    assert all(later - earlier >= 0.1 - 1e-9 for earlier, later in zip(sampled_at, sampled_at[1:]))
    assert len(clock.sleeps) == len(live.frames)
    assert clock.now == pytest.approx(11)
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert service.visualizer.call_args_list[-1].args == (False,)


def test_slow_request_never_accumulates_worker_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    pending: Future[dict[str, Any]] = Future()
    executor = Mock()
    executor.submit.return_value = pending
    monkeypatch.setattr(cli, "ThreadPoolExecutor", lambda **_options: executor)
    telemetry = cli._AudioTelemetry(Mock())
    for frame in range(120):
        assert telemetry.poll(frame / 60) is None
    executor.submit.assert_called_once()
    telemetry.close()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)


def test_frames_do_not_duplicate_history() -> None:
    history = deque([0.1, 0.2], maxlen=42)
    sample = {"available": True, "rms": [0.5], "peak": [0.75]}
    for _ in range(60):
        cli._audio_frame(sample, history)
    assert list(history) == [0.1, 0.2]
    filled, empty = cli._audio_bar(0.15625, 4)
    assert filled == "▋"
    assert len(filled + empty) == 4


def test_json_is_one_raw_snapshot_and_always_disables_analysis(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor],
) -> None:
    service, clock, live, executor = render_environment
    result = CliRunner().invoke(cli.app, ["--json", "visualize"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == service.audio_levels.return_value
    service.audio_levels.assert_called_once_with()
    assert not live.frames and not clock.sleeps and executor.submitted == 0
    assert service.visualizer.call_args_list[-1].args == (False,)


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [(["--no-animation"], {}), ([], {"MYNE_REDUCED_MOTION": "true"}), ([], {"TERM": "dumb"})],
)
def test_reduced_motion_caps_render_updates_and_yields(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor],
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    environment: dict[str, str],
) -> None:
    service, clock, live, _executor = render_environment
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    result = CliRunner().invoke(cli.app, ["visualize", "--seconds", "0.5", "--fps", "60", *arguments])
    assert result.exit_code == 0, result.output
    assert live.options["refresh_per_second"] == 10
    assert 5 <= len(live.frames) <= 6
    assert len(clock.sleeps) == len(live.frames)
    assert service.audio_levels.call_count <= 6


def test_no_color_does_not_disable_animation(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    result = CliRunner().invoke(cli.app, ["visualize", "--seconds", "0.1"])
    assert result.exit_code == 0, result.output
    assert render_environment[2].options["refresh_per_second"] == 30


def test_interrupt_closes_worker_and_analysis(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _clock, _live, executor = render_environment
    monkeypatch.setattr(cli.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(cli.app, ["visualize"])
    assert result.exit_code == 0, result.output
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert service.visualizer.call_args_list[-1].args == (False,)


def test_unavailable_track_still_disables_analysis(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor],
) -> None:
    service, _clock, _live, executor = render_environment
    service.visualizer.return_value = {"track_id": None, "error": None}
    result = CliRunner().invoke(cli.app, ["visualize"])
    assert result.exit_code == 1
    assert "Start a song" in result.output
    assert executor.submitted == 0
    assert service.visualizer.call_args_list[-1].args == (False,)


def test_off_does_not_request_levels(
    render_environment: tuple[Mock, Clock, RecordingLive, ImmediateExecutor],
) -> None:
    service, _clock, _live, executor = render_environment
    result = CliRunner().invoke(cli.app, ["visualize", "--off"])
    assert result.exit_code == 0, result.output
    service.visualizer.assert_called_once_with(False)
    service.audio_levels.assert_not_called()
    assert executor.submitted == 0
